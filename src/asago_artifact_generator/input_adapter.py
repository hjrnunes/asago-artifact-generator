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

from .attack_shape import attack_shape_violation, implicit_attack_shape
from .contract_kit import (
    CLAIM_LEVELS,
    ClaimLevel,
    canonical_json,
    first_schema_error,
    framed_digest,
    sha256_hex,
    verify_contract_lock,
)
from .value_checks import is_nonblank_str, is_sha256_hex

_HANDOFF_SCHEMA_VERSION = "scenario-handoff-v3"
_HANDOFF_SCHEMA_VERSION_V4 = "scenario-handoff-v4"
_HANDOFF_DIGEST_DOMAIN = "scenario-handoff-v3"
# v1 and v2 kits stay mirrored as frozen contract files, but their documents
# carry no tool_call_condition_status, so authoring cannot decide whether a
# command_attempt scenario is executable.
_FROZEN_HANDOFF_SCHEMA_VERSIONS = frozenset({"scenario-handoff-v1", "scenario-handoff-v2"})
_HANDOFF_CONDITION_FIELDS = (
    "discriminating_condition",
    "condition_check",
    "condition_omitted_reason",
)
_HANDOFF_TOOL_CALL_FIELDS = ("tool_call_condition_status", "tool_call_condition")
_HANDOFF_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "scenario-handoff"
# The ownership rules below follow the producer's handoff_ownership_violations;
# the vendored kit's invalid cases fail when the two drift apart.
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
_FORBIDDEN_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "role_serialization",
        re.compile(r"\broles?\s*\"?\s*:\s*\"?(user|assistant|system)\b", re.I),
    ),
    ("turn_array", re.compile(r"\bturns?\s*\"?\s*:", re.I)),
    ("prepared_message_field", re.compile(r"prepared_user_text", re.I)),
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


class InputKind(StrEnum):
    """The supported design-time input representation, recorded in package manifests."""

    SCENARIO_HANDOFF_V3 = "scenario-handoff-v3"
    SCENARIO_HANDOFF_V4 = "scenario-handoff-v4"


class InputSourceError(ValueError):
    """Raised when an input or its vendored contract is not trustworthy."""


class ShapeVersionMalformed(InputSourceError):
    """Raised when a v4 handoff's ``attack_shape`` breaks the vendored contract.

    The pre-model refusal code is ``shape_version_malformed``; ``schema_path``
    locates the break.
    """

    code = "shape_version_malformed"

    def __init__(self, schema_path: str, reason: str, *, violation: str = "attack_shape") -> None:
        self.schema_path = schema_path
        self.reason = reason
        super().__init__(
            f"{self.code}: handoff schema invalid: schema_violation:{violation} "
            f"({schema_path}: {reason})"
        )


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
        """Return the producer's attack shape; None for a functional v4 scenario.

        A handoff without the key (v3) carries one attacker message, so it gets
        the implicit single-turn direct shape.
        """

        if "attack_shape" not in self.payload:
            return implicit_attack_shape()
        value = self.payload["attack_shape"]
        return deepcopy(value) if value is not None else None


def load_input(source_path: str | Path) -> InputView:
    """Load one producer scenario handoff and produce a source-pinned view."""

    path = Path(source_path)
    source_bytes = _read_source(path)
    source = SourceSnapshot(str(path), sha256_hex(source_bytes), len(source_bytes))
    payload = _parse_document(path, source_bytes)
    return _handoff_view(path, payload, source_bytes, source, _infer_kind(payload))


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
        if path.suffix.lower() == ".json":
            return json.loads(source_bytes)
        return yaml.safe_load(source_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InputSourceError(f"cannot parse input source {path}: {exc}") from exc


def _handoff_view(
    path: Path,
    payload: dict[str, Any],
    source_bytes: bytes,
    source: SourceSnapshot,
    kind: InputKind,
) -> InputView:
    _validate_handoff_kit()
    _validate_handoff_payload(payload, kind)
    expected_digest = payload.get("content_digest", "")
    digest_payload = {key: value for key, value in payload.items() if key != "content_digest"}
    if expected_digest != _framed_digest(kind.value, digest_payload):
        raise InputSourceError("scenario handoff content_digest does not match source")
    schema_error = first_schema_error(_handoff_schema(kind.value), payload)
    if schema_error is not None:
        raise InputSourceError(f"handoff schema invalid {schema_error}")
    gherkin = payload["gherkin"]
    gherkin_bytes = canonical_json(gherkin, nfc=True).encode("utf-8")
    companion = path.with_suffix(".feature")
    if companion.is_file():
        gherkin_bytes = _read_source(companion)
        try:
            gherkin_text = gherkin_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InputSourceError(f"Gherkin companion is not UTF-8: {companion}") from exc
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


def _infer_kind(document: Any) -> InputKind:
    version = document.get("schema_version") if isinstance(document, dict) else None
    if version == _HANDOFF_SCHEMA_VERSION_V4:
        return InputKind.SCENARIO_HANDOFF_V4
    if version in {_HANDOFF_SCHEMA_VERSION, *_FROZEN_HANDOFF_SCHEMA_VERSIONS}:
        return InputKind.SCENARIO_HANDOFF_V3
    raise InputSourceError(
        "authoring source must be a producer scenario-handoff-v3 or scenario-handoff-v4 document"
    )


def _validate_handoff_kit() -> None:
    verify_contract_lock(
        _HANDOFF_ROOT,
        InputSourceError,
        lock_label="vendored handoff lock",
        member_label="vendored handoff kit",
        metadata={"authority": "asago-scenario-generator"},
        metadata_message="vendored handoff kit authority mismatch",
    )


def _handoff_schema(version: str = _HANDOFF_SCHEMA_VERSION) -> dict[str, Any]:
    kit = version.removeprefix("scenario-")
    schema_path = _HANDOFF_ROOT / kit / "schema.json"
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputSourceError(f"cannot read vendored {kit} schema: {exc}") from exc


def _validate_handoff_payload(
    payload: dict[str, Any], kind: InputKind = InputKind.SCENARIO_HANDOFF_V3
) -> None:
    _validate_handoff_schema_version(payload.get("schema_version"), kind)
    _validate_handoff_field_names(payload, kind)
    if payload.get("kind") not in {"adversarial", "functional"}:
        raise InputSourceError("handoff kind is invalid")
    for key in (
        "scenario_id",
        "hypothesis_framing",
        "narrative",
        "semantic_failure_criterion",
        "safe_alternative",
    ):
        if not isinstance(payload[key], str) or not payload[key].strip():
            raise InputSourceError(f"handoff field is blank or mistyped: {key}")
    if not isinstance(payload["attack_tree"], dict) or not isinstance(payload["lineage"], dict):
        raise InputSourceError("handoff attack_tree and lineage must be objects")
    _validate_handoff_metadata(payload)
    _validate_handoff_gherkin(payload["gherkin"])
    violations = _ownership_violations(payload)
    if violations:
        raise InputSourceError(f"handoff ownership violation: {', '.join(violations)}")
    if kind is InputKind.SCENARIO_HANDOFF_V4:
        _validate_attack_shape(payload)


def _validate_attack_shape(payload: dict[str, Any]) -> None:
    """Validate the v4 ``attack_shape`` the top-level field-name check leaves unread.

    The ownership scan has already run, so a forbidden key inside the shape is
    reported by its own code first.
    """

    shape = payload["attack_shape"]
    if (payload["kind"] == "adversarial") != (shape is not None):
        raise ShapeVersionMalformed(
            "attack_shape",
            f"a {payload['kind']} scenario "
            + ("has no" if shape is None else "must not have")
            + " attack_shape",
            violation="<root>",
        )
    if shape is None:
        return
    violation = attack_shape_violation(shape, _handoff_schema(_HANDOFF_SCHEMA_VERSION_V4))
    if violation is not None:
        raise ShapeVersionMalformed(*violation)


def _validate_handoff_schema_version(
    schema_version: Any, kind: InputKind = InputKind.SCENARIO_HANDOFF_V3
) -> None:
    if schema_version is None or schema_version in _FROZEN_HANDOFF_SCHEMA_VERSIONS:
        raise InputSourceError(
            f"{schema_version or 'scenario-handoff-v1'} handoffs carry no "
            "tool_call_condition_status; authoring requires a scenario-handoff-v3 or "
            "scenario-handoff-v4 document"
        )
    if schema_version != kind.value:
        raise InputSourceError("unknown scenario handoff schema version")


def _validate_handoff_field_names(
    payload: dict[str, Any], kind: InputKind = InputKind.SCENARIO_HANDOFF_V3
) -> None:
    schema = _handoff_schema(kind.value)
    missing = set(schema["required"]) - payload.keys()
    if "attack_shape" in missing:
        raise ShapeVersionMalformed("attack_shape", "the required property is missing")
    unknown = set(payload) - set(schema["properties"])
    if missing or unknown:
        raise InputSourceError(
            f"handoff schema invalid (missing={sorted(missing)}, unknown={sorted(unknown)})"
        )


def _validate_handoff_metadata(payload: dict[str, Any]) -> None:
    """Validate the optional producer metadata, the condition fields, and the tool-call fields."""

    if payload.get("observation") is not None:
        _validate_observation_metadata(payload["observation"])
    if payload.get("safe_observable_outcome") is not None:
        _validate_safe_observable_outcome(payload["safe_observable_outcome"])
    if payload.get("deduplication") is not None:
        _validate_deduplication(payload["deduplication"])
    _validate_schema_fields(payload)
    _validate_tool_call_status_reason(payload["tool_call_condition_status"])


def _validate_handoff_gherkin(gherkin: Any) -> None:
    if (
        not isinstance(gherkin, dict)
        or not isinstance(gherkin.get("feature"), str)
        or not isinstance(gherkin.get("scenario"), str)
    ):
        raise InputSourceError("handoff Gherkin is invalid")
    for key, value in gherkin.items():
        if key not in {
            "feature",
            "scenario",
            "given",
            "when",
            "then_expected",
            "then_unsafe_alternative",
        }:
            raise InputSourceError(f"handoff Gherkin has unknown field: {key}")
        if key not in {"feature", "scenario"} and not isinstance(value, list):
            raise InputSourceError(f"handoff Gherkin field is not a list: {key}")


def _validate_observation_metadata(value: Any) -> None:
    """Validate additive producer testability metadata without target facts."""

    if not isinstance(value, dict):
        raise InputSourceError("handoff observation metadata must be an object")
    required = {"contract_schema", "contract_id", "contract_digest", "criteria", "assessment"}
    unknown = set(value) - required
    missing = required - set(value)
    if missing or unknown:
        raise InputSourceError(
            "handoff observation metadata is invalid "
            f"(missing={sorted(missing)}, unknown={sorted(unknown)})"
        )
    _validate_observation_contract(value)
    criteria = value["criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise InputSourceError("handoff observation criteria must be a non-empty list")
    for criterion in criteria:
        _validate_observation_criterion(criterion)
    _validate_observation_assessment(value["assessment"])


def _validate_observation_contract(value: dict[str, Any]) -> None:
    for key in ("contract_schema", "contract_id", "contract_digest"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise InputSourceError(f"handoff observation field is blank or mistyped: {key}")
    if not is_sha256_hex(value["contract_digest"]):
        raise InputSourceError("handoff observation contract_digest is not a SHA-256 hex digest")
    if value["contract_schema"] != "observation-contract-v1":
        raise InputSourceError("unknown observation contract schema")


_OBSERVATION_CRITERION_KEYS = frozenset(
    {
        "criterion_id",
        "outcome",
        "observable",
        "claim_level",
        "evidence",
        "operation_name",
        "reason",
    }
)
_REQUIRED_OBSERVATION_CRITERION_KEYS = frozenset(
    {
        "criterion_id",
        "outcome",
        "observable",
        "reason",
    }
)


def _validate_observation_criterion(criterion: Any) -> None:
    if not isinstance(criterion, dict):
        raise InputSourceError("handoff observation criterion must be an object")
    if (
        not _REQUIRED_OBSERVATION_CRITERION_KEYS <= set(criterion)
        or not set(criterion) <= _OBSERVATION_CRITERION_KEYS
    ):
        raise InputSourceError("handoff observation criterion fields are invalid")
    if not all(is_nonblank_str(criterion[key]) for key in ("criterion_id", "outcome", "reason")):
        raise InputSourceError("handoff observation criterion text is invalid")
    if not isinstance(criterion["observable"], bool):
        raise InputSourceError("handoff observation criterion observable is invalid")
    _validate_observation_criterion_claim(criterion)
    _validate_observation_criterion_operation(criterion)


def _validate_observation_criterion_operation(criterion: dict[str, Any]) -> None:
    operation_name = criterion.get("operation_name")
    if operation_name is not None and not is_nonblank_str(operation_name):
        raise InputSourceError("observation criterion operation_name is invalid")
    if not criterion["observable"] and operation_name is not None:
        raise InputSourceError("analytical-only criterion must omit operation_name")


def _validate_observation_criterion_claim(criterion: dict[str, Any]) -> None:
    if criterion["observable"]:
        if not is_nonblank_str(criterion.get("claim_level")):
            raise InputSourceError("observable criterion requires claim_level")
        if not is_nonblank_str(criterion.get("evidence")):
            raise InputSourceError("observable criterion requires evidence")
    elif criterion.get("claim_level") is not None or criterion.get("evidence") is not None:
        raise InputSourceError("analytical-only criterion must omit claim_level and evidence")


def _validate_observation_assessment(assessment: Any) -> None:
    if not isinstance(assessment, dict):
        raise InputSourceError("handoff observation assessment must be an object")
    assessment_keys = {
        "disposition",
        "reason",
        "supported_criteria",
        "unsupported_criteria",
    }
    if set(assessment) != assessment_keys:
        raise InputSourceError("handoff observation assessment fields are invalid")
    if assessment["disposition"] not in {"executable", "analytical_only"}:
        raise InputSourceError("handoff observation disposition is invalid")
    if not isinstance(assessment["reason"], str) or not assessment["reason"].strip():
        raise InputSourceError("handoff observation assessment reason is invalid")
    for key in ("supported_criteria", "unsupported_criteria"):
        if not _is_nonblank_text_list(assessment[key]):
            raise InputSourceError(f"handoff observation assessment {key} is invalid")


def _is_nonblank_text_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def _validate_safe_observable_outcome(value: Any) -> None:
    """Validate the producer-owned scenario-specific safe boundary."""

    if not isinstance(value, dict):
        raise InputSourceError("handoff safe_observable_outcome must be an object")
    allowed = {
        "observable",
        "statement",
        "claim_level",
        "evidence",
        "operation_name",
        "record_refs",
        "fact_refs",
    }
    if set(value) - allowed:
        raise InputSourceError("handoff safe_observable_outcome fields are invalid")
    if not isinstance(value.get("observable"), bool):
        raise InputSourceError("handoff safe outcome observable is invalid")
    if not isinstance(value.get("statement"), str) or not value["statement"].strip():
        raise InputSourceError("handoff safe outcome statement is invalid")
    _validate_safe_outcome_claim(value)
    for key in ("record_refs", "fact_refs"):
        _validate_safe_outcome_refs(key, value.get(key))
    if not value["observable"] and any(value.get(key) for key in ("record_refs", "fact_refs")):
        raise InputSourceError(
            "analytical-only handoff safe outcome must omit record and fact references"
        )


_SAFE_OUTCOME_EXPECTED_EVIDENCE = {
    ClaimLevel.COMMAND_ATTEMPT.value: "tool_call",
    ClaimLevel.REPLY.value: "assistant_message",
    ClaimLevel.RETURNED_RESULT.value: "tool_result",
    ClaimLevel.STATE_EFFECT.value: "snapshot",
}


def _validate_safe_outcome_claim(value: dict[str, Any]) -> None:
    claim_level = value.get("claim_level")
    evidence = value.get("evidence")
    operation_name = value.get("operation_name")
    if value["observable"]:
        if (
            claim_level not in _SAFE_OUTCOME_EXPECTED_EVIDENCE
            or evidence != _SAFE_OUTCOME_EXPECTED_EVIDENCE[claim_level]
        ):
            raise InputSourceError("handoff safe outcome claim_level and evidence do not match")
        if operation_name is not None and not is_nonblank_str(operation_name):
            raise InputSourceError("handoff safe outcome operation_name is invalid")
    elif any(value.get(key) is not None for key in ("claim_level", "evidence", "operation_name")):
        raise InputSourceError(
            "analytical-only handoff safe outcome must omit claim and operation"
        )


def _validate_safe_outcome_refs(key: str, refs: Any) -> None:
    if not isinstance(refs, list) or not all(is_nonblank_str(item) for item in refs):
        raise InputSourceError(f"handoff safe outcome {key} is invalid")
    if len(refs) != len(set(refs)):
        raise InputSourceError(f"handoff safe outcome {key} must be unique")


def _validate_schema_fields(payload: dict[str, Any]) -> None:
    """Validate the condition and tool-call fields against the vendored producer schema.

    The root check is the schema's own if/then/else: a bound status requires a
    tool_call_condition, and any other status forbids one.
    """

    schema = _handoff_schema(payload["schema_version"])
    violations = []
    for key in (*_HANDOFF_CONDITION_FIELDS, *_HANDOFF_TOOL_CALL_FIELDS):
        validator = Draft202012Validator({"$defs": schema["$defs"], **schema["properties"][key]})
        if not validator.is_valid(payload.get(key)):
            violations.append(f"schema_violation:{key}")
    root = Draft202012Validator({"$defs": schema["$defs"], "allOf": _tool_call_root_rules(schema)})
    if not violations and not root.is_valid(payload):
        violations.append("schema_violation:<root>")
    if violations:
        raise InputSourceError(f"handoff schema invalid: {', '.join(violations)}")


def _tool_call_root_rules(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the schema's root if/then/else rules that pair the tool-call condition.

    A v3 schema holds that one rule at its root; a v4 schema holds it among the
    ``allOf`` rules, beside the attack-shape pairing the shape validation owns.
    """

    rules = schema["allOf"] if "allOf" in schema else [schema]
    return [
        {key: rule[key] for key in ("if", "then", "else")}
        for rule in rules
        if "tool_call_condition" in rule["else"]["properties"]
    ]


def _validate_tool_call_status_reason(status: dict[str, Any]) -> None:
    if (status["status"] == "bound") != (status["reason"] == "bound"):
        raise InputSourceError(
            "handoff tool_call_condition_status reason must be bound exactly when "
            "the status is bound"
        )


def _validate_deduplication(value: Any) -> None:
    """Validate producer duplicate metadata before authoring consumes it."""

    if not isinstance(value, dict):
        raise InputSourceError("handoff deduplication must be an object")
    required = {"scenario_id", "status", "key"}
    allowed = required | {"duplicate_of"}
    if not required <= set(value) or not set(value) <= allowed:
        raise InputSourceError("handoff deduplication fields are invalid")
    if not isinstance(value["scenario_id"], str) or not value["scenario_id"].strip():
        raise InputSourceError("handoff deduplication scenario_id is invalid")
    status = value["status"]
    if status not in {"canonical", "duplicate", "analytical_only"}:
        raise InputSourceError("handoff deduplication status is invalid")
    _validate_duplicate_of(status, value.get("duplicate_of"))
    _validate_deduplication_key(value["key"])


def _validate_duplicate_of(status: str, duplicate_of: Any) -> None:
    if status == "duplicate":
        if not isinstance(duplicate_of, str) or not duplicate_of.strip():
            raise InputSourceError("duplicate handoff requires duplicate_of")
    elif duplicate_of is not None:
        raise InputSourceError("canonical and analytical-only handoffs must omit duplicate_of")


def _validate_deduplication_key(key: Any) -> None:
    key_required = {"uca_id", "control_action_id", "claim_level"}
    key_optional = {"operation_name", "condition"}
    # The producer omits null fields, so a key without an operation has no
    # operation_name entry.
    if (
        not isinstance(key, dict)
        or not key_required <= set(key)
        or not set(key) <= key_required | key_optional
    ):
        raise InputSourceError("handoff deduplication key is invalid")
    _validate_deduplication_key_fields({"operation_name": None, "condition": None, **key})


def _validate_deduplication_key_fields(key: dict[str, Any]) -> None:
    if key["condition"] is not None and not is_nonblank_str(key["condition"]):
        raise InputSourceError("handoff deduplication condition is invalid")
    if not all(is_nonblank_str(key[field]) for field in ("uca_id", "control_action_id")):
        raise InputSourceError("handoff deduplication key identity is invalid")
    if key["operation_name"] is not None and not is_nonblank_str(key["operation_name"]):
        raise InputSourceError("handoff deduplication operation_name is invalid")
    if key["claim_level"] not in {*CLAIM_LEVELS, "unknown"}:
        raise InputSourceError("handoff deduplication claim_level is invalid")


def _ownership_violations(payload: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for path, text in _keys_and_strings(payload):
        leaf = path.rsplit(".", 1)[-1].lower()
        codes = [f"artifact_design_field:{leaf}"] if leaf in _FORBIDDEN_KEYS else []
        codes.extend(
            f"prose_hiding:{slug}"
            for slug, pattern in _FORBIDDEN_VALUE_PATTERNS
            if pattern.search(text)
        )
        found.extend(code for code in codes if code not in found)
    return found


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
    "InputKind",
    "InputSourceError",
    "InputView",
    "ShapeVersionMalformed",
    "SourceSnapshot",
    "load_input",
]
