"""Strict, offline loader for the producer's STPA execution bundle.

The loader is intentionally independent of the legacy YAML extractor and of
all platform modules.  It reads one canonical JSON index, verifies the index
and its paired canonical files, and only then creates immutable inward models.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from ..models._base import canonical_json_bytes, compute_framed_digest, freeze_value, sha256_bytes
from ..models.execution_classification import (
    BindingCompleteness,
    EnvironmentBasis,
    ExecutionClaimScope,
    ExecutionClassification,
    ExecutionContractDisposition,
    ExecutionProfileFit,
    SemanticExecutionContract,
)
from ..models.execution_intent import CONDITION_TYPES, OPERATORS, ExecutionIntent

BUNDLE_SCHEMA_VERSION = "stpa-execution-bundle-v1"
PROJECTION_SCHEMA_VERSION = "stpa-execution-projection-v2"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_FACTOR_REFERENCE = re.compile(r"^(?:PM|FB|CA)-\d+(?:-\d+)?$")
_OUTCOME_ID_RE = re.compile(r"^OUTCOME-[A-Za-z0-9._-]+$")

_BUNDLE_FIELDS = frozenset({"schema_version", "run_id", "producer", "entries", "bundle_digest"})
_PRODUCER_FIELDS = frozenset({"name", "version"})
_ENTRY_FIELDS = frozenset(
    {
        "scenario_id",
        "candidate_id",
        "ica_slot_id",
        "ica_id",
        "scenario",
        "projection",
        "validation",
    }
)
_FILE_REF_FIELDS = frozenset({"path", "content_sha256"})
_PROJECTION_REF_FIELDS = frozenset({"path", "schema_version", "content_sha256", "semantic_digest"})
_PROJECTION_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "scenario_id",
        "candidate_id",
        "ica_slot_id",
        "ica_id",
        "controller_id",
        "control_action_id",
        "uca_type",
        "causal_factors",
        "steps",
        "unsafe_outcome",
        "stimulus_requirements",
        "execution_requirements",
        "execution_contract",
        "execution_classification",
        "trace_refs",
        "semantic_digest",
    }
)
_FACTOR_FIELDS = frozenset(
    {
        "factor_id",
        "order",
        "kind",
        "structural_source_id",
        "description",
        "evidence_status",
        "capability_refs",
        "access_refs",
        "bounded_assumption",
        "temporal_condition",
    }
)
_STEP_FIELDS = frozenset({"step_id", "order", "kind", "factor_id", "structural_source_id"})
_OUTCOME_FIELDS = frozenset(
    {
        "outcome_id",
        "control_action_id",
        "uca_type",
        "condition",
        "semantic_binding_required",
        "hazard_refs",
        "constraint_refs",
    }
)
_REQUIREMENT_FIELDS = frozenset(
    {
        "requires_multi_turn",
        "requires_tool_execution",
        "requires_persistent_state",
        "requires_multi_agent",
        "requires_real_clock",
        "requires_state_observation",
        "required_surface_categories",
    }
)
_TRACE_FIELDS = frozenset(
    {
        "obligation_ids",
        "risk_ids",
        "attack_pattern_ids",
        "technique_ids",
        "loss_ids",
        "hazard_ids",
        "constraint_ids",
        "source_pins",
    }
)
_SOURCE_PIN_FIELDS = frozenset(
    {"control_structure", "loss_analysis", "ica_enumeration", "scenario_context"}
)
_PLACEHOLDER_FIELDS = frozenset({"binding_ref", "value_type", "description", "minimum", "maximum"})
_PLACEHOLDER_REQUIRED_FIELDS = frozenset({"binding_ref", "value_type", "description"})
_CONDITION_FIELDS = {
    "ordering": {"type", "reference_step_id", "relation"},
    "delay": {"type", "reference_ref", "delay_ms"},
    "duration": {"type", "reference_ref", "duration_ms"},
    "window": {"type", "reference_ref", "window_from_ms", "window_to_ms"},
    "absence": {"type", "reference_ref", "until_step_id"},
    "action_presence": {"type", "control_action_id", "expected"},
    "action_value": {"type", "control_action_id", "property", "operator", "expected"},
    "state_value": {"type", "subject_ref", "property", "operator", "expected"},
}
_CONDITION_REQUIRED_FIELDS = {
    condition_type: fields - {"type"} for condition_type, fields in _CONDITION_FIELDS.items()
}
_CONDITION_STRING_FIELDS = frozenset(
    {
        "reference_step_id",
        "reference_ref",
        "until_step_id",
        "control_action_id",
        "property",
        "subject_ref",
    }
)
_UCA_CONDITION_TYPES = {
    "NOT_PROVIDED": {"action_presence"},
    "INCORRECT": {"action_value", "state_value"},
    "WRONG_TIMING": {"ordering", "delay", "window", "absence"},
    "WRONG_DURATION": {"duration"},
}
_FACTOR_KINDS = frozenset(
    {"PROCESS_MODEL_FLAW", "FEEDBACK_DELAY", "SENSOR_ANOMALY", "ACTUATOR_ANOMALY"}
)
_FACTOR_NAMESPACES = {
    "PROCESS_MODEL_FLAW": "PM",
    "FEEDBACK_DELAY": "FB",
    "SENSOR_ANOMALY": "FB",
    "ACTUATOR_ANOMALY": "CA",
}
_EVIDENCE_STATUSES = frozenset(
    {"structural_failure", "reachable_capability", "bounded_assumption"}
)
_STRUCTURAL_REFERENCE = re.compile(r"^(?:PM|FB|CA|CM)-\d+(?:-\d+)?$|^S-\d+$")
_ACTION_REFERENCE = re.compile(r"^(?:CA|CM)-\d+(?:-\d+)?$")
_SURFACE_CATEGORY_VALUES = {
    "external_input",
    "system_instruction",
    "tool_result",
    "tool_definition",
    "persistent_data",
    "agent_message",
    "environment_event",
}
_CONDITION_REFERENCE_RULES = {
    "ordering": (("reference_step_id", re.compile(r"^S-\d+$")),),
    "absence": (("until_step_id", re.compile(r"^S-\d+$")),),
    "delay": (("reference_ref", _STRUCTURAL_REFERENCE),),
    "duration": (("reference_ref", _STRUCTURAL_REFERENCE),),
    "window": (("reference_ref", _STRUCTURAL_REFERENCE),),
    "action_presence": (("control_action_id", _ACTION_REFERENCE),),
    "action_value": (("control_action_id", _ACTION_REFERENCE),),
    "state_value": (("subject_ref", _STRUCTURAL_REFERENCE),),
}
_FORBIDDEN_KEYS = frozenset(
    {
        "observation",
        "observations",
        "observation_receipt",
        "runtime_observation",
        "execution_id",
        "receipt_digest",
        "artifact_digest",
        "binding_set_id",
        "detector",
        "detector_prompt",
        "platform",
        "platform_role",
        "surface",
        "locator",
        "tool_name",
        "tool_schema",
        "endpoint",
        "provider",
        "clock_binding",
    }
)


@dataclass(frozen=True, slots=True)
class ValidationViolation:
    """One deterministic closed-contract violation."""

    code: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.detail}"


class BundleValidationError(ValueError):
    """Raised when a bundle index or one of its paired files is invalid."""

    def __init__(self, violations: list[ValidationViolation] | tuple[ValidationViolation, ...]):
        self.violations = tuple(violations)
        message = "; ".join(str(item) for item in self.violations)
        super().__init__(message or "execution bundle validation failed")


@dataclass(frozen=True, slots=True)
class LoaderLimits:
    """Conservative resource limits for one bundle load."""

    max_index_bytes: int = 4 * 1024 * 1024
    max_entry_count: int = 1000
    max_scenario_bytes: int = 8 * 1024 * 1024
    max_projection_bytes: int = 8 * 1024 * 1024
    max_json_depth: int = 64
    max_total_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_index_bytes",
            "max_entry_count",
            "max_scenario_bytes",
            "max_projection_bytes",
            "max_json_depth",
            "max_total_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class VerifiedExecutionBundleEntry:
    """One verified scenario/projection pair and its immutable intent."""

    scenario_id: str
    candidate_id: str
    ica_slot_id: str
    ica_id: str
    scenario_path: Path
    projection_path: Path
    scenario_content_sha256: str
    projection_content_sha256: str
    projection_semantic_digest: str
    scenario_document: Mapping[str, Any]
    projection_document: Mapping[str, Any]
    intent: ExecutionIntent
    scenario_bytes: bytes
    projection_bytes: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_document", freeze_value(self.scenario_document))
        object.__setattr__(self, "projection_document", freeze_value(self.projection_document))


@dataclass(frozen=True, slots=True)
class VerifiedExecutionBundle:
    """Verified run-level index containing one or more execution intents."""

    index_path: Path
    schema_version: str
    run_id: str
    producer: Mapping[str, str]
    bundle_digest: str
    entries: tuple[VerifiedExecutionBundleEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "producer", freeze_value(self.producer))

    @property
    def intents(self) -> tuple[ExecutionIntent, ...]:
        """Return the immutable intents in canonical index order."""

        return tuple(entry.intent for entry in self.entries)

    @property
    def intent(self) -> ExecutionIntent:
        """Return the sole intent for convenience on single-entry bundles."""

        if len(self.entries) != 1:
            raise ValueError("bundle contains more than one execution intent")
        return self.entries[0].intent


def _violation(code: str, path: str, detail: str) -> ValidationViolation:
    return ValidationViolation(code=code, path=path, detail=detail)


def _has_exact_fields(
    value: Any,
    expected: frozenset[str],
    path: str,
    violations: list[ValidationViolation],
) -> bool:
    if not isinstance(value, dict):
        violations.append(_violation("container_type_mismatch", path, "expected a JSON object"))
        return False
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    for key in missing:
        violations.append(
            _violation("required_field_missing", f"{path}.{key}", "required field is missing")
        )
    for key in unknown:
        violations.append(
            _violation("unexpected_field", f"{path}.{key}", "field is not in the closed contract")
        )
    return not missing and not unknown


def _string(
    value: Any,
    path: str,
    violations: list[ValidationViolation],
    *,
    nonempty: bool = True,
) -> bool:
    if not isinstance(value, str) or (nonempty and not value):
        violations.append(
            _violation("container_type_mismatch", path, "expected a non-empty string")
        )
        return False
    return True


def _digest(value: Any, path: str, violations: list[ValidationViolation]) -> bool:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        violations.append(
            _violation("condition_value_invalid", path, "expected lowercase hexadecimal SHA-256")
        )
        return False
    return True


def _strict_bool(value: Any, path: str, violations: list[ValidationViolation]) -> bool:
    if type(value) is not bool:
        violations.append(_violation("container_type_mismatch", path, "expected a JSON boolean"))
        return False
    return True


def _strict_integer(
    value: Any, path: str, violations: list[ValidationViolation], *, nonnegative: bool = False
) -> bool:
    if type(value) is not int:
        violations.append(_violation("container_type_mismatch", path, "expected an integer"))
        return False
    if nonnegative and value < 0:
        violations.append(
            _violation("condition_value_invalid", path, "integer must be non-negative")
        )
        return False
    return True


def _strings_array(value: Any, path: str, violations: list[ValidationViolation]) -> bool:
    if not isinstance(value, list):
        violations.append(_violation("container_type_mismatch", path, "expected an array"))
        return False
    valid = True
    for index, item in enumerate(value):
        valid = _string(item, f"{path}[{index}]", violations) and valid
    return valid


def _parse_json(raw: bytes, path: Path, limits: LoaderLimits) -> Any:
    violations: list[ValidationViolation] = []

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        violations.append(_violation("schema_parse_error", str(path), str(exc)))
        raise BundleValidationError(violations) from exc

    def depth(node: Any, current: int = 0) -> int:
        if isinstance(node, dict):
            return max([current, *(depth(item, current + 1) for item in node.values())])
        if isinstance(node, list):
            return max([current, *(depth(item, current + 1) for item in node)])
        return current

    if depth(value) > limits.max_json_depth:
        violations.append(
            _violation(
                "resource_limit_exceeded",
                str(path),
                f"JSON nesting exceeds {limits.max_json_depth}",
            )
        )
        raise BundleValidationError(violations)
    return value


def _canonical_bytes_violation(
    raw: bytes,
    value: Any,
    path: str,
) -> ValidationViolation | None:
    """Return a stable violation when persisted JSON is not canonical bytes."""

    try:
        expected = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        return _violation("schema_field_invalid", path, f"cannot canonicalize JSON: {exc}")
    if raw != expected:
        return _violation(
            "canonical_bytes_mismatch",
            path,
            "persisted JSON bytes differ from canonical JSON bytes",
        )
    return None


def _read_limited(path: Path, maximum: int, total: list[int], limits: LoaderLimits) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(maximum + 1)
    except OSError as exc:
        raise BundleValidationError(
            [_violation("bundle_path_invalid", str(path), str(exc))]
        ) from exc
    if len(data) > maximum:
        raise BundleValidationError(
            [_violation("resource_limit_exceeded", str(path), f"file exceeds {maximum} bytes")]
        )
    total[0] += len(data)
    if total[0] > limits.max_total_bytes:
        raise BundleValidationError(
            [_violation("resource_limit_exceeded", str(path), "bundle byte budget exceeded")]
        )
    return data


def _safe_relative_path(root: Path, raw: Any, path: str) -> Path:
    parts = _safe_path_parts(raw, path)
    candidate = root.joinpath(*parts)
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise BundleValidationError(
            [_violation("bundle_path_invalid", path, f"path escapes bundle directory: {exc}")]
        ) from exc
    return resolved


def _safe_path_parts(raw: Any, path: str) -> tuple[str, ...]:
    if not _nonempty_path(raw):
        _raise_path_error(path, "path must be a non-empty POSIX path")
    if not _posix_relative_path(raw):
        _raise_path_error(path, "absolute or non-POSIX path is forbidden")
    parts = tuple(raw.split("/"))
    if not _safe_path_components(parts):
        _raise_path_error(path, "empty, dot, or traversal path component is forbidden")
    return parts


def _nonempty_path(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _posix_relative_path(value: str) -> bool:
    return (
        "\\" not in value
        and not PurePosixPath(value).is_absolute()
        and not re.match(r"^[A-Za-z]:", value)
    )


def _safe_path_components(parts: tuple[str, ...]) -> bool:
    return all(part not in {"", ".", ".."} for part in parts)


def _raise_path_error(path: str, detail: str) -> None:
    raise BundleValidationError([_violation("bundle_path_invalid", path, detail)])


def _validate_file_ref(
    value: Any,
    path: str,
    violations: list[ValidationViolation],
    *,
    projection: bool,
) -> bool:
    expected = _PROJECTION_REF_FIELDS if projection else _FILE_REF_FIELDS
    if not _has_exact_fields(value, expected, path, violations):
        return False
    _string(value.get("path"), f"{path}.path", violations)
    _digest(value.get("content_sha256"), f"{path}.content_sha256", violations)
    if projection:
        if value.get("schema_version") != PROJECTION_SCHEMA_VERSION:
            violations.append(
                _violation(
                    "schema_version_mismatch",
                    f"{path}.schema_version",
                    "projection version is not supported",
                )
            )
        _digest(value.get("semantic_digest"), f"{path}.semantic_digest", violations)
    return True


def _validate_bundle_header(value: Any, violations: list[ValidationViolation]) -> bool:
    if not _has_exact_fields(value, _BUNDLE_FIELDS, "$", violations):
        return False
    if value.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        violations.append(
            _violation(
                "schema_version_mismatch", "$.schema_version", "bundle version is not supported"
            )
        )
    _string(value.get("run_id"), "$.run_id", violations)
    producer = value.get("producer")
    if _has_exact_fields(producer, _PRODUCER_FIELDS, "$.producer", violations):
        _string(producer.get("name"), "$.producer.name", violations)
        _string(producer.get("version"), "$.producer.version", violations)
    return True


def _validate_entry_validation(
    value: Any, path: str, violations: list[ValidationViolation]
) -> None:
    if not isinstance(value, dict) or set(value) != {"status", "validator_version"}:
        violations.append(
            _violation(
                "unexpected_field",
                path,
                "validation must contain only status and validator_version",
            )
        )
    elif value.get("status") != "valid":
        violations.append(
            _violation("schema_field_invalid", f"{path}.status", "status must be valid")
        )
    elif value.get("validator_version") != PROJECTION_SCHEMA_VERSION:
        violations.append(
            _violation(
                "schema_version_mismatch",
                f"{path}.validator_version",
                "validator version must equal the projection contract",
            )
        )


def _validate_bundle_entry(entry: Any, index: int, violations: list[ValidationViolation]) -> None:
    entry_path = f"$.entries[{index}]"
    if not _has_exact_fields(entry, _ENTRY_FIELDS, entry_path, violations):
        return
    for key in ("scenario_id", "candidate_id", "ica_slot_id", "ica_id"):
        _string(entry.get(key), f"{entry_path}.{key}", violations)
    _validate_file_ref(
        entry.get("scenario"), f"{entry_path}.scenario", violations, projection=False
    )
    _validate_file_ref(
        entry.get("projection"), f"{entry_path}.projection", violations, projection=True
    )
    _validate_entry_validation(entry.get("validation"), f"{entry_path}.validation", violations)


def _entry_identity(entry: Any) -> tuple[Any, ...]:
    if not isinstance(entry, dict):
        return ()
    return tuple(
        entry.get(key) for key in ("scenario_id", "candidate_id", "ica_slot_id", "ica_id")
    )


def _validate_entry_order(entries: list[Any], violations: list[ValidationViolation]) -> None:
    identities = [_entry_identity(entry) for entry in entries]
    ordered = sorted(identities, key=lambda item: tuple(str(part) for part in item))
    if identities != ordered:
        violations.append(
            _violation(
                "schema_field_invalid", "$.entries", "entries are not in canonical identity order"
            )
        )
    if len(identities) != len(set(identities)):
        violations.append(
            _violation("identity_mismatch", "$.entries", "entry identity tuples must be unique")
        )


def _validate_bundle_digest(value: dict[str, Any], violations: list[ValidationViolation]) -> None:
    if not _digest(value.get("bundle_digest"), "$.bundle_digest", violations) or violations:
        return
    expected = _compute_digest(value, BUNDLE_SCHEMA_VERSION, "$.bundle_digest", violations)
    if expected is None:
        return
    if value.get("bundle_digest") != expected:
        violations.append(
            _violation(
                "semantic_digest_mismatch",
                "$.bundle_digest",
                "bundle digest does not match canonical index",
            )
        )


def _compute_digest(
    value: dict[str, Any], domain: str, path: str, violations: list[ValidationViolation]
) -> str | None:
    try:
        return compute_framed_digest(
            domain,
            {
                key: item
                for key, item in value.items()
                if key not in {"bundle_digest", "semantic_digest"}
            },
        )
    except (TypeError, ValueError) as exc:
        violations.append(_violation("schema_field_invalid", path, str(exc)))
        return None


def _validate_bundle_index(value: Any) -> list[ValidationViolation]:
    violations: list[ValidationViolation] = []
    if not _validate_bundle_header(value, violations):
        return violations
    entries = value.get("entries")
    if not isinstance(entries, list):
        violations.append(_violation("container_type_mismatch", "$.entries", "expected an array"))
        return violations
    for index, entry in enumerate(entries):
        _validate_bundle_entry(entry, index, violations)
    _validate_entry_order(entries, violations)
    _validate_bundle_digest(value, violations)
    return violations


def _validate_placeholder_shape(
    value: Any, path: str, violations: list[ValidationViolation]
) -> bool:
    if not isinstance(value, dict):
        return False
    missing = sorted(_PLACEHOLDER_REQUIRED_FIELDS - value.keys())
    unknown = sorted(value.keys() - _PLACEHOLDER_FIELDS)
    for key in missing:
        violations.append(
            _violation("required_field_missing", f"{path}.{key}", "required field is missing")
        )
    for key in unknown:
        violations.append(
            _violation("unexpected_field", f"{path}.{key}", "field is not in the closed contract")
        )
    return not missing and not unknown


def _validate_placeholder_type(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> None:
    _string(value.get("binding_ref"), f"{path}.binding_ref", violations)
    value_type = value.get("value_type")
    if value_type not in {"string", "integer", "number", "boolean"}:
        violations.append(
            _violation(
                "condition_value_invalid", f"{path}.value_type", "unsupported placeholder type"
            )
        )
    _string(value.get("description"), f"{path}.description", violations)
    _validate_placeholder_type_bounds(value, value_type, path, violations)


def _validate_placeholder_type_bounds(
    value: dict[str, Any], value_type: Any, path: str, violations: list[ValidationViolation]
) -> None:
    if value_type == "integer":
        _validate_integer_placeholder_bounds(value, path, violations)
    elif value_type in {"string", "boolean"}:
        _validate_non_numeric_placeholder_bounds(value, path, violations)


def _validate_integer_placeholder_bounds(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> None:
    for field in ("minimum", "maximum"):
        bound = value.get(field)
        if bound is not None and type(bound) is not int:
            violations.append(
                _violation(
                    "condition_value_invalid",
                    f"{path}.{field}",
                    "integer placeholder bounds must be integers",
                )
            )


def _validate_non_numeric_placeholder_bounds(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> None:
    if any(value.get(field) is not None for field in ("minimum", "maximum")):
        violations.append(
            _violation(
                "condition_value_invalid",
                path,
                "string and boolean placeholders cannot declare numeric bounds",
            )
        )


def _validate_placeholder_bounds(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> None:
    for field in ("minimum", "maximum"):
        _validate_one_placeholder_bound(value.get(field), f"{path}.{field}", violations)
    minimum = value.get("minimum")
    maximum = value.get("maximum")
    _validate_placeholder_bound_order(minimum, maximum, path, violations)


def _validate_one_placeholder_bound(
    bound: Any, path: str, violations: list[ValidationViolation]
) -> None:
    if bound is None:
        return
    if not _valid_numeric_bound(bound):
        violations.append(
            _violation("condition_value_invalid", path, "bound must be a non-negative number")
        )
        return
    if isinstance(bound, float) and not math.isfinite(bound):
        violations.append(_violation("condition_value_invalid", path, "bound must be finite"))


def _valid_numeric_bound(value: Any) -> bool:
    return type(value) in {int, float} and value >= 0


def _validate_placeholder_bound_order(
    minimum: Any, maximum: Any, path: str, violations: list[ValidationViolation]
) -> None:
    if minimum is not None and maximum is not None and minimum > maximum:
        violations.append(_violation("condition_value_invalid", path, "minimum exceeds maximum"))


def _validate_placeholder(value: Any, path: str, violations: list[ValidationViolation]) -> bool:
    if not _validate_placeholder_shape(value, path, violations):
        return False
    _validate_placeholder_type(value, path, violations)
    _validate_placeholder_bounds(value, path, violations)
    return True


def _scalar_or_placeholder(value: Any, path: str, violations: list[ValidationViolation]) -> bool:
    if isinstance(value, dict):
        return _validate_placeholder(value, path, violations)
    if type(value) in {str, int, float, bool}:
        return True
    violations.append(
        _violation(
            "condition_value_invalid", path, "condition values must be scalar or typed placeholder"
        )
    )
    return False


def _validate_quantity(value: Any, path: str, violations: list[ValidationViolation]) -> None:
    if isinstance(value, dict):
        if _validate_placeholder(value, path, violations) and value.get("value_type") not in {
            "integer",
            "number",
        }:
            violations.append(
                _violation(
                    "condition_value_invalid",
                    f"{path}.value_type",
                    "time placeholders must be numeric",
                )
            )
    elif not _strict_integer(value, path, violations, nonnegative=True):
        return


def _condition_type(value: Any, path: str, violations: list[ValidationViolation]) -> str | None:
    if not isinstance(value, dict):
        violations.append(
            _violation("container_type_mismatch", path, "condition must be an object")
        )
        return None
    condition_type = value.get("type")
    if condition_type not in CONDITION_TYPES:
        violations.append(
            _violation(
                "condition_type_mismatch", f"{path}.type", "unknown semantic condition type"
            )
        )
        return None
    return condition_type


def _validate_condition_shape(
    value: dict[str, Any], condition_type: str, path: str, violations: list[ValidationViolation]
) -> None:
    allowed = _CONDITION_FIELDS[condition_type]
    required = _CONDITION_REQUIRED_FIELDS[condition_type]
    unknown = sorted(set(value) - allowed)
    for key in unknown:
        violations.append(
            _violation(
                "unexpected_field",
                f"{path}.{key}",
                "field is not admitted by this condition variant",
            )
        )
    for key in sorted(required - value.keys()):
        violations.append(
            _violation("required_field_missing", f"{path}.{key}", "condition field is required")
        )


def _validate_condition_references(
    value: dict[str, Any], condition_type: str, path: str, violations: list[ValidationViolation]
) -> None:
    for field in _CONDITION_STRING_FIELDS & set(value):
        _string(value[field], f"{path}.{field}", violations)
    for field, pattern in _CONDITION_REFERENCE_RULES.get(condition_type, ()):
        reference = value.get(field)
        if isinstance(reference, str):
            _validate_reference_pattern(reference, pattern, path, field, violations)


def _validate_reference_pattern(
    value: str,
    pattern: str | re.Pattern[str],
    path: str,
    field: str,
    violations: list[ValidationViolation],
) -> None:
    matcher = re.compile(pattern) if isinstance(pattern, str) else pattern
    if matcher.fullmatch(value) is None:
        violations.append(
            _violation(
                "condition_reference_invalid",
                f"{path}.{field}",
                "reference does not match the producer condition contract",
            )
        )


def _validate_condition_values(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> set[str]:
    placeholders = _validate_condition_quantities(value, path, violations)
    placeholders.update(_validate_condition_expected(value, path, violations))
    _validate_condition_order(value, path, violations)
    return placeholders


def _validate_condition_quantities(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> set[str]:
    placeholders: set[str] = set()
    for field in ("delay_ms", "duration_ms", "window_from_ms", "window_to_ms"):
        if field in value:
            _validate_quantity(value[field], f"{path}.{field}", violations)
            _add_placeholder_ref(placeholders, value[field])
    return placeholders


def _validate_condition_expected(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> set[str]:
    if "expected" not in value:
        return set()
    _scalar_or_placeholder(value["expected"], f"{path}.expected", violations)
    refs: set[str] = set()
    _add_placeholder_ref(refs, value["expected"])
    return refs


def _add_placeholder_ref(refs: set[str], value: Any) -> None:
    if isinstance(value, dict) and isinstance(value.get("binding_ref"), str):
        refs.add(value["binding_ref"])


def _validate_condition_order(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> None:
    _validate_relation(value, path, violations)
    _validate_operator(value, path, violations)
    _validate_action_presence_expected(value, path, violations)
    _validate_window_order(value, path, violations)


def _validate_relation(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> None:
    if value.get("relation") not in {None, "before", "after"}:
        violations.append(
            _violation("condition_field_mismatch", f"{path}.relation", "relation is not supported")
        )


def _validate_operator(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> None:
    if value.get("operator") not in {None, *OPERATORS}:
        violations.append(
            _violation("condition_field_mismatch", f"{path}.operator", "operator is not supported")
        )


def _validate_action_presence_expected(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> None:
    if value.get("type") == "action_presence" and value.get("expected") != "not_provided":
        violations.append(
            _violation(
                "condition_field_mismatch",
                f"{path}.expected",
                "action_presence expects not_provided",
            )
        )


def _validate_window_order(
    value: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> None:
    lower = value.get("window_from_ms")
    upper = value.get("window_to_ms")
    if isinstance(lower, int) and isinstance(upper, int) and lower > upper:
        violations.append(
            _violation("condition_value_invalid", path, "window lower bound exceeds upper bound")
        )


def _validate_condition(value: Any, path: str, violations: list[ValidationViolation]) -> set[str]:
    condition_type = _condition_type(value, path, violations)
    if condition_type is None:
        return set()
    _validate_condition_shape(value, condition_type, path, violations)
    _validate_condition_references(value, condition_type, path, violations)
    return _validate_condition_values(value, path, violations)


def _validate_projection_header(value: Any, violations: list[ValidationViolation]) -> bool:
    if not _has_exact_fields(value, _PROJECTION_FIELDS, "$projection", violations):
        return False
    if value.get("schema_version") != PROJECTION_SCHEMA_VERSION:
        violations.append(
            _violation(
                "schema_version_mismatch",
                "$.projection.schema_version",
                "projection version is not supported",
            )
        )
    for key in (
        "run_id",
        "scenario_id",
        "candidate_id",
        "ica_slot_id",
        "ica_id",
        "controller_id",
        "control_action_id",
        "uca_type",
    ):
        _string(value.get(key), f"$.projection.{key}", violations)
    if value.get("uca_type") not in _UCA_CONDITION_TYPES:
        violations.append(
            _violation("identity_mismatch", "$.projection.uca_type", "unknown UCA type")
        )
    return True


def _validate_identity_value(
    actual: Any,
    expected: str,
    path: str,
    detail: str,
    violations: list[ValidationViolation],
) -> None:
    if actual != expected:
        violations.append(_violation("identity_mismatch", path, detail))


def _validate_projection_identity(
    value: dict[str, Any], violations: list[ValidationViolation]
) -> None:
    fields = ("controller_id", "control_action_id", "uca_type")
    if not all(isinstance(value.get(key), str) and value.get(key) for key in fields):
        return
    controller, action, uca_type = (value[key] for key in fields)
    expected_slot = f"{controller}:{action}:{uca_type}"
    _validate_projection_candidate(value, controller, action, uca_type, violations)
    _validate_projection_slot(value, expected_slot, violations)
    _validate_projection_ica(value, expected_slot, violations)


def _validate_projection_candidate(
    value: dict[str, Any],
    controller: str,
    action: str,
    uca_type: str,
    violations: list[ValidationViolation],
) -> None:
    expected = f"EXEC:{controller}:{action}:{uca_type}"
    _validate_identity_value(
        value.get("candidate_id"),
        expected,
        "$.projection.candidate_id",
        "candidate identity is not canonical",
        violations,
    )


def _validate_projection_slot(
    value: dict[str, Any], expected_slot: str, violations: list[ValidationViolation]
) -> None:
    _validate_identity_value(
        value.get("ica_slot_id"),
        expected_slot,
        "$.projection.ica_slot_id",
        "ICA slot identity is not canonical",
        violations,
    )


def _validate_projection_ica(
    value: dict[str, Any], expected_slot: str, violations: list[ValidationViolation]
) -> None:
    ica_id = value.get("ica_id")
    if isinstance(ica_id, str) and not ica_id.startswith(f"{expected_slot}:"):
        violations.append(
            _violation(
                "identity_mismatch", "$.projection.ica_id", "ICA does not belong to its slot"
            )
        )


def _validate_factor_order(
    factor: dict[str, Any], index: int, path: str, violations: list[ValidationViolation]
) -> None:
    if factor.get("factor_id") != f"CF-{index}" or factor.get("order") != index:
        violations.append(
            _violation("factor_order_mismatch", path, "factor IDs and order must be contiguous")
        )


def _validate_factor_fields(
    factor: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> None:
    for key in ("factor_id", "structural_source_id", "description"):
        _string(factor.get(key), f"{path}.{key}", violations)
    _validate_factor_kind(factor.get("kind"), path, violations)
    _validate_factor_source(
        factor.get("kind"), factor.get("structural_source_id"), path, violations
    )
    for key in ("capability_refs", "access_refs"):
        _strings_array(factor.get(key), f"{path}.{key}", violations)
    if factor.get("bounded_assumption") is not None:
        _string(factor.get("bounded_assumption"), f"{path}.bounded_assumption", violations)
    _validate_factor_evidence(factor, path, violations)


def _validate_factor_kind(kind: Any, path: str, violations: list[ValidationViolation]) -> None:
    if kind not in _FACTOR_KINDS:
        violations.append(
            _violation("schema_field_invalid", f"{path}.kind", "unknown causal-factor kind")
        )


def _validate_factor_source(
    kind: Any, source: Any, path: str, violations: list[ValidationViolation]
) -> None:
    if not isinstance(source, str):
        return
    namespace = _FACTOR_NAMESPACES.get(kind)
    if not _FACTOR_REFERENCE.fullmatch(source) or (
        namespace is not None and not source.startswith(f"{namespace}-")
    ):
        violations.append(
            _violation(
                "factor_reference_mismatch",
                f"{path}.structural_source_id",
                "structural source does not use the namespace required by factor kind",
            )
        )


def _validate_factor_evidence(
    factor: dict[str, Any], path: str, violations: list[ValidationViolation]
) -> None:
    status = factor.get("evidence_status")
    if not _validate_evidence_status(status, path, violations):
        return
    _validate_capability_evidence(factor, status, path, violations)
    _validate_assumption_evidence(factor, status, path, violations)


def _validate_evidence_status(
    status: Any, path: str, violations: list[ValidationViolation]
) -> bool:
    if status in _EVIDENCE_STATUSES:
        return True
    violations.append(
        _violation("schema_field_invalid", f"{path}.evidence_status", "unknown evidence status")
    )
    return False


def _validate_capability_evidence(
    factor: dict[str, Any], status: str, path: str, violations: list[ValidationViolation]
) -> None:
    capabilities = factor.get("capability_refs")
    accesses = factor.get("access_refs")
    if status == "reachable_capability":
        if not capabilities:
            violations.append(
                _violation(
                    "schema_field_invalid",
                    f"{path}.capability_refs",
                    "reachable_capability evidence requires capability refs",
                )
            )
        if not accesses:
            violations.append(
                _violation(
                    "schema_field_invalid",
                    f"{path}.access_refs",
                    "reachable_capability evidence requires access refs",
                )
            )
    elif capabilities or accesses:
        violations.append(
            _violation(
                "schema_field_invalid",
                path,
                "capability and access refs require reachable_capability evidence",
            )
        )


def _validate_assumption_evidence(
    factor: dict[str, Any], status: str, path: str, violations: list[ValidationViolation]
) -> None:
    assumption = factor.get("bounded_assumption")
    if status == "bounded_assumption":
        if not isinstance(assumption, str) or not assumption.strip():
            violations.append(
                _violation(
                    "schema_field_invalid",
                    f"{path}.bounded_assumption",
                    "bounded_assumption evidence requires text",
                )
            )
    elif assumption is not None:
        violations.append(
            _violation(
                "schema_field_invalid",
                f"{path}.bounded_assumption",
                "bounded_assumption text requires bounded_assumption evidence",
            )
        )


def _validate_factor(
    factor: Any, index: int, violations: list[ValidationViolation]
) -> tuple[str | None, set[str]]:
    path = f"$projection.causal_factors[{index - 1}]"
    if not _has_exact_fields(factor, _FACTOR_FIELDS, path, violations):
        return None, set()
    _validate_factor_order(factor, index, path, violations)
    _validate_factor_fields(factor, path, violations)
    condition = factor.get("temporal_condition")
    refs = (
        _validate_condition(condition, f"{path}.temporal_condition", violations)
        if condition is not None
        else set()
    )
    source = (
        factor.get("structural_source_id")
        if isinstance(factor.get("structural_source_id"), str)
        else None
    )
    return source, refs


def _validate_projection_factors(
    value: dict[str, Any], violations: list[ValidationViolation]
) -> tuple[list[str | None], set[str], list[Any] | None]:
    factors = value.get("causal_factors")
    if not isinstance(factors, list) or not factors:
        violations.append(
            _violation(
                "required_field_missing",
                "$.projection.causal_factors",
                "published v2 projection requires factors",
            )
        )
        return [], set(), None
    sources: list[str | None] = []
    refs: set[str] = set()
    for index, factor in enumerate(factors, start=1):
        source, factor_refs = _validate_factor(factor, index, violations)
        sources.append(source)
        refs.update(factor_refs)
    return sources, refs, factors


def _validate_step_fields(
    step: dict[str, Any], index: int, path: str, violations: list[ValidationViolation]
) -> None:
    if step.get("step_id") != f"S-{index}" or step.get("order") != index:
        violations.append(
            _violation("step_mapping_mismatch", path, "step IDs and order must be contiguous")
        )
    if step.get("kind") not in {"CAUSAL_FACTOR", "UNSAFE_CONTROL_ACTION"}:
        violations.append(_violation("step_mapping_mismatch", f"{path}.kind", "unknown step kind"))
    if step.get("factor_id") is not None:
        _string(step.get("factor_id"), f"{path}.factor_id", violations)
    _string(step.get("structural_source_id"), f"{path}.structural_source_id", violations)


def _validate_step_mapping(
    step: dict[str, Any],
    index: int,
    source: str | None,
    path: str,
    violations: list[ValidationViolation],
) -> None:
    if source is None:
        return
    if step.get("kind") != "CAUSAL_FACTOR" or step.get("factor_id") != f"CF-{index}":
        violations.append(
            _violation("step_mapping_mismatch", path, "causal step does not map to its factor")
        )
    if source is not None and step.get("structural_source_id") != source:
        violations.append(
            _violation(
                "factor_reference_mismatch", path, "causal step source does not match factor"
            )
        )


def _validate_step(
    step: Any, index: int, source: str | None, violations: list[ValidationViolation]
) -> None:
    path = f"$projection.steps[{index - 1}]"
    if not _has_exact_fields(step, _STEP_FIELDS, path, violations):
        return
    _validate_step_fields(step, index, path, violations)
    _validate_step_mapping(step, index, source, path, violations)


def _validate_final_step(
    steps: list[Any], control_action_id: Any, violations: list[ValidationViolation]
) -> None:
    final = steps[-1]
    if not isinstance(final, dict):
        return
    valid = (
        final.get("kind") == "UNSAFE_CONTROL_ACTION"
        and final.get("factor_id") is None
        and final.get("structural_source_id") == control_action_id
    )
    if not valid:
        violations.append(
            _violation(
                "uca_condition_incompatible",
                "$.projection.steps[-1]",
                "final step must be the target unsafe control action",
            )
        )


def _validate_projection_steps(
    value: dict[str, Any],
    sources: list[str | None],
    factors: list[Any] | None,
    violations: list[ValidationViolation],
) -> None:
    steps = value.get("steps")
    if not _validate_steps_container(steps, violations):
        return
    _validate_step_count(steps, factors, violations)
    _validate_causal_steps(steps, sources, violations)
    _validate_final_step_fields(steps, violations)
    _validate_final_step(steps, value.get("control_action_id"), violations)


def _validate_steps_container(steps: Any, violations: list[ValidationViolation]) -> bool:
    if isinstance(steps, list) and steps:
        return True
    violations.append(
        _violation(
            "required_field_missing",
            "$.projection.steps",
            "published v2 projection requires ordered steps",
        )
    )
    return False


def _validate_step_count(
    steps: list[Any], factors: list[Any] | None, violations: list[ValidationViolation]
) -> None:
    if factors is not None and len(steps) != len(factors) + 1:
        violations.append(
            _violation(
                "step_mapping_mismatch",
                "$.projection.steps",
                "one causal step and one final UCA step are required per factor",
            )
        )


def _validate_causal_steps(
    steps: list[Any], sources: list[str | None], violations: list[ValidationViolation]
) -> None:
    for index, step in enumerate(steps[:-1], start=1):
        source = sources[index - 1] if index <= len(sources) else None
        _validate_step(step, index, source, violations)


def _validate_final_step_fields(steps: list[Any], violations: list[ValidationViolation]) -> None:
    _validate_step(steps[-1], len(steps), None, violations)


def _validate_outcome_fields(
    outcome: dict[str, Any],
    projection: dict[str, Any],
    path: str,
    violations: list[ValidationViolation],
) -> None:
    for key in ("control_action_id", "uca_type"):
        if outcome.get(key) != projection.get(key):
            violations.append(
                _violation(
                    "identity_mismatch",
                    f"{path}.{key}",
                    f"outcome {key} differs from projection",
                )
            )
    _validate_outcome_id(outcome.get("outcome_id"), path, violations)
    for key in ("hazard_refs", "constraint_refs"):
        _strings_array(outcome.get(key), f"{path}.{key}", violations)
    _strict_bool(
        outcome.get("semantic_binding_required"),
        f"{path}.semantic_binding_required",
        violations,
    )


def _validate_outcome_id(
    outcome_id: Any, path: str, violations: list[ValidationViolation]
) -> None:
    if not _string(outcome_id, f"{path}.outcome_id", violations):
        return
    if not _OUTCOME_ID_RE.fullmatch(outcome_id):
        violations.append(
            _violation(
                "schema_field_invalid",
                f"{path}.outcome_id",
                "outcome_id must use the OUTCOME-* namespace",
            )
        )


def _validate_outcome_semantics(
    outcome: dict[str, Any],
    projection: dict[str, Any],
    path: str,
    existing_refs: set[str],
    violations: list[ValidationViolation],
) -> set[str]:
    refs = _validate_condition(outcome.get("condition"), f"{path}.condition", violations)
    _validate_outcome_ref_uniqueness(existing_refs, refs, path, violations)
    _validate_outcome_condition_type(outcome, projection, path, violations)
    _validate_outcome_binding_state(outcome, refs, path, violations)
    _validate_outcome_action(outcome, projection, path, violations)
    return refs


def _validate_outcome_ref_uniqueness(
    existing_refs: set[str], refs: set[str], path: str, violations: list[ValidationViolation]
) -> None:
    if existing_refs & refs:
        violations.append(
            _violation(
                "semantic_binding_state_mismatch",
                f"{path}.condition",
                "binding references must be unique",
            )
        )


def _validate_outcome_condition_type(
    outcome: dict[str, Any],
    projection: dict[str, Any],
    path: str,
    violations: list[ValidationViolation],
) -> None:
    condition = outcome.get("condition")
    condition_type = condition.get("type") if isinstance(condition, dict) else None
    allowed = _UCA_CONDITION_TYPES.get(projection.get("uca_type"), set())
    if condition_type not in allowed:
        violations.append(
            _violation(
                "uca_condition_incompatible",
                f"{path}.condition.type",
                "condition type is incompatible with UCA",
            )
        )


def _validate_outcome_binding_state(
    outcome: dict[str, Any],
    refs: set[str],
    path: str,
    violations: list[ValidationViolation],
) -> None:
    if outcome.get("semantic_binding_required") != bool(refs):
        violations.append(
            _violation(
                "semantic_binding_state_mismatch",
                f"{path}.semantic_binding_required",
                "binding flag does not match condition placeholders",
            )
        )


def _validate_outcome_action(
    outcome: dict[str, Any],
    projection: dict[str, Any],
    path: str,
    violations: list[ValidationViolation],
) -> None:
    condition = outcome.get("condition")
    condition_action = condition.get("control_action_id") if isinstance(condition, dict) else None
    if condition_action and condition_action != projection.get("control_action_id"):
        violations.append(
            _violation(
                "condition_reference_mismatch",
                f"{path}.condition.control_action_id",
                "condition action differs from projection",
            )
        )


def _validate_projection_outcome(
    value: dict[str, Any], existing_refs: set[str], violations: list[ValidationViolation]
) -> set[str]:
    path = "$.projection.unsafe_outcome"
    outcome = value.get("unsafe_outcome")
    if not _has_exact_fields(outcome, _OUTCOME_FIELDS, path, violations):
        return set()
    _validate_outcome_fields(outcome, value, path, violations)
    return _validate_outcome_semantics(outcome, value, path, existing_refs, violations)


def _validate_categories(
    categories: Any, path: str, violations: list[ValidationViolation]
) -> None:
    if not _strings_array(categories, path, violations):
        return
    for item in categories:
        if item not in _SURFACE_CATEGORY_VALUES:
            violations.append(
                _violation("condition_value_invalid", path, f"unknown category {item!r}")
            )
    if len(categories) != len(set(categories)):
        violations.append(_violation("condition_value_invalid", path, "categories must be unique"))


def _validate_projection_requirements(
    value: dict[str, Any], violations: list[ValidationViolation]
) -> None:
    path = "$.projection.execution_requirements"
    requirements = value.get("execution_requirements")
    if not _has_exact_fields(requirements, _REQUIREMENT_FIELDS, path, violations):
        return
    for key in _REQUIREMENT_FIELDS - {"required_surface_categories"}:
        _strict_bool(requirements.get(key), f"{path}.{key}", violations)
    _validate_categories(
        requirements.get("required_surface_categories"),
        f"{path}.required_surface_categories",
        violations,
    )


def _validate_projection_execution_models(
    value: dict[str, Any], violations: list[ValidationViolation]
) -> None:
    """Validate the v2 contract/classification as closed producer models."""

    contract = _parse_projection_contract(value, violations)
    classification = _parse_projection_classification(value, violations)
    if contract is None or classification is None:
        return
    _validate_contract_factor_reference(contract, value, violations)
    _validate_target_action_requirements(contract, value, violations)
    _validate_projection_classification(contract, classification, violations)


def _parse_projection_contract(
    value: dict[str, Any], violations: list[ValidationViolation]
) -> SemanticExecutionContract | None:
    try:
        return SemanticExecutionContract.model_validate(value.get("execution_contract"))
    except (ValidationError, TypeError, ValueError) as exc:
        violations.append(
            _violation(
                "schema_field_invalid",
                "$.projection.execution_contract",
                f"execution contract is invalid: {exc}",
            )
        )
        return None


def _parse_projection_classification(
    value: dict[str, Any], violations: list[ValidationViolation]
) -> ExecutionClassification | None:
    try:
        return ExecutionClassification.model_validate(value.get("execution_classification"))
    except (ValidationError, TypeError, ValueError) as exc:
        violations.append(
            _violation(
                "schema_field_invalid",
                "$.projection.execution_classification",
                f"execution classification is invalid: {exc}",
            )
        )
        return None


def _validate_contract_factor_reference(
    contract: SemanticExecutionContract,
    value: dict[str, Any],
    violations: list[ValidationViolation],
) -> None:
    factors = value.get("causal_factors")
    factor_ids = (
        {item.get("factor_id") for item in factors if isinstance(item, dict)}
        if isinstance(factors, list)
        else set()
    )
    if contract.delivery is not None and contract.delivery.factor_id not in factor_ids:
        violations.append(
            _violation(
                "schema_field_invalid",
                "$.projection.execution_contract.delivery.factor_id",
                "execution delivery factor_id must resolve to a causal factor",
            )
        )


def _validate_target_action_requirements(
    contract: SemanticExecutionContract,
    value: dict[str, Any],
    violations: list[ValidationViolation],
) -> None:
    """Require external-action requirements to name the selected UCA."""

    outcome = value.get("unsafe_outcome")
    action_id = outcome.get("control_action_id") if isinstance(outcome, dict) else None
    if not isinstance(action_id, str):
        return
    for index, requirement in enumerate(contract.resource_requirements):
        if requirement.purpose.value != "target_action":
            continue
        if requirement.owner_ref == action_id and requirement.operation == action_id:
            continue
        violations.append(
            _violation(
                "identity_mismatch",
                f"$.projection.execution_contract.resource_requirements[{index}]",
                "target_action requirement must name the unsafe outcome control action",
            )
        )


def _validate_projection_classification(
    contract: SemanticExecutionContract,
    classification: ExecutionClassification,
    violations: list[ValidationViolation],
) -> None:
    expected = _expected_classification(contract)
    if expected is None:
        return
    actual = (
        classification.binding_completeness,
        classification.environment_basis,
        classification.profile_fit,
        classification.claim_scope,
    )
    if actual != expected or _classification_has_bindings(classification):
        detail = (
            "analytical_only contracts require the analytical classification tuple"
            if contract.disposition is ExecutionContractDisposition.analytical_only
            else "resource-free contracts require the target-agnostic classification tuple"
        )
        violations.append(
            _violation(
                "schema_field_invalid",
                "$.projection.execution_classification",
                detail,
            )
        )


def _expected_classification(
    contract: SemanticExecutionContract,
) -> tuple[BindingCompleteness, EnvironmentBasis, ExecutionProfileFit, ExecutionClaimScope] | None:
    if contract.disposition is ExecutionContractDisposition.analytical_only:
        return (
            BindingCompleteness.analytical_only,
            EnvironmentBasis.none,
            ExecutionProfileFit.invalid,
            ExecutionClaimScope.no_execution_claim,
        )
    if not contract.resource_requirements:
        return (
            BindingCompleteness.concrete,
            EnvironmentBasis.target_agnostic,
            ExecutionProfileFit.not_required,
            ExecutionClaimScope.model_behavior_only,
        )
    return None


def _classification_has_bindings(classification: ExecutionClassification) -> bool:
    return bool(
        classification.resolved_bindings
        or classification.unresolved_requirement_ids
        or classification.ambiguous_matches
        or classification.unsupported_requirement_ids
        or classification.target_profile_digest is not None
    )


def _validate_projection_trace(
    value: dict[str, Any], violations: list[ValidationViolation]
) -> None:
    path = "$.projection.trace_refs"
    trace_refs = value.get("trace_refs")
    if not _has_exact_fields(trace_refs, _TRACE_FIELDS, path, violations):
        return
    for key in _TRACE_FIELDS - {"source_pins"}:
        _strings_array(trace_refs.get(key), f"{path}.{key}", violations)
    pins = trace_refs.get("source_pins")
    if not isinstance(pins, dict):
        violations.append(
            _violation("container_type_mismatch", f"{path}.source_pins", "expected an object")
        )
        return
    if set(pins) != _SOURCE_PIN_FIELDS:
        violations.append(
            _violation(
                "source_pin_mismatch",
                f"{path}.source_pins",
                "source pins must contain exactly the four producer digests",
            )
        )
    for key, item in pins.items():
        _string(key, f"{path}.source_pins.{key}", violations)
        _digest(item, f"{path}.source_pins.{key}", violations)


def _validate_projection_digest(
    value: dict[str, Any], violations: list[ValidationViolation]
) -> None:
    if not _digest(value.get("semantic_digest"), "$.projection.semantic_digest", violations):
        return
    if violations:
        return
    expected = _compute_digest(value, PROJECTION_SCHEMA_VERSION, "$.projection", violations)
    if expected is not None and value.get("semantic_digest") != expected:
        violations.append(
            _violation(
                "semantic_digest_mismatch",
                "$.projection.semantic_digest",
                "projection digest does not match canonical content",
            )
        )


def _validate_projection(value: Any) -> list[ValidationViolation]:
    """Validate one projection by delegating each closed section."""

    violations: list[ValidationViolation] = []
    _scan_forbidden(value, "$projection", violations)
    if not _validate_projection_header(value, violations):
        return violations
    _validate_projection_identity(value, violations)
    sources, refs, factors = _validate_projection_factors(value, violations)
    _validate_projection_steps(value, sources, factors, violations)
    refs.update(_validate_projection_outcome(value, refs, violations))
    _validate_projection_requirements(value, violations)
    _validate_projection_execution_models(value, violations)
    _validate_projection_trace(value, violations)
    _validate_projection_digest(value, violations)
    return violations


def _scan_forbidden(value: Any, path: str, violations: list[ValidationViolation]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FORBIDDEN_KEYS:
                violations.append(
                    _violation(
                        "runtime_observation_forbidden",
                        f"{path}.{key}",
                        "runtime/platform data is not allowed in projection",
                    )
                )
            _scan_forbidden(item, f"{path}.{key}", violations)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_forbidden(item, f"{path}[{index}]", violations)


def _scenario_identities(document: dict[str, Any]) -> dict[str, Any]:
    """Return the scenario envelope's exact execution identity fields.

    The producer's canonical ``ScenarioEnvelope`` keeps the execution
    identity inside ``scenario_spec`` rather than duplicating candidate,
    slot, and ICA fields at the envelope root.  Keep the root/container
    fallback for older envelopes, but let the authoritative nested shape
    override it whenever it is present.
    """

    values = {
        key: document[key] for key in ("scenario_id", "target_responsibility") if key in document
    }
    values.update(_scenario_spec_identities(document.get("scenario_spec")))
    return values


def _scenario_spec_identities(scenario_spec: Any) -> dict[str, Any]:
    if not isinstance(scenario_spec, dict):
        return {}
    direct_fields = {
        "scenario_id": "scenario_id",
        "target_controller": "controller_id",
        "target_control_action": "control_action_id",
        "ica_type": "uca_type",
    }
    values = {
        identity_key: scenario_spec[source_key]
        for source_key, identity_key in direct_fields.items()
        if source_key in scenario_spec
    }
    _copy_threat_source_identities(values, scenario_spec.get("threat_source"))
    _set_scenario_candidate(values)
    return values


def _copy_threat_source_identities(values: dict[str, Any], threat_source: Any) -> None:
    if not isinstance(threat_source, dict):
        return
    for key in ("ica_slot_id", "ica_id"):
        if key in threat_source:
            values[key] = threat_source[key]


def _set_scenario_candidate(values: dict[str, Any]) -> None:
    controller = values.get("controller_id")
    action = values.get("control_action_id")
    uca_type = values.get("uca_type")
    if all(isinstance(item, str) and item for item in (controller, action, uca_type)):
        values["candidate_id"] = f"EXEC:{controller}:{action}:{uca_type}"


def _presentation_context(document: dict[str, Any]) -> dict[str, Any]:
    """Copy only optional context fields for a post-readiness author."""

    keys = (
        "narrative",
        "attack_tree",
        "behavior_spec",
        "loss_context",
        "gherkin",
        "feature",
        "title",
        "summary",
    )
    return {key: document[key] for key in keys if key in document}


def _check_pair_identity(
    entry: dict[str, Any],
    projection: dict[str, Any],
    scenario: dict[str, Any],
    run_id: str,
) -> list[ValidationViolation]:
    projection_expected = {
        "run_id": run_id,
        "scenario_id": entry.get("scenario_id"),
        "candidate_id": entry.get("candidate_id"),
        "ica_slot_id": entry.get("ica_slot_id"),
        "ica_id": entry.get("ica_id"),
        "controller_id": projection.get("controller_id"),
        "control_action_id": projection.get("control_action_id"),
        "uca_type": projection.get("uca_type"),
    }
    violations = _compare_pair_fields(
        projection, projection_expected, "projection", required_keys=set(projection_expected)
    )
    scenario_values = _scenario_identities(scenario)
    scenario_expected = {
        **projection_expected,
        "target_responsibility": projection_expected["controller_id"],
    }
    violations.extend(
        _compare_pair_fields(
            scenario_values,
            scenario_expected,
            "scenario",
            required_keys={
                "scenario_id",
                "candidate_id",
                "ica_slot_id",
                "ica_id",
                "controller_id",
                "control_action_id",
                "uca_type",
                "target_responsibility",
            },
        )
    )
    violations.extend(_scenario_envelope_identity_violations(scenario))
    return violations


def _scenario_envelope_identity_violations(
    scenario: Mapping[str, Any],
) -> list[ValidationViolation]:
    scenario_spec = scenario.get("scenario_spec")
    if not isinstance(scenario_spec, dict):
        return []
    violations: list[ValidationViolation] = []
    if scenario.get("scenario_id") != scenario_spec.get("scenario_id"):
        violations.append(
            _violation(
                "pair_identity_mismatch",
                "$.scenario.scenario_spec.scenario_id",
                "scenario envelope and scenario_spec IDs differ",
            )
        )
    if "ica_type" in scenario and scenario.get("ica_type") != scenario_spec.get("ica_type"):
        violations.append(
            _violation(
                "pair_identity_mismatch",
                "$.scenario.ica_type",
                "scenario envelope and scenario_spec UCA types differ",
            )
        )
    return violations


def _compare_pair_fields(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any],
    section: str,
    required_keys: set[str],
) -> list[ValidationViolation]:
    violations: list[ValidationViolation] = []
    for key, expected_value in expected.items():
        if key not in actual and key not in required_keys:
            continue
        if actual.get(key) != expected_value:
            violations.append(
                _violation(
                    "pair_identity_mismatch",
                    f"$.{section}.{key}",
                    f"{section} identity differs from index",
                )
            )
    return violations


def load_execution_bundle(
    index_path: Path,
    *,
    limits: LoaderLimits | None = None,
) -> VerifiedExecutionBundle:
    """Load and strictly verify one canonical ``execution-bundle.json``.

    The function performs no provider or network activity.  It raises
    :class:`BundleValidationError` on the first invalid bundle transaction;
    no partial intent is returned.
    """

    return _load_execution_bundle(index_path, limits=limits or LoaderLimits())


def _load_execution_bundle(index_path: Path, *, limits: LoaderLimits) -> VerifiedExecutionBundle:
    path = _bundle_index_path(index_path)
    total = [0]
    index_bytes = _read_limited(path, limits.max_index_bytes, total, limits)
    index = _parse_json(index_bytes, path, limits)
    _require_canonical_bytes(index_bytes, index, str(path))
    index_violations = _validate_bundle_index(index)
    if index_violations:
        raise BundleValidationError(index_violations)
    entries = _bundle_entries(index, limits)
    root = path.parent.resolve()
    verified_entries = tuple(
        _load_bundle_entry(entry, index_number, root, total, limits, index)
        for index_number, entry in enumerate(entries)
    )
    return VerifiedExecutionBundle(
        index_path=path.resolve(),
        schema_version=index["schema_version"],
        run_id=index["run_id"],
        producer=dict(index["producer"]),
        bundle_digest=index["bundle_digest"],
        entries=verified_entries,
    )


def _bundle_index_path(index_path: Path) -> Path:
    path = Path(index_path)
    if not path.is_file():
        raise BundleValidationError(
            [_violation("bundle_path_invalid", str(path), "bundle index does not exist")]
        )
    return path


def _require_canonical_bytes(raw: bytes, value: Any, path: str) -> None:
    violation = _canonical_bytes_violation(raw, value, path)
    if violation is not None:
        raise BundleValidationError([violation])


def _bundle_entries(index: dict[str, Any], limits: LoaderLimits) -> list[Any]:
    entries = index["entries"]
    if len(entries) > limits.max_entry_count:
        raise BundleValidationError(
            [_violation("resource_limit_exceeded", "$.entries", "entry count limit exceeded")]
        )
    return entries


def _load_bundle_entry(
    entry: dict[str, Any],
    index_number: int,
    root: Path,
    total: list[int],
    limits: LoaderLimits,
    index: dict[str, Any],
) -> VerifiedExecutionBundleEntry:
    entry_path = f"$.entries[{index_number}]"
    scenario_path = _safe_relative_path(
        root, entry["scenario"]["path"], f"{entry_path}.scenario.path"
    )
    projection_path = _safe_relative_path(
        root, entry["projection"]["path"], f"{entry_path}.projection.path"
    )
    scenario_bytes = _read_limited(scenario_path, limits.max_scenario_bytes, total, limits)
    projection_bytes = _read_limited(projection_path, limits.max_projection_bytes, total, limits)
    scenario_document, scenario_violations = _read_scenario_document(
        scenario_bytes, scenario_path, limits
    )
    projection_document, projection_violations = _read_projection_document(
        projection_bytes, projection_path, limits
    )
    violations = _entry_pair_violations(
        entry,
        index,
        entry_path,
        scenario_bytes,
        projection_bytes,
        scenario_document,
        projection_document,
        scenario_violations,
        projection_violations,
    )
    if violations:
        raise BundleValidationError(violations)
    intent = _build_entry_intent(
        entry,
        index,
        entry_path,
        scenario_document,
        projection_document,
    )
    return _make_verified_entry(
        entry,
        scenario_path,
        projection_path,
        scenario_bytes,
        projection_bytes,
        scenario_document,
        projection_document,
        intent,
    )


def _read_scenario_document(
    raw: bytes, path: Path, limits: LoaderLimits
) -> tuple[dict[str, Any], list[ValidationViolation]]:
    document = _parse_json(raw, path, limits)
    if not isinstance(document, dict):
        return (
            {},
            [_violation("container_type_mismatch", str(path), "scenario root must be an object")],
        )
    violation = _canonical_bytes_violation(raw, document, str(path))
    return document, [] if violation is None else [violation]


def _read_projection_document(
    raw: bytes, path: Path, limits: LoaderLimits
) -> tuple[Any, list[ValidationViolation]]:
    document = _parse_json(raw, path, limits)
    violations: list[ValidationViolation] = []
    if isinstance(document, dict):
        violation = _canonical_bytes_violation(raw, document, str(path))
        if violation is not None:
            violations.append(violation)
    return document, violations


def _content_digest_violations(
    entry: dict[str, Any],
    entry_path: str,
    scenario_bytes: bytes,
    projection_bytes: bytes,
) -> list[ValidationViolation]:
    violations: list[ValidationViolation] = []
    if sha256_bytes(scenario_bytes) != entry["scenario"]["content_sha256"]:
        violations.append(
            _violation(
                "content_digest_mismatch",
                f"{entry_path}.scenario.content_sha256",
                "scenario bytes do not match index",
            )
        )
    if sha256_bytes(projection_bytes) != entry["projection"]["content_sha256"]:
        violations.append(
            _violation(
                "content_digest_mismatch",
                f"{entry_path}.projection.content_sha256",
                "projection bytes do not match index",
            )
        )
    return violations


def _projection_reference_violation(
    entry: dict[str, Any], entry_path: str, projection_document: Any
) -> ValidationViolation | None:
    if (
        isinstance(projection_document, dict)
        and projection_document.get("semantic_digest") != entry["projection"]["semantic_digest"]
    ):
        return _violation(
            "semantic_digest_mismatch",
            f"{entry_path}.projection.semantic_digest",
            "index projection digest differs from document",
        )
    return None


def _entry_pair_violations(
    entry: dict[str, Any],
    index: dict[str, Any],
    entry_path: str,
    scenario_bytes: bytes,
    projection_bytes: bytes,
    scenario_document: dict[str, Any],
    projection_document: Any,
    scenario_violations: list[ValidationViolation],
    projection_violations: list[ValidationViolation],
) -> list[ValidationViolation]:
    violations = list(scenario_violations) + list(projection_violations)
    violations.extend(
        _content_digest_violations(entry, entry_path, scenario_bytes, projection_bytes)
    )
    reference_violation = _projection_reference_violation(entry, entry_path, projection_document)
    if reference_violation is not None:
        violations.append(reference_violation)
    if isinstance(projection_document, dict):
        violations.extend(
            _check_pair_identity(entry, projection_document, scenario_document, index["run_id"])
        )
    return violations


def _build_entry_intent(
    entry: dict[str, Any],
    index: dict[str, Any],
    entry_path: str,
    scenario_document: dict[str, Any],
    projection_document: Any,
) -> ExecutionIntent:
    try:
        return ExecutionIntent.from_projection(
            projection_document,
            bundle_digest=index["bundle_digest"],
            scenario_content_sha256=entry["scenario"]["content_sha256"],
            projection_content_sha256=entry["projection"]["content_sha256"],
            presentation_context=_presentation_context(scenario_document),
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise BundleValidationError(
            [
                _violation(
                    "schema_field_invalid",
                    entry_path,
                    f"cannot build immutable execution intent: {exc}",
                )
            ]
        ) from exc


def _make_verified_entry(
    entry: dict[str, Any],
    scenario_path: Path,
    projection_path: Path,
    scenario_bytes: bytes,
    projection_bytes: bytes,
    scenario_document: dict[str, Any],
    projection_document: dict[str, Any],
    intent: ExecutionIntent,
) -> VerifiedExecutionBundleEntry:
    return VerifiedExecutionBundleEntry(
        scenario_id=entry["scenario_id"],
        candidate_id=entry["candidate_id"],
        ica_slot_id=entry["ica_slot_id"],
        ica_id=entry["ica_id"],
        scenario_path=scenario_path,
        projection_path=projection_path,
        scenario_content_sha256=entry["scenario"]["content_sha256"],
        projection_content_sha256=entry["projection"]["content_sha256"],
        projection_semantic_digest=entry["projection"]["semantic_digest"],
        scenario_document=scenario_document,
        projection_document=projection_document,
        intent=intent,
        scenario_bytes=scenario_bytes,
        projection_bytes=projection_bytes,
    )


def load_execution_bundle_with_limits(
    index_path: Path, limits: LoaderLimits
) -> VerifiedExecutionBundle:
    """Explicit-limit variant used by resource-limit tests and callers."""

    return _load_execution_bundle(index_path, limits=limits)


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "BundleValidationError",
    "LoaderLimits",
    "PROJECTION_SCHEMA_VERSION",
    "ValidationViolation",
    "VerifiedExecutionBundle",
    "VerifiedExecutionBundleEntry",
    "load_execution_bundle",
    "load_execution_bundle_with_limits",
]
