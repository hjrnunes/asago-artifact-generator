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
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from .attack_shape import attack_shape_violation
from .contract_kit import (
    ClaimLevel,
    canonical_json,
    contract_dir,
    framed_digest,
    load_json_file,
    parse_document,
    sha256_hex,
    verify_contract_lock,
)
from .value_checks import is_nonblank_str, is_sha256_hex

_HANDOFF_CONDITION_FIELDS = (
    "discriminating_condition",
    "condition_check",
    "condition_omitted_reason",
)
_HANDOFF_ROOT = contract_dir("scenario-handoff")
_REGEX_FLAGS = {"IGNORECASE": re.IGNORECASE}
_OwnershipRules = tuple[frozenset[str], tuple[tuple[str, re.Pattern[str]], ...]]


class InputKind(StrEnum):
    """The supported design-time input representations, recorded in package manifests."""

    SCENARIO_HANDOFF_V5 = "scenario-handoff-v5"


# The structured-analysis fields of scenario-handoff-v5, in the producer's order.
V5_FIELDS = ("uca", "actor", "causal_factors", "preconditions", "procedure", "worst_case_threat")


@dataclass(frozen=True)
class _HandoffVersion:
    """One handoff version the mirrored lock declares: its kit directory and digest domain."""

    kind: InputKind
    kit: str
    digest_domain: str


class InputSourceError(ValueError):
    """Raised when an input or its vendored contract is not trustworthy."""


class HandoffSchemaInvalid(InputSourceError):
    """Raised when a handoff breaks the producer's contract for its version.

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
    """Load one producer scenario handoff and produce a source-pinned view."""

    path = Path(source_path)
    source_bytes = _read_source(path)
    source = SourceSnapshot(str(path), sha256_hex(source_bytes), len(source_bytes))
    payload = _parse_document(path, source_bytes)
    kind = _validate_handoff_schema_version(payload)
    return _handoff_view(path, payload, source_bytes, source, kind)


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
    payload: dict[str, Any],
    source_bytes: bytes,
    source: SourceSnapshot,
    kind: InputKind,
) -> InputView:
    version = _validate_handoff_kit(kind)
    _validate_handoff_payload(payload, version.kit)
    expected_digest = payload.get("content_digest", "")
    digest_payload = {key: value for key, value in payload.items() if key != "content_digest"}
    if expected_digest != _framed_digest(version.digest_domain, digest_payload):
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
        kind=kind,
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


def _validate_handoff_schema_version(document: Any) -> InputKind:
    version = document.get("schema_version") if isinstance(document, dict) else None
    if version not in set(InputKind):
        found = version if isinstance(version, str) else "no schema_version"
        raise InputSourceError(
            "authoring source must be a producer "
            f"{' or '.join(kind.value for kind in InputKind)} document; found {found}"
        )
    return InputKind(version)


def _validate_handoff_kit(kind: InputKind = InputKind.SCENARIO_HANDOFF_V5) -> _HandoffVersion:
    """Verify the mirrored lock and return the version entry it declares for ``kind``.

    The lock's top level declares one version and ``additional_versions``, when present,
    declares the others.
    """

    verify_contract_lock(
        _HANDOFF_ROOT,
        InputSourceError,
        lock_label="vendored handoff lock",
        member_label="vendored handoff kit",
        metadata={"authority": "asago-scenario-generator"},
        metadata_message="vendored handoff kit authority mismatch",
    )
    lock = load_json_file(
        _HANDOFF_ROOT / "CONTRACT.lock", InputSourceError, "cannot read vendored handoff lock"
    )
    top = {
        "handoff_schema_version": lock.get("handoff_schema_version"),
        "digest_domain": lock.get("digest_domain"),
        "kit": str(lock.get("handoff_schema_version", "")).removeprefix("scenario-"),
    }
    for entry in (top, *lock.get("additional_versions", [])):
        if isinstance(entry, dict) and entry.get("handoff_schema_version") == kind.value:
            kit, domain = entry.get("kit"), entry.get("digest_domain")
            if not is_nonblank_str(kit) or not is_nonblank_str(domain):
                raise InputSourceError(
                    f"vendored handoff lock entry for {kind.value} is incomplete"
                )
            return _HandoffVersion(kind, kit, domain)
    raise InputSourceError(f"vendored handoff lock does not declare {kind.value}")


def _handoff_schema(kit: str = "handoff-v5") -> dict[str, Any]:
    return load_json_file(
        _HANDOFF_ROOT / kit / "schema.json", InputSourceError, f"cannot read vendored {kit} schema"
    )


# One violation: its code, the dotted path of the break, and the reason.
_Violation = tuple[str, str, str]


def _validate_handoff_payload(payload: dict[str, Any], kit: str) -> None:
    """Report the producer's codes for every rule the handoff breaks, ownership first.

    The mirrored schema of the handoff's version runs whole. Only a schema-valid
    handoff reaches the hand rules, which the schema cannot state; the first one
    that fails reports its top-level field. A break confined to the attack shape
    keeps its typed refusal.
    """

    ownership = _ownership_violations(payload)
    breaks = _schema_violations(payload, kit) or _hand_rule_violations(payload, kit)
    if not ownership:
        _raise_shape_refusal(payload, breaks, kit)
    violations = [*ownership, *breaks]
    if violations:
        raise HandoffSchemaInvalid(violations)


def _raise_shape_refusal(payload: dict[str, Any], breaks: list[_Violation], kit: str) -> None:
    """Raise the typed attack-shape refusal when every break concerns the shape."""

    if breaks and {code for code, _, _ in breaks} <= _SHAPE_CODES:
        refusal = _attack_shape_refusal(payload, kit)
        if refusal is not None:
            raise refusal


_SHAPE_CODES = frozenset({"schema_violation:attack_shape", "schema_violation:<root>"})


def _schema_violations(payload: dict[str, Any], kit: str) -> list[_Violation]:
    """Map the schema's errors to the producer's codes: one per top-level field.

    A ``required`` error names the missing field; an ``additionalProperties``
    error names each extra field. The root if/then/else pairings report
    ``<root>`` only when no field breaks.
    """

    found: dict[str, _Violation] = {}
    rooted: _Violation | None = None
    # The producer lists a missing required field before the fields it rejects as extra,
    # whatever order the schema's keywords come in.
    errors = sorted(
        Draft202012Validator(_handoff_schema(kit)).iter_errors(payload),
        key=lambda error: not (error.validator == "required" and not error.path),
    )
    for error in errors:
        where, reason = _error_detail(error)
        if _is_root_pairing(error):
            rooted = rooted or ("schema_violation:<root>", where, reason)
            continue
        for name in _error_fields(error):
            found.setdefault(name, (f"schema_violation:{name}", where, reason))
    if found or rooted is None:
        return list(found.values())
    return [rooted]


def _error_detail(error: Any) -> tuple[str, str]:
    # best_match descends into anyOf branches, so the reason names the break
    # inside an optional object rather than restating the whole object.
    detail = best_match([error]) or error
    return ".".join(str(part) for part in detail.absolute_path) or "<root>", detail.message


def _is_root_pairing(error: Any) -> bool:
    return bool(error.schema_path) and error.schema_path[0] == "allOf"


def _error_fields(error: Any) -> list[str]:
    if error.path:
        return [str(error.path[0])]
    if error.validator == "required":
        return [error.message.split("'")[1]]
    if error.validator == "additionalProperties":
        return re.findall(r"'([^']+)'", error.message.split("(", 1)[1])
    return ["<root>"]


def _attack_shape_refusal(payload: dict[str, Any], kit: str) -> ShapeVersionMalformed | None:
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
    violation = attack_shape_violation(shape, _handoff_schema(kit))
    return ShapeVersionMalformed(*violation) if violation is not None else None


def _hand_rule_violations(payload: dict[str, Any], kit: str) -> list[_Violation]:
    found = _hand_rule_violation(payload, kit)
    return [found] if found is not None else []


def _hand_rule_violation(payload: dict[str, Any], kit: str) -> _Violation | None:
    """Return the first rule the schema cannot state that a schema-valid handoff breaks.

    The rules are blank text, the observation-contract format, presence where
    the schema has a default, and cross-field pairings. The reported code names
    the top-level field the rule reads; the v5 procedure chain, a rule across
    the steps that the producer checks on the whole handoff, reports ``<root>``.
    """

    for key, rules in _HAND_RULES:
        if payload.get(key) is None:
            continue
        for rule in rules:
            found = rule(payload[key], key)
            if found is not None:
                return (f"schema_violation:{key}", *found)
    if payload.get("attack_shape") is not None:
        refusal = _attack_shape_refusal(payload, kit)
        if refusal is not None:
            return ("schema_violation:attack_shape", refusal.schema_path, refusal.reason)
    if "procedure" in payload:
        found = _procedure_chain(payload["procedure"], "procedure")
        if found is not None:
            return ("schema_violation:<root>", *found)
    return None


_Break = tuple[str, str] | None


def _nonblank_text(value: str, path: str) -> _Break:
    return None if value.strip() else (path, "must not be blank")


def _semantic_failure_call(value: dict[str, Any], path: str) -> _Break:
    return _nonblank_text(value["operation_name"], f"{path}.operation_name")


def _observation_contract(value: dict[str, Any], path: str) -> _Break:
    for key in ("contract_schema", "contract_id", "contract_digest"):
        if not value[key].strip():
            return f"{path}.{key}", "must not be blank"
    if not is_sha256_hex(value["contract_digest"]):
        return f"{path}.contract_digest", "is not a SHA-256 hex digest"
    if value["contract_schema"] != "observation-contract-v1":
        return f"{path}.contract_schema", "must be observation-contract-v1"
    return None


def _observation_criteria(value: dict[str, Any], path: str) -> _Break:
    for index, criterion in enumerate(value["criteria"]):
        found = _observation_criterion(criterion, f"{path}.criteria.{index}")
        if found is not None:
            return found
    return None


def _observation_criterion(criterion: dict[str, Any], path: str) -> _Break:
    for key in ("criterion_id", "outcome", "reason"):
        if not criterion[key].strip():
            return f"{path}.{key}", "must not be blank"
    return _criterion_claim(criterion, path) or _criterion_operation(criterion, path)


def _criterion_claim(criterion: dict[str, Any], path: str) -> _Break:
    if not criterion["observable"]:
        if any(criterion.get(key) is not None for key in ("claim_level", "evidence")):
            return path, "an analytical-only criterion must omit claim_level and evidence"
        return None
    for key in ("claim_level", "evidence"):
        if not is_nonblank_str(criterion.get(key)):
            return path, f"an observable criterion requires {key}"
    return None


def _criterion_operation(criterion: dict[str, Any], path: str) -> _Break:
    operation_name = criterion.get("operation_name")
    if operation_name is None:
        return None
    if not is_nonblank_str(operation_name):
        return f"{path}.operation_name", "must not be blank"
    if not criterion["observable"]:
        return path, "an analytical-only criterion must omit operation_name"
    return None


def _observation_assessment(value: dict[str, Any], path: str) -> _Break:
    assessment = value["assessment"]
    path = f"{path}.assessment"
    if not {"supported_criteria", "unsupported_criteria"} <= set(assessment):
        return path, "requires supported_criteria and unsupported_criteria"
    if not assessment["reason"].strip():
        return f"{path}.reason", "must not be blank"
    for key in ("supported_criteria", "unsupported_criteria"):
        for index, item in enumerate(assessment[key]):
            if not item.strip():
                return f"{path}.{key}.{index}", "must not be blank"
    return None


_SAFE_OUTCOME_EXPECTED_EVIDENCE = {
    ClaimLevel.COMMAND_ATTEMPT.value: "tool_call",
    ClaimLevel.REPLY.value: "assistant_message",
    ClaimLevel.RETURNED_RESULT.value: "tool_result",
    ClaimLevel.STATE_EFFECT.value: "snapshot",
}


def _safe_outcome_claim(value: dict[str, Any], path: str) -> _Break:
    if not value["statement"].strip():
        return f"{path}.statement", "must not be blank"
    claim_level = value.get("claim_level")
    operation_name = value.get("operation_name")
    if not value["observable"]:
        if any(
            value.get(key) is not None for key in ("claim_level", "evidence", "operation_name")
        ):
            return path, (
                "an analytical-only safe outcome must omit claim_level, evidence "
                "and operation_name"
            )
        return None
    if (
        claim_level not in _SAFE_OUTCOME_EXPECTED_EVIDENCE
        or value.get("evidence") != _SAFE_OUTCOME_EXPECTED_EVIDENCE[claim_level]
    ):
        return path, "claim_level and evidence do not match"
    if operation_name is not None and not is_nonblank_str(operation_name):
        return f"{path}.operation_name", "must not be blank"
    return None


def _safe_outcome_refs(value: dict[str, Any], path: str) -> _Break:
    for key in ("record_refs", "fact_refs"):
        if key not in value:
            return f"{path}.{key}", "is required"
        refs = value[key]
        for index, item in enumerate(refs):
            if not item.strip():
                return f"{path}.{key}.{index}", "must not be blank"
        if len(refs) != len(set(refs)):
            return f"{path}.{key}", "must be unique"
    if not value["observable"] and (value["record_refs"] or value["fact_refs"]):
        return path, "an analytical-only safe outcome must omit record and fact references"
    return None


def _safe_outcome_call_arguments(value: dict[str, Any], path: str) -> _Break:
    """State each argument of the safe call once, by a non-blank name.

    The rules are the producer's ``SafeObservableOutcome`` call-argument checks;
    keep both in step. An empty list states no value, as an absent one does.
    """

    arguments = value.get("call_arguments")
    if not arguments:
        return None
    path = f"{path}.call_arguments"
    names = [item["argument"] for item in arguments]
    for index, name in enumerate(names):
        if not name.strip():
            return f"{path}.{index}.argument", "must not be blank"
    if not value["observable"]:
        return path, "an analytical-only safe outcome must omit call_arguments"
    if value.get("claim_level") != ClaimLevel.COMMAND_ATTEMPT.value or not value.get(
        "operation_name"
    ):
        return path, "requires claim_level command_attempt and an operation_name"
    if len({name.strip() for name in names}) != len(names):
        return path, "must name each argument once"
    return None


def _procedure_chain(steps: list[dict[str, Any]], path: str) -> _Break:
    """Number the steps S00, S01, ... in order and link each to the next.

    A step names a tactic exactly when it names a technique. The rules are the
    producer's ``ScenarioHandoffV5.validate_procedure_chain``; keep both in step.
    """

    ids = [f"S{index:02d}" for index in range(len(steps))]
    for index, step in enumerate(steps):
        if step["step_id"] != ids[index]:
            return f"{path}.{index}.step_id", f"must be {ids[index]}"
        if step["leads_to"] != ids[index + 1 : index + 2]:
            return f"{path}.{index}.leads_to", f"must be {ids[index + 1 : index + 2]}"
        if (step["tactic"] is None) != (step["technique"] is None):
            return f"{path}.{index}", "names a tactic exactly when it names a technique"
    return None


def _taxonomy_references(taxonomy: dict[str, Any], path: str) -> _Break:
    """List the references sorted by scheme and id, each once.

    The rule is the producer's ``HandoffTaxonomy.validate_reference_order``; keep both in step.
    """

    keys = [(ref["scheme"], ref["id"]) for ref in taxonomy.get("references", [])]
    if keys != sorted(set(keys)):
        return f"{path}.references", "must be sorted by scheme and id, each once"
    return None


def _tool_call_status_reason(status: dict[str, Any], path: str) -> _Break:
    if (status["status"] == "bound") != (status["reason"] == "bound"):
        return path, "reason must be bound exactly when the status is bound"
    return None


def _deduplication(value: dict[str, Any], path: str) -> _Break:
    if not value["scenario_id"].strip():
        return f"{path}.scenario_id", "must not be blank"
    duplicate_of = value.get("duplicate_of")
    if value["status"] != "duplicate":
        if duplicate_of is not None:
            return path, "canonical and analytical-only handoffs must omit duplicate_of"
    elif duplicate_of is None:
        return path, "a duplicate requires duplicate_of"
    elif not duplicate_of.strip():
        return f"{path}.duplicate_of", "must not be blank"
    return _deduplication_key(value["key"], f"{path}.key")


def _deduplication_key(key: dict[str, Any], path: str) -> _Break:
    for name in ("condition", "uca_id", "control_action_id", "operation_name"):
        if key.get(name) is not None and not key[name].strip():
            return f"{path}.{name}", "must not be blank"
    for index, constraint_id in enumerate(key.get("constraint_ids", ())):
        if not constraint_id.strip():
            return f"{path}.constraint_ids.{index}", "must not be blank"
    return None


_HAND_RULES: tuple[tuple[str, tuple[Any, ...]], ...] = (
    *(
        (key, (_nonblank_text,))
        for key in (
            "scenario_id",
            "hypothesis_framing",
            "narrative",
            "semantic_failure_criterion",
            "safe_alternative",
        )
    ),
    ("observation", (_observation_contract, _observation_criteria, _observation_assessment)),
    ("semantic_failure_call", (_semantic_failure_call,)),
    (
        "safe_observable_outcome",
        (_safe_outcome_claim, _safe_outcome_refs, _safe_outcome_call_arguments),
    ),
    ("deduplication", (_deduplication,)),
    ("tool_call_condition_status", (_tool_call_status_reason,)),
    ("taxonomy", (_taxonomy_references,)),
)


def _ownership_rules() -> _OwnershipRules:
    """Return the producer's ownership rules from the mirrored, lock-checked kit."""

    return _read_ownership_rules(_HANDOFF_ROOT / "ownership-rules.json")


def _read_ownership_rules(path: Path) -> _OwnershipRules:
    rules = load_json_file(path, InputSourceError, "cannot read vendored ownership rules")
    patterns = []
    for entry in rules["forbidden_value_patterns"]:
        unknown = sorted(set(entry["flags"]) - _REGEX_FLAGS.keys())
        if unknown:
            raise InputSourceError(
                f"vendored ownership rule {entry['code']} has unknown flags: {unknown}"
            )
        flags = 0
        for name in entry["flags"]:
            flags |= _REGEX_FLAGS[name]
        patterns.append((entry["code"], re.compile(entry["pattern"], flags)))
    return frozenset(rules["forbidden_keys"]), tuple(patterns)


def _ownership_violations(payload: dict[str, Any]) -> list[_Violation]:
    forbidden_keys, forbidden_patterns = _ownership_rules()
    found: dict[str, _Violation] = {}
    for path, text in _keys_and_strings(payload):
        leaf = path.rsplit(".", 1)[-1].lower()
        where = path.lstrip(".")
        if leaf in forbidden_keys:
            code = f"artifact_design_field:{leaf}"
            found.setdefault(code, (code, where, "is an artifact-design field"))
        for slug, pattern in forbidden_patterns:
            if pattern.search(text):
                code = f"prose_hiding:{slug}"
                found.setdefault(code, (code, where, f"matches the {slug} pattern"))
    return list(found.values())


def _keys_and_strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    """List ``(path, text)`` for every mapping key and string scalar, as the producer does."""
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append((f"{path}.{key}", str(key)))
            found.extend(_keys_and_strings(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_keys_and_strings(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        found.append((path, value))
    return found


def _framed_digest(domain: str, value: Any) -> str:
    return framed_digest(domain, value, nfc=True)


def _companion_mismatch(gherkin: dict[str, Any], text: str) -> str | None:
    """Name the first rule a feature file breaks, or None when it holds the block.

    The rules are the producer's `feature_matches`
    (`asago_scenario_generator/report/scenario_panels.py`); keep both in step.
    """
    lines = [stripped for line in text.splitlines() if (stripped := line.strip())]
    for rule in (_feature_rule, _scenario_rule, _steps_rule, _comments_rule):
        if failed := rule(gherkin, lines):
            return failed
    return None


def _feature_rule(gherkin: dict[str, Any], lines: list[str]) -> str | None:
    if lines[:1] == [f"Feature: {gherkin['feature']}"]:
        return None
    return f"Feature line: the first line is not 'Feature: {gherkin['feature']}'"


def _scenario_rule(gherkin: dict[str, Any], lines: list[str]) -> str | None:
    if f"Scenario: {gherkin['scenario']}" in lines:
        return None
    return f"Scenario line: no line reads 'Scenario: {gherkin['scenario']}'"


def _steps_rule(gherkin: dict[str, Any], lines: list[str]) -> str | None:
    skipped = ("Feature:", "Background:", "Scenario:", "#")
    steps = [line for line in lines if not line.startswith(skipped)]
    expected = [
        step for key in ("given", "when", "then_expected") for step in gherkin.get(key, [])
    ]
    if steps == expected:
        return None
    return "steps: they differ from given, when and then_expected in order"


def _comments_rule(gherkin: dict[str, Any], lines: list[str]) -> str | None:
    comments = sum(line.startswith("#") for line in lines)
    expected = len(gherkin.get("then_unsafe_alternative", []))
    if comments == expected:
        return None
    return f"comment count: {comments} comments, then_unsafe_alternative holds {expected}"


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
    "V5_FIELDS",
    "load_input",
]
