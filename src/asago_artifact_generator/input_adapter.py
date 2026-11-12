"""Source-pinned, target-free scenario-handoff input adapter."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from asago_contracts_core import CONTRACT_DIR
from asago_handoff import Violation, feature_violations, validate_handoff
from asago_handoff.contract import (
    Contract,
    ContractError,
    load_contract,
    load_ownership_rules,
)
from asago_handoff.envelope import (
    ENVELOPE_FIELD,
    ContractEnvelopeError,
    InvalidDocumentError,
    UnknownFieldError,
)
from asago_handoff.shape import attack_shape_violation

from .contract_kit import (
    canonical_json,
    framed_digest,
    load_json_file,
    parse_document,
    sha256_hex,
    verify_contract_lock,
)

_HANDOFF_CONDITION_FIELDS = (
    "discriminating_condition",
    "condition_check",
    "condition_omitted_reason",
)
_HANDOFF_ROOT = CONTRACT_DIR


class InputKind(StrEnum):
    """The supported design-time input representations, recorded in package manifests."""

    SCENARIO_HANDOFF_V1 = "scenario-handoff-v1"


# The structured-analysis fields of the current handoff, in the producer's order.
HANDOFF_FIELDS = (
    "uca",
    "actor",
    "causal_factors",
    "preconditions",
    "procedure",
    "worst_case_threat",
)
_HANDOFF_VERSION = InputKind.SCENARIO_HANDOFF_V1.value


class InputSourceError(ValueError):
    """Raised when an input or its vendored contract is not trustworthy."""


class HandoffSchemaInvalid(InputSourceError):
    """Raised when a handoff breaks the producer's contract.

    ``codes`` holds the producer's violation codes in the order of the kit's
    ``expected-violations.json``: ownership codes, then ``schema_violation:<field>``.
    ``schema_path`` and ``reason`` describe the first code's break.
    """

    def __init__(self, violations: list[tuple[str, str, str]]) -> None:
        self.codes = tuple(code for code, _, _ in violations)
        _, self.schema_path, self.reason = violations[0]
        super().__init__(
            f"handoff schema invalid: {', '.join(self.codes)} ({self.schema_path}: {self.reason})"
        )


class ShapeVersionMalformed(HandoffSchemaInvalid):
    """Raised when only a handoff's ``attack_shape`` breaks the vendored contract.

    The pre-model refusal code is ``shape_version_malformed``; ``schema_path``
    locates the break.
    """

    code = "shape_version_malformed"

    def __init__(self, schema_path: str, reason: str, *, violation: str = "attack_shape") -> None:
        super().__init__([(f"schema_violation:{violation}", schema_path, reason)])
        self.args = (f"{self.code}: {self.args[0]}",)


@dataclass(frozen=True)
class SourceSnapshot:
    """A hash-verified input source."""

    source_path: str
    sha256: str
    length: int


@dataclass(frozen=True)
class InputView:
    """The complete deterministic view supplied to authoring."""

    kind: InputKind
    scenario_id: str
    payload: dict[str, Any]
    source: SourceSnapshot
    source_bytes: bytes
    narrative: Any
    narrative_bytes: bytes
    gherkin: Any
    gherkin_text: str
    gherkin_bytes: bytes
    source_digests: dict[str, str] = field(default_factory=dict)

    @property
    def source_sha256(self) -> str:
        """Return the exact source-file digest."""

        return self.source.sha256

    @property
    def tool_call_condition_status(self) -> dict[str, str]:
        """Return the producer's verdict on whether the condition bound."""

        return deepcopy(self.payload["tool_call_condition_status"])

    @property
    def tool_call_condition(self) -> dict[str, Any] | None:
        """Return the bound tool-call condition, or None when the status is not bound."""

        value = self.payload.get("tool_call_condition")
        return deepcopy(value) if value is not None else None

    @property
    def attack_shape(self) -> dict[str, Any] | None:
        """Return the producer's attack shape; None for a functional scenario."""

        value = self.payload["attack_shape"]
        return deepcopy(value) if value is not None else None


def load_input(source_path: str | Path) -> InputView:
    """Load one producer scenario handoff and produce a source-pinned view.

    The current handoff names the controlling party ``controlling_party`` and
    requires the envelope. The shared validator owns the version rule; a
    document that states another version, or none, is refused before authoring.
    """

    path = Path(source_path)
    source_bytes = _read_source(path)
    source = SourceSnapshot(str(path), sha256_hex(source_bytes), len(source_bytes))
    payload = _parse_document(path, source_bytes)
    return _handoff_view(path, payload, source_bytes, source)


def build_scenario_handoff_view(view: InputView) -> dict[str, Any]:
    """Project the producer handoff into meaning-bearing model context."""

    result = {
        "kind": view.kind.value,
        "scenario_id": view.scenario_id,
        "narrative": deepcopy(view.narrative),
        "gherkin": view.gherkin_text,
        "semantic_failure_condition": view.payload.get("semantic_failure_criterion", ""),
        "safe_alternative": view.payload.get("safe_alternative", ""),
        "safe_observable_outcome": deepcopy(view.payload.get("safe_observable_outcome")),
        "supplied_facts": deepcopy(view.payload.get("sourced_facts", [])),
        "environment_documentation": {
            "documented_operations": deepcopy(view.payload.get("documented_operations", [])),
            "governing_rules": deepcopy(view.payload.get("governing_rules", [])),
            "assumptions_and_unknowns": deepcopy(view.payload.get("assumptions_and_unknowns", [])),
        },
        "source_references": dict(view.source_digests),
    }
    if view.payload.get("observation") is not None:
        result["observation"] = deepcopy(view.payload["observation"])
    if view.payload.get("semantic_failure_call") is not None:
        result["semantic_failure_call"] = deepcopy(view.payload["semantic_failure_call"])
    # The tool-call fields stay out of model context: code alone decides
    # executability and passes the bound condition through.
    for key in _HANDOFF_CONDITION_FIELDS:
        if view.payload.get(key) is not None:
            result[key] = deepcopy(view.payload[key])
    return result


def _read_source(path: Path) -> bytes:
    if not path.is_file():
        raise InputSourceError(f"input source is not a file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise InputSourceError(f"cannot read input source {path}: {exc}") from exc


def _parse_document(path: Path, source_bytes: bytes) -> Any:
    try:
        return parse_document(path, source_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InputSourceError(f"cannot parse input source {path}: {exc}") from exc


def _handoff_view(
    path: Path,
    payload: Any,
    source_bytes: bytes,
    source: SourceSnapshot,
) -> InputView:
    _validate_handoff_kit()
    _validate_handoff_payload(payload)
    expected_digest = payload.get("content_digest", "")
    digest_payload = {
        key: value
        for key, value in payload.items()
        if key not in ("content_digest", ENVELOPE_FIELD)
    }
    if expected_digest != _framed_digest(_HANDOFF_VERSION, digest_payload):
        raise InputSourceError("scenario handoff content_digest does not match source")
    gherkin = payload["gherkin"]
    gherkin_bytes = canonical_json(gherkin, nfc=True).encode("utf-8")
    companion = path.with_suffix(".feature")
    if companion.is_file():
        gherkin_bytes = _read_source(companion)
        try:
            gherkin_text = gherkin_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InputSourceError(f"Gherkin companion is not UTF-8: {companion}") from exc
        failed_rule = _companion_mismatch(gherkin, gherkin_text)
        if failed_rule:
            raise InputSourceError(
                f"Gherkin companion does not match the handoff gherkin block: {companion}: "
                f"{failed_rule}"
            )
    else:
        gherkin_text = _render_handoff_gherkin(gherkin)
    return InputView(
        kind=InputKind.SCENARIO_HANDOFF_V1,
        scenario_id=payload["scenario_id"],
        payload=payload,
        source=source,
        source_bytes=source_bytes,
        narrative=payload["narrative"],
        narrative_bytes=payload["narrative"].encode("utf-8"),
        gherkin=gherkin,
        gherkin_text=gherkin_text,
        gherkin_bytes=gherkin_bytes,
        source_digests={
            "input": source.sha256,
            "gherkin": sha256_hex(gherkin_bytes),
        },
    )


def _validate_handoff_kit() -> None:
    """Verify the installed lock declares and pins the current handoff kit."""

    lock = load_json_file(
        _HANDOFF_ROOT / "CONTRACT.lock", InputSourceError, "cannot read vendored handoff lock"
    )
    locked = {relative.split("/", 1)[0] for relative in lock.get("files", {})}
    if lock.get("schema_version") != _HANDOFF_VERSION or _HANDOFF_VERSION not in locked:
        raise InputSourceError(f"vendored handoff lock does not declare {_HANDOFF_VERSION}")
    verify_contract_lock(
        _HANDOFF_ROOT,
        InputSourceError,
        lock_label="vendored handoff lock",
        member_label="vendored handoff kit",
        metadata={"authority": "asago-scenario-generator"},
        metadata_message="vendored handoff kit authority mismatch",
    )


def _handoff_schema() -> dict[str, Any]:
    return load_json_file(
        _HANDOFF_ROOT / _HANDOFF_VERSION / "schema.json",
        InputSourceError,
        f"cannot read vendored {_HANDOFF_VERSION} schema",
    )


def _read_ownership_rules(
    path: Path,
) -> tuple[frozenset[str], tuple[tuple[str, re.Pattern[str]], ...]]:
    """Read an ownership-rules document, naming a file the reader cannot use."""

    try:
        return load_ownership_rules(path)
    except ContractError as exc:
        raise InputSourceError(f"cannot read vendored ownership rules: {exc}") from exc


def _handoff_contract() -> Contract:
    """Read the installed contract documents the shared validator walks.

    The shared core's package data publishes the one supported schema.
    """

    try:
        return load_contract(_HANDOFF_ROOT)
    except (ContractError, ContractEnvelopeError) as exc:
        raise InputSourceError(f"cannot read vendored handoff contract: {exc}") from exc


def _validate_handoff_payload(payload: Any) -> None:
    """Report the shared validator's codes for every rule the handoff breaks.

    ``asago_handoff.validate_handoff`` reads the document through the mirrored
    spec first, then walks the ownership boundary, the mirrored schema's rules
    and the hand rules the schema cannot state, the schema version included.
    The consumer keeps the typed attack-shape refusal for a break confined to
    the shape, and reports every other break, including a document that is not
    a JSON object, as :class:`HandoffSchemaInvalid`. A body the version's schema
    rejects still reports the producer's codes: the envelope states who wrote
    the document, not that its content holds. A document the reader alone can
    refuse, such as a missing envelope, names that refusal instead.
    """

    stated = payload.get("schema_version") if isinstance(payload, dict) else None
    if stated is not None and stated != _HANDOFF_VERSION:
        # The kit's own codes for a document of another version, with the stated version named.
        raise HandoffSchemaInvalid(
            [
                Violation(
                    "schema_violation:schema_version",
                    "schema_version",
                    f"the handoff states {stated!r}; this reader reads {_HANDOFF_VERSION}",
                ),
                Violation(
                    "schema_violation:attack_shape",
                    "schema_version",
                    "another schema version states another attack shape",
                ),
            ]
        )
    contract = _handoff_contract()
    try:
        violations = validate_handoff(payload, contract=contract)
    except (InvalidDocumentError, UnknownFieldError):
        violations = validate_handoff(payload, contract=contract, through_spec=False)
    except ContractEnvelopeError as exc:
        raise InputSourceError(f"scenario handoff envelope refused: {exc}") from exc
    if not violations:
        return
    breaks = [item for item in violations if item.code.startswith("schema_violation:")]
    if breaks and len(breaks) == len(violations) and isinstance(payload, dict):
        _raise_shape_refusal(payload, breaks)
    raise HandoffSchemaInvalid(list(violations))


def _raise_shape_refusal(payload: dict[str, Any], breaks: list[Violation]) -> None:
    """Raise the typed attack-shape refusal when every break concerns the shape."""

    if breaks and {item.code for item in breaks} <= _SHAPE_CODES:
        refusal = _attack_shape_refusal(payload)
        if refusal is not None:
            raise refusal


_SHAPE_CODES = frozenset({"schema_violation:attack_shape", "schema_violation:<root>"})


def _attack_shape_refusal(payload: dict[str, Any]) -> ShapeVersionMalformed | None:
    if "attack_shape" not in payload:
        return ShapeVersionMalformed("attack_shape", "the required property is missing")
    shape = payload["attack_shape"]
    if (payload.get("kind") == "adversarial") != (shape is not None):
        return ShapeVersionMalformed(
            "attack_shape",
            f"a {payload.get('kind')} scenario "
            + ("has no" if shape is None else "must not have")
            + " attack_shape",
            violation="<root>",
        )
    if shape is None:
        return None
    violation = attack_shape_violation(shape, _handoff_schema())
    return ShapeVersionMalformed(*violation) if violation is not None else None


def _framed_digest(domain: str, value: Any) -> str:
    return framed_digest(domain, value, nfc=True)


def _companion_mismatch(gherkin: dict[str, Any], text: str) -> str | None:
    """Name the first rule a feature file breaks, or None when it holds the block.

    ``asago_handoff.feature_violations`` states the rule the producer's
    ``feature_matches`` states; the consumer reports its first failure.
    """
    violations = feature_violations(gherkin, text)
    return violations[0].reason if violations else None


def _render_handoff_gherkin(gherkin: dict[str, Any]) -> str:
    lines = [f"Feature: {gherkin['feature']}", f"Scenario: {gherkin['scenario']}"]
    for key in ("given", "when", "then_expected"):
        lines.extend(f"  {line}" for line in gherkin.get(key, []))
    lines.extend(
        f"  # Unsafe alternative (non-executable): {line}"
        for line in gherkin.get("then_unsafe_alternative", [])
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "build_scenario_handoff_view",
    "HandoffSchemaInvalid",
    "InputKind",
    "InputSourceError",
    "InputView",
    "ShapeVersionMalformed",
    "SourceSnapshot",
    "HANDOFF_FIELDS",
    "load_input",
]
