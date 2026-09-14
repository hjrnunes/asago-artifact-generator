"""Versioned scenario handoff reader.

The consumer reads the producer's published scenario handoff: the versioned
envelope over narrative, attack tree, Gherkin and necessary metadata defined
by the vendored contract kit (``contracts/scenario-handoff/``). The reader
fails closed with a typed reason for every defect class:

- ``handoff_unreadable`` — the file did not parse as JSON/YAML
- ``handoff_schema_invalid`` — the envelope does not match the kit schema
- ``unknown_schema_version`` — the envelope version is not supported
- ``content_digest_mismatch`` — the recorded digest does not match content
- ``ownership_violation`` — the handoff carries artifact-design content
- ``lineage_unresolved`` — lineage references do not resolve in the envelope
- ``kit_digest_mismatch`` — the vendored contract kit fails lock verification

The design path admits the handoff plus an explicit environment as its only
producer inputs, so lineage resolution is defined within the envelope:
constraint references must resolve to governing rules, and the controller /
control-action / ICA identity spine must be self-consistent. Hazard and loss
identifiers are recorded with ``producer_declared`` authority; the envelope is
their registry because no other producer input is admitted.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import StrictStr, field_validator

from ..models._base import ImmutableModel, compute_framed_digest

HANDOFF_SCHEMA_VERSION = "scenario-handoff-v1"
HANDOFF_DIGEST_DOMAIN = "scenario-handoff-v1"
_DEFAULT_KIT_PARENTS = 3

#: Field names that only artifact design may own. A handoff carrying any of
#: these keys (at any depth) fails the ownership boundary. Vendored discipline
#: mirrored from the producer's publication side; the kit's invalid fixtures
#: and expected-violations record pin the exact behavior.
_FORBIDDEN_KEYS = frozenset(
    {
        "prepared_user_text",
        "stimulus_turns",
        "turns",
        "turn",
        "role",
        "roles",
        "delivery",
        "delivery_class",
        "delivery_route",
        "route",
        "execution_route",
        "oracle",
        "oracle_kind",
        "oracle_observes",
        "oracle_basis",
        "detector",
        "judge",
        "judge_prompt",
        "prompt",
        "prompts",
        "setup",
        "execution_setup",
        "execution_contract",
        "projection",
        "bundle",
        "payload",
        "messages",
        "message",
        "replay",
        "unsafe_outcome_condition",
        "operands",
        "reference_tool",
        "reference_argument",
        "applies_when",
        "trigger_evidence",
    }
)

#: Prose patterns that betray hiding artifact design inside narrative, tree
#: leaves, Gherkin or intent-like metadata. The first element is the typed
#: violation code used by the contract kit's expected-violations record.
_FORBIDDEN_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "role_serialization",
        re.compile(r"\broles?\s*\"?\s*:\s*\"?(user|assistant|system)\b", re.I),
    ),
    ("turn_array", re.compile(r"\bturns?\s*\"?\s*:", re.I)),
    ("prepared_message_field", re.compile(r"prepared_user_text", re.I)),
    (
        "historical_saved_request",
        re.compile(r"full refund of 100\.0", re.I),
    ),
    (
        "detector_expression",
        re.compile(r"\bgreater_than\b|\bless_than\b|\bamount\s*[<>=]", re.I),
    ),
    (
        "ready_to_send_instruction",
        re.compile(
            r"\bsend this message\b|\bdeliver this message\b|"
            r"\bexecute the following\b|\bsystem prompt is\b",
            re.I,
        ),
    ),
    ("judge_prompt", re.compile(r"\bjudge prompt\b", re.I)),
)


class HandoffValidationError(ValueError):
    """A handoff defect with the typed reason naming the failed check."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class HandoffModel(ImmutableModel):
    """Strict base for handoff documents: unknown keys are rejected."""


class HandoffLineage(HandoffModel):
    """Stable identity plus loss/hazard/constraint lineage."""

    loss_ids: tuple[StrictStr, ...] = ()
    hazard_ids: tuple[StrictStr, ...] = ()
    constraint_ids: tuple[StrictStr, ...] = ()
    ica_slot_id: StrictStr = ""
    ica_id: StrictStr | None = None
    controller_id: StrictStr = ""
    control_action_id: StrictStr = ""

    @field_validator("loss_ids", "hazard_ids", "constraint_ids", mode="before")
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("lineage id collections must be arrays")
        return tuple(value)


class HandoffRule(HandoffModel):
    """One governing rule the unsafe behavior would violate."""

    constraint_id: StrictStr
    statement: StrictStr


class HandoffOperation(HandoffModel):
    """A documented operation named only when it is supported and relevant."""

    name: StrictStr
    relevance: StrictStr


class HandoffFact(HandoffModel):
    """One source-grounded fact with its authority stamp."""

    statement: StrictStr
    source: StrictStr
    authority: StrictStr


class HandoffGherkin(HandoffModel):
    """Declarative Gherkin: no bindings, no prompt, no detector code."""

    feature: StrictStr
    scenario: StrictStr
    given: tuple[StrictStr, ...] = ()
    when: tuple[StrictStr, ...] = ()
    then_expected: tuple[StrictStr, ...] = ()
    then_unsafe_alternative: tuple[StrictStr, ...] = ()

    @field_validator("given", "when", "then_expected", "then_unsafe_alternative", mode="before")
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("gherkin steps must be arrays")
        return tuple(value)


class ScenarioHandoff(HandoffModel):
    """The versioned scenario handoff envelope (consumer mirror)."""

    schema_version: StrictStr = HANDOFF_SCHEMA_VERSION
    scenario_id: StrictStr
    scenario_version: int = 1
    kind: Literal["adversarial", "functional"]
    hypothesis_framing: StrictStr
    narrative: StrictStr
    attack_tree: Mapping[str, Any]
    gherkin: HandoffGherkin
    semantic_failure_criterion: StrictStr
    safe_alternative: StrictStr
    governing_rules: tuple[HandoffRule, ...] = ()
    lineage: HandoffLineage
    documented_operations: tuple[HandoffOperation, ...] = ()
    sourced_facts: tuple[HandoffFact, ...] = ()
    assumptions_and_unknowns: tuple[StrictStr, ...] = ()
    content_digest: StrictStr = ""

    @field_validator(
        "governing_rules",
        "documented_operations",
        "sourced_facts",
        "assumptions_and_unknowns",
        mode="before",
    )
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("handoff collections must be arrays")
        return tuple(value)


class HandoffVerification(HandoffModel):
    """Recorded evidence of the three handoff verifications."""

    schema_version: StrictStr
    content_digest: StrictStr
    digest_verified: bool
    lineage_hazard_ids: tuple[StrictStr, ...] = ()
    lineage_constraint_ids: tuple[StrictStr, ...] = ()
    lineage_loss_ids: tuple[StrictStr, ...] = ()
    lineage_resolved: bool

    @field_validator(
        "lineage_hazard_ids", "lineage_constraint_ids", "lineage_loss_ids", mode="before"
    )
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("lineage verification collections must be arrays")
        return tuple(value)


class VerifiedHandoff:
    """One accepted handoff with its recorded verifications."""

    def __init__(
        self,
        handoff: ScenarioHandoff,
        *,
        source_path: Path,
        source_sha256: str,
        verification: HandoffVerification,
    ) -> None:
        self.handoff = handoff
        self.source_path = source_path
        self.source_sha256 = source_sha256
        self.verification = verification

    @property
    def scenario_id(self) -> str:
        return self.handoff.scenario_id


def _default_kit_dir() -> Path:
    return (
        Path(__file__).resolve().parents[_DEFAULT_KIT_PARENTS] / "contracts" / "scenario-handoff"
    )


def verify_vendored_kit(kit_dir: Path | None = None) -> dict[str, Any]:
    """Verify every vendored kit file against ``CONTRACT.lock``.

    The consumer validates handoffs against its vendored copy of the
    producer-owned kit. Any modification of a vendored kit file fails closed
    before any handoff is read.
    """

    kit = Path(kit_dir) if kit_dir is not None else _default_kit_dir()
    lock_path = kit / "CONTRACT.lock"
    upstream_path = kit / "UPSTREAM.lock"
    if not lock_path.is_file() or not upstream_path.is_file():
        raise HandoffValidationError(
            "kit_digest_mismatch", f"vendored kit locks missing under {kit}"
        )
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        upstream = json.loads(upstream_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffValidationError("kit_digest_mismatch", f"unreadable kit lock: {exc}") from exc
    recorded = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    if upstream.get("contract_lock_sha256") != recorded:
        raise HandoffValidationError(
            "kit_digest_mismatch",
            "UPSTREAM.lock contract_lock_sha256 does not match CONTRACT.lock",
        )
    for relative, expected in sorted(lock.get("files", {}).items()):
        candidate = kit / relative
        if not candidate.is_file():
            raise HandoffValidationError(
                "kit_digest_mismatch", f"vendored kit file missing: {relative}"
            )
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected:
            raise HandoffValidationError(
                "kit_digest_mismatch", f"vendored kit file modified: {relative}"
            )
    return {
        "contract": lock.get("contract"),
        "schema_versions": list(lock.get("handoff_schema_versions", [])),
        "files_verified": len(lock.get("files", {})),
        "contract_lock_sha256": recorded,
    }


def _iter_strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Yield ``(path, text)`` for every mapping key and string scalar."""
    found: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            found.append((f"{path}.{key}", str(key)))
            found.extend(_iter_strings(item, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_iter_strings(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        found.append((path, value))
    return found


def ownership_violations(payload: Mapping[str, Any]) -> list[str]:
    """Return every ownership-boundary violation in one handoff payload.

    A violation is artifact-design content the producer must never author:
    prepared messages or histories, role/turn arrays, harness delivery routes,
    oracle selections, detector expressions, judge prompts, executable setup,
    or such content hidden in prose. Each violation is a typed code of the
    form ``artifact_design_field:<name>`` or ``prose_hiding:<pattern>`` so the
    contract kit can assert an exact expected set. An empty list means the
    boundary holds.
    """

    violations: list[str] = []
    for path, text in _iter_strings(payload):
        leaf = path.rsplit(".", 1)[-1].lower()
        if leaf in _FORBIDDEN_KEYS:
            _record_violation(violations, f"artifact_design_field:{leaf}")
        for slug, pattern in _FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(text):
                _record_violation(violations, f"prose_hiding:{slug}")
    return violations


def _record_violation(violations: list[str], code: str) -> None:
    """Append a typed violation code once, preserving first-seen order."""
    if code not in violations:
        violations.append(code)


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HandoffValidationError("handoff_unreadable", str(exc)) from exc
    try:
        if path.suffix.lower() in {".yaml", ".yml"}:
            payload = yaml.safe_load(text)
        else:
            payload = json.loads(text)
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise HandoffValidationError("handoff_unreadable", str(exc)) from exc
    if not isinstance(payload, dict):
        raise HandoffValidationError("handoff_unreadable", "handoff root must be an object")
    return payload


def _handoff_payload_digest(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "content_digest"}
    return compute_framed_digest(HANDOFF_DIGEST_DOMAIN, body)


def _resolve_lineage(handoff: ScenarioHandoff) -> None:
    """Fail closed when lineage references do not resolve in the envelope."""

    lineage = handoff.lineage
    rule_ids = {rule.constraint_id for rule in handoff.governing_rules}
    for constraint_id in lineage.constraint_ids:
        if constraint_id not in rule_ids:
            raise HandoffValidationError(
                "lineage_unresolved",
                f"lineage constraint {constraint_id} has no governing rule",
            )
    for rule in handoff.governing_rules:
        if rule.constraint_id not in lineage.constraint_ids:
            raise HandoffValidationError(
                "lineage_unresolved",
                f"governing rule {rule.constraint_id} is not referenced by the lineage",
            )
    if handoff.kind == "adversarial":
        if not lineage.hazard_ids:
            raise HandoffValidationError(
                "lineage_unresolved", "adversarial handoff names no hazard"
            )
        if not lineage.loss_ids:
            raise HandoffValidationError("lineage_unresolved", "adversarial handoff names no loss")
    _resolve_identity_spine(lineage)


def _resolve_identity_spine(lineage: HandoffLineage) -> None:
    """Check the controller/action/slot/ICA identity spine when present."""

    slot = lineage.ica_slot_id
    if not slot:
        return
    if lineage.controller_id and not slot.startswith(f"{lineage.controller_id}:"):
        raise HandoffValidationError(
            "lineage_unresolved",
            f"ica slot {slot} does not start with controller {lineage.controller_id}",
        )
    if lineage.control_action_id and f":{lineage.control_action_id}:" not in f":{slot}:":
        raise HandoffValidationError(
            "lineage_unresolved",
            f"ica slot {slot} does not carry control action {lineage.control_action_id}",
        )
    ica_id = lineage.ica_id
    if ica_id and not ica_id.startswith(f"{slot}:"):
        raise HandoffValidationError(
            "lineage_unresolved",
            f"ica id {ica_id} does not extend ica slot {slot}",
        )


def load_scenario_handoff(
    path: Path,
    *,
    kit_dir: Path | None = None,
) -> VerifiedHandoff:
    """Load and fully verify one scenario handoff envelope.

    Every defect class fails closed with a typed reason before any design
    work starts, and no compiled artifact can exist for a rejected handoff.
    """

    kit_report = verify_vendored_kit(kit_dir)
    payload = _load_payload(Path(path))
    versions = kit_report.get("schema_versions", [])
    if payload.get("schema_version") not in versions:
        raise HandoffValidationError(
            "unknown_schema_version",
            f"schema_version {payload.get('schema_version')!r} is not supported by the "
            f"vendored kit ({', '.join(versions)})",
        )
    recorded = _handoff_payload_digest(payload)
    if payload.get("content_digest", "") != recorded:
        raise HandoffValidationError(
            "content_digest_mismatch",
            f"recorded {payload.get('content_digest', '')!r} does not match "
            f"recomputed {recorded!r}",
        )
    violations = ownership_violations(payload)
    if violations:
        raise HandoffValidationError(
            "ownership_violation",
            "the handoff carries artifact-design content: " + ", ".join(violations),
        )
    from pydantic import ValidationError

    try:
        handoff = ScenarioHandoff.model_validate(payload)
    except ValidationError as exc:
        raise HandoffValidationError(
            "handoff_schema_invalid",
            "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_input=False, include_url=False)
            ),
        ) from exc
    _resolve_lineage(handoff)
    verification = HandoffVerification(
        schema_version=handoff.schema_version,
        content_digest=recorded,
        digest_verified=True,
        lineage_hazard_ids=lineage_ids(handoff, "hazard"),
        lineage_constraint_ids=lineage_ids(handoff, "constraint"),
        lineage_loss_ids=lineage_ids(handoff, "loss"),
        lineage_resolved=True,
    )
    source = Path(path)
    return VerifiedHandoff(
        handoff,
        source_path=source,
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        verification=verification,
    )


def lineage_ids(handoff: ScenarioHandoff, kind: str) -> tuple[str, ...]:
    """Return one lineage id collection from a handoff."""

    lineage = handoff.lineage
    return {
        "hazard": lineage.hazard_ids,
        "constraint": lineage.constraint_ids,
        "loss": lineage.loss_ids,
    }[kind]


__all__ = [
    "HANDOFF_DIGEST_DOMAIN",
    "HANDOFF_SCHEMA_VERSION",
    "HandoffValidationError",
    "HandoffVerification",
    "ScenarioHandoff",
    "VerifiedHandoff",
    "lineage_ids",
    "load_scenario_handoff",
    "ownership_violations",
    "verify_vendored_kit",
]
