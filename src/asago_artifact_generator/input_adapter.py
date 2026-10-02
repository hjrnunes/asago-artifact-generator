"""Source-pinned, target-free scenario-handoff input adapter."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

_HANDOFF_SCHEMA_VERSION = "scenario-handoff-v1"
_HANDOFF_SCHEMA_VERSION_V2 = "scenario-handoff-v2"
# Each accepted handoff schema version frames its content digest in its own domain.
_HANDOFF_DIGEST_DOMAINS = {
    _HANDOFF_SCHEMA_VERSION: "scenario-handoff-v1",
    _HANDOFF_SCHEMA_VERSION_V2: "scenario-handoff-v2",
}
_HANDOFF_V2_FIELDS = ("discriminating_condition", "condition_check", "condition_omitted_reason")
_HANDOFF_ROOT = Path(__file__).resolve().parents[2] / "contracts" / "scenario-handoff"


class InputKind(StrEnum):
    """The supported design-time input representation.

    The value names the producer handoff family recorded in package manifests.
    Both scenario-handoff-v1 and scenario-handoff-v2 documents load under it;
    ``InputView.handoff_schema_version`` records the exact document version.
    """

    SCENARIO_HANDOFF_V1 = "scenario-handoff-v1"


class InputSourceError(ValueError):
    """Raised when an input or its vendored contract is not trustworthy."""


@dataclass(frozen=True)
class SourceSnapshot:
    """A hash-verified source and optional immutable working snapshot."""

    source_path: str
    sha256: str
    length: int
    snapshot_path: str | None = None

    @property
    def digest(self) -> str:
        """Compatibility name for callers that use digest terminology."""

        return self.sha256


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
    owner_scope: dict[str, list[dict[str, str]]] | None = None
    handoff_schema_version: str = _HANDOFF_SCHEMA_VERSION

    @property
    def source_sha256(self) -> str:
        """Return the exact source-file digest."""

        return self.source.sha256

    @property
    def input_kind(self) -> str:
        """Return the wire value used in package manifests."""

        return self.kind.value


def snapshot_input(source_path: str | Path, snapshot_dir: str | Path) -> SourceSnapshot:
    """Read one source and copy its exact bytes into a hash-addressed snapshot.

    The source is opened read-only.  The snapshot is written through a
    temporary file and renamed only after its bytes have been flushed.
    """

    path = Path(source_path)
    source_bytes = _read_source(path)
    digest = _sha256(source_bytes)
    directory = Path(snapshot_dir)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{digest}-{path.name}"
    if destination.exists() and destination.read_bytes() != source_bytes:
        raise InputSourceError(f"snapshot collision for {path}")
    if not destination.exists():
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            with open(fd, "wb", closefd=True) as handle:
                handle.write(source_bytes)
                handle.flush()
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return SourceSnapshot(str(path), digest, len(source_bytes), str(destination))


def load_input(
    source_path: str | Path,
    *,
    kind: InputKind | str | None = None,
    input_kind: InputKind | str | None = None,
    snapshot_dir: str | Path | None = None,
) -> InputView:
    """Load one producer scenario handoff and produce a source-pinned view."""

    path = Path(source_path)
    try:
        explicit_kind = _coerce_kind(kind) if kind is not None else None
        alternate_kind = _coerce_kind(input_kind) if input_kind is not None else None
    except ValueError as exc:
        raise InputSourceError(f"unsupported input kind: {kind or input_kind}") from exc
    if (
        explicit_kind is not None
        and alternate_kind is not None
        and explicit_kind != alternate_kind
    ):
        raise InputSourceError("kind and input_kind disagree")
    source_bytes = _read_source(path)
    source = (
        snapshot_input(path, snapshot_dir)
        if snapshot_dir is not None
        else SourceSnapshot(str(path), _sha256(source_bytes), len(source_bytes))
    )
    selected_kind = explicit_kind or alternate_kind or _infer_kind(path, source_bytes)
    if selected_kind is InputKind.SCENARIO_HANDOFF_V1:
        return _handoff_view(path, source_bytes, source)
    raise InputSourceError(f"unsupported input kind: {selected_kind}")


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
    for key in _HANDOFF_V2_FIELDS:
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


def _coerce_kind(value: InputKind | str) -> InputKind:
    return value if isinstance(value, InputKind) else InputKind(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_document(path: Path, source_bytes: bytes) -> Any:
    try:
        if path.suffix.lower() == ".json":
            return json.loads(source_bytes)
        return yaml.safe_load(source_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise InputSourceError(f"cannot parse input source {path}: {exc}") from exc


def _handoff_view(path: Path, source_bytes: bytes, source: SourceSnapshot) -> InputView:
    _validate_handoff_kit()
    payload = _parse_document(path, source_bytes)
    if not isinstance(payload, dict):
        raise InputSourceError("scenario handoff must be an object")
    _validate_handoff_payload(payload)
    schema_version = payload.get("schema_version", _HANDOFF_SCHEMA_VERSION)
    expected_digest = payload.get("content_digest", "")
    digest_payload = {key: value for key, value in payload.items() if key != "content_digest"}
    if expected_digest != _framed_digest(_HANDOFF_DIGEST_DOMAINS[schema_version], digest_payload):
        raise InputSourceError("scenario handoff content_digest does not match source")
    gherkin = payload["gherkin"]
    gherkin_bytes = _canonical_json(gherkin)
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
            "gherkin": _sha256(gherkin_bytes),
        },
        handoff_schema_version=schema_version,
    )


def _infer_kind(path: Path, source_bytes: bytes) -> InputKind:
    document = _parse_document(path, source_bytes)
    if isinstance(document, dict) and document.get("schema_version") in _HANDOFF_DIGEST_DOMAINS:
        return InputKind.SCENARIO_HANDOFF_V1
    raise InputSourceError(
        "authoring source must be a producer scenario-handoff-v1 or scenario-handoff-v2 document"
    )


def _validate_handoff_kit() -> None:
    lock_path = _HANDOFF_ROOT / "CONTRACT.lock"
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputSourceError(f"cannot read vendored handoff lock: {exc}") from exc
    if lock.get("authority") != "asago-scenario-generator":
        raise InputSourceError("vendored handoff kit authority mismatch")
    for relative, expected in lock.get("files", {}).items():
        member = _HANDOFF_ROOT / relative
        if not member.is_file() or _sha256(member.read_bytes()) != expected:
            raise InputSourceError(f"vendored handoff kit digest mismatch: {relative}")


def _validate_handoff_payload(payload: dict[str, Any]) -> None:
    schema_version = payload.get("schema_version", _HANDOFF_SCHEMA_VERSION)
    if schema_version not in _HANDOFF_DIGEST_DOMAINS:
        raise InputSourceError("unknown scenario handoff schema version")
    is_v2 = schema_version == _HANDOFF_SCHEMA_VERSION_V2
    required = {
        "scenario_id",
        "kind",
        "hypothesis_framing",
        "narrative",
        "attack_tree",
        "gherkin",
        "semantic_failure_criterion",
        "safe_alternative",
        "lineage",
    }
    allowed = {
        "schema_version",
        "scenario_id",
        "scenario_version",
        "kind",
        "hypothesis_framing",
        "semantic_failure_criterion",
        "safe_alternative",
        "governing_rules",
        "lineage",
        "documented_operations",
        "sourced_facts",
        "assumptions_and_unknowns",
        "observation",
        "safe_observable_outcome",
        "deduplication",
        "content_digest",
        "narrative",
        "attack_tree",
        "gherkin",
    }
    if is_v2:
        allowed.update(_HANDOFF_V2_FIELDS)
    missing = required - payload.keys()
    unknown = set(payload) - allowed
    if missing or unknown:
        raise InputSourceError(
            f"handoff schema invalid (missing={sorted(missing)}, unknown={sorted(unknown)})"
        )
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
    if payload.get("observation") is not None:
        _validate_observation_metadata(payload["observation"])
    if payload.get("safe_observable_outcome") is not None:
        _validate_safe_observable_outcome(payload["safe_observable_outcome"])
    if payload.get("deduplication") is not None:
        _validate_deduplication(payload["deduplication"], allow_condition=is_v2)
    if is_v2:
        _validate_v2_fields(payload)
    gherkin = payload["gherkin"]
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
    violations = _ownership_violations(payload)
    if violations:
        raise InputSourceError(f"handoff ownership violation: {', '.join(violations)}")


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
    for key in ("contract_schema", "contract_id", "contract_digest"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise InputSourceError(f"handoff observation field is blank or mistyped: {key}")
    if not re.fullmatch(r"[0-9a-f]{64}", value["contract_digest"]):
        raise InputSourceError("handoff observation contract_digest is not a SHA-256 hex digest")
    if value["contract_schema"] != "observation-contract-v1":
        raise InputSourceError("unknown observation contract schema")
    criteria = value["criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise InputSourceError("handoff observation criteria must be a non-empty list")
    criterion_keys = {
        "criterion_id",
        "outcome",
        "observable",
        "claim_level",
        "evidence",
        "operation_name",
        "reason",
    }
    required_criterion_keys = {
        "criterion_id",
        "outcome",
        "observable",
        "reason",
    }
    for criterion in criteria:
        if not isinstance(criterion, dict):
            raise InputSourceError("handoff observation criterion must be an object")
        if not required_criterion_keys <= set(criterion) or not set(criterion) <= criterion_keys:
            raise InputSourceError("handoff observation criterion fields are invalid")
        if not all(
            isinstance(criterion[key], str) and criterion[key].strip()
            for key in ("criterion_id", "outcome", "reason")
        ):
            raise InputSourceError("handoff observation criterion text is invalid")
        if not isinstance(criterion["observable"], bool):
            raise InputSourceError("handoff observation criterion observable is invalid")
        if criterion["observable"]:
            if (
                not isinstance(criterion.get("claim_level"), str)
                or not criterion["claim_level"].strip()
            ):
                raise InputSourceError("observable criterion requires claim_level")
            if not isinstance(criterion.get("evidence"), str) or not criterion["evidence"].strip():
                raise InputSourceError("observable criterion requires evidence")
        elif criterion.get("claim_level") is not None or criterion.get("evidence") is not None:
            raise InputSourceError("analytical-only criterion must omit claim_level and evidence")
        operation_name = criterion.get("operation_name")
        if operation_name is not None and (
            not isinstance(operation_name, str) or not operation_name.strip()
        ):
            raise InputSourceError("observation criterion operation_name is invalid")
        if not criterion["observable"] and operation_name is not None:
            raise InputSourceError("analytical-only criterion must omit operation_name")
    assessment = value["assessment"]
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
        if not isinstance(assessment[key], list) or not all(
            isinstance(item, str) and item.strip() for item in assessment[key]
        ):
            raise InputSourceError(f"handoff observation assessment {key} is invalid")


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
    claim_level = value.get("claim_level")
    evidence = value.get("evidence")
    operation_name = value.get("operation_name")
    if value["observable"]:
        expected_evidence = {
            "command_attempt": "tool_call",
            "reply": "assistant_message",
            "returned_result": "tool_result",
            "state_effect": "snapshot",
        }
        if claim_level not in expected_evidence or evidence != expected_evidence[claim_level]:
            raise InputSourceError("handoff safe outcome claim_level and evidence do not match")
        if operation_name is not None and (
            not isinstance(operation_name, str) or not operation_name.strip()
        ):
            raise InputSourceError("handoff safe outcome operation_name is invalid")
    elif any(value.get(key) is not None for key in ("claim_level", "evidence", "operation_name")):
        raise InputSourceError(
            "analytical-only handoff safe outcome must omit claim and operation"
        )
    for key in ("record_refs", "fact_refs"):
        refs = value.get(key)
        if not isinstance(refs, list) or not all(
            isinstance(item, str) and item.strip() for item in refs
        ):
            raise InputSourceError(f"handoff safe outcome {key} is invalid")
        if len(refs) != len(set(refs)):
            raise InputSourceError(f"handoff safe outcome {key} must be unique")
    if not value["observable"] and any(value.get(key) for key in ("record_refs", "fact_refs")):
        raise InputSourceError(
            "analytical-only handoff safe outcome must omit record and fact references"
        )


def _validate_v2_fields(payload: dict[str, Any]) -> None:
    """Validate the v2 condition fields against the vendored producer schema."""

    schema_path = _HANDOFF_ROOT / "handoff-v2" / "schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputSourceError(f"cannot read vendored handoff-v2 schema: {exc}") from exc
    violations = []
    for key in _HANDOFF_V2_FIELDS:
        validator = Draft202012Validator({"$defs": schema["$defs"], **schema["properties"][key]})
        if not validator.is_valid(payload.get(key)):
            violations.append(f"schema_violation:{key}")
    if violations:
        raise InputSourceError(f"handoff schema invalid: {', '.join(violations)}")


def _validate_deduplication(value: Any, *, allow_condition: bool = False) -> None:
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
    duplicate_of = value.get("duplicate_of")
    if status == "duplicate":
        if not isinstance(duplicate_of, str) or not duplicate_of.strip():
            raise InputSourceError("duplicate handoff requires duplicate_of")
    elif duplicate_of is not None:
        raise InputSourceError("canonical and analytical-only handoffs must omit duplicate_of")
    key = value["key"]
    key_required = {"uca_id", "control_action_id", "claim_level"}
    key_optional = {"operation_name", "condition"} if allow_condition else {"operation_name"}
    # The producer omits null fields, so a key without an operation has no
    # operation_name entry.
    if (
        not isinstance(key, dict)
        or not key_required <= set(key)
        or not set(key) <= key_required | key_optional
    ):
        raise InputSourceError("handoff deduplication key is invalid")
    key = {"operation_name": None, "condition": None, **key}
    if key["condition"] is not None and (
        not isinstance(key["condition"], str) or not key["condition"].strip()
    ):
        raise InputSourceError("handoff deduplication condition is invalid")
    if not all(
        isinstance(key[field], str) and key[field].strip()
        for field in ("uca_id", "control_action_id")
    ):
        raise InputSourceError("handoff deduplication key identity is invalid")
    if key["operation_name"] is not None and (
        not isinstance(key["operation_name"], str) or not key["operation_name"].strip()
    ):
        raise InputSourceError("handoff deduplication operation_name is invalid")
    if key["claim_level"] not in {
        "command_attempt",
        "reply",
        "returned_result",
        "state_effect",
        "unknown",
    }:
        raise InputSourceError("handoff deduplication claim_level is invalid")


def _ownership_violations(payload: dict[str, Any]) -> list[str]:
    forbidden = {
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
    patterns = (
        ("prepared_message_field", re.compile(r"prepared_user_text", re.I)),
        ("judge_prompt", re.compile(r"\bjudge prompt\b", re.I)),
        (
            "detector_expression",
            re.compile(r"\bgreater_than\b|\bless_than\b|\bamount\s*[<>=]", re.I),
        ),
        (
            "ready_to_send_instruction",
            re.compile(r"\bsend this message\b|\bexecute the following\b", re.I),
        ),
    )
    found: list[str] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                leaf = str(key).lower()
                if leaf in forbidden and f"artifact_design_field:{leaf}" not in found:
                    found.append(f"artifact_design_field:{leaf}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, str):
            for slug, pattern in patterns:
                code = f"prose_hiding:{slug}"
                if pattern.search(value) and code not in found:
                    found.append(code)

    walk(payload)
    return found


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        _normalize_unicode(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _normalize_unicode(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", str(key)): _normalize_unicode(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_unicode(item) for item in value]
    return value


def _framed_digest(domain: str, value: Any) -> str:
    return _sha256(domain.encode("utf-8") + b"\0" + _canonical_json(value))


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
    "SourceSnapshot",
    "load_input",
    "snapshot_input",
]
