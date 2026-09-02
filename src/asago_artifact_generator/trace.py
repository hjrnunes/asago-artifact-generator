"""Closed artifact trace and runtime-observation receipt values."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .models._base import compute_framed_digest, freeze_value

TRACE_SCHEMA_VERSION = "artifact-trace-v1"
RECEIPT_SCHEMA_VERSION = "execution-observation-receipt-v1"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_digest(value: Any, label: str) -> None:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return value


def _require_text(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


def _require_optional_text(value: str | None, label: str) -> None:
    if value is not None:
        _require_text(value, label)


@dataclass(frozen=True, slots=True)
class ArtifactElementTrace:
    artifact_element_id: str
    projection_step_id: str
    factor_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.artifact_element_id, "artifact element trace artifact_element_id")
        _require_text(self.projection_step_id, "artifact element trace projection_step_id")
        _require_optional_text(self.factor_id, "artifact element trace factor_id")

    def to_dict(self) -> dict[str, Any]:
        data = {
            "artifact_element_id": self.artifact_element_id,
            "projection_step_id": self.projection_step_id,
        }
        if self.factor_id is not None:
            data["factor_id"] = self.factor_id
        return data


@dataclass(frozen=True, slots=True)
class OracleTrace:
    artifact_oracle_id: str
    condition_ref: str
    observer_kind: str

    def __post_init__(self) -> None:
        _require_text(self.artifact_oracle_id, "oracle trace artifact_oracle_id")
        _require_text(self.condition_ref, "oracle trace condition_ref")
        _require_text(self.observer_kind, "oracle trace observer_kind")

    def to_dict(self) -> dict[str, str]:
        return {
            "artifact_oracle_id": self.artifact_oracle_id,
            "condition_ref": self.condition_ref,
            "observer_kind": self.observer_kind,
        }


def _validate_trace_source(source: Any) -> None:
    source = _require_mapping(source, "artifact trace source")
    if source.get("projection_schema_version") != "stpa-execution-projection-v2":
        raise ValueError("artifact trace source has an unsupported projection schema")
    _validate_digest_fields(
        source,
        (
            "bundle_digest",
            "projection_semantic_digest",
            "scenario_content_sha256",
            "projection_content_sha256",
        ),
        "artifact trace source",
    )
    _validate_text_fields(
        source,
        ("run_id", "scenario_id", "candidate_id", "ica_slot_id", "ica_id"),
        "artifact trace source",
    )


def _validate_digest_fields(values: Mapping[str, Any], keys: tuple[str, ...], label: str) -> None:
    for key in keys:
        if not values.get(key):
            raise ValueError(f"{label} missing {key}")
        _require_digest(values[key], f"{label} {key}")


def _validate_text_fields(values: Mapping[str, Any], keys: tuple[str, ...], label: str) -> None:
    for key in keys:
        if not isinstance(values.get(key), str) or not values[key].strip():
            raise ValueError(f"{label} missing {key}")


def _validate_trace_binding(binding: Any) -> None:
    binding = _require_mapping(binding, "artifact trace binding")
    if not binding.get("binding_set_id") or not binding.get("semantic_digest"):
        raise ValueError("artifact trace binding requires binding_set_id and semantic_digest")
    _require_digest(binding["semantic_digest"], "artifact trace binding semantic_digest")


def _validate_trace_compiler(compiler: Any) -> None:
    compiler = _require_mapping(compiler, "artifact trace compiler")
    _validate_text_fields(
        compiler,
        ("platform", "adapter_version", "compiler_version"),
        "artifact trace compiler requires",
    )


def _validate_trace_schema(schema_version: str) -> None:
    if schema_version != TRACE_SCHEMA_VERSION:
        raise ValueError("unsupported artifact trace schema_version")


def _validate_trace_entries(
    step_map: tuple[ArtifactElementTrace, ...], oracle_map: tuple[OracleTrace, ...]
) -> None:
    if any(not isinstance(item, ArtifactElementTrace) for item in step_map):
        raise TypeError("artifact trace step_map must contain ArtifactElementTrace values")
    if any(not isinstance(item, OracleTrace) for item in oracle_map):
        raise TypeError("artifact trace oracle_map must contain OracleTrace values")


def _validate_optional_author_digest(author_digest: str) -> None:
    if author_digest and not _DIGEST_RE.fullmatch(author_digest):
        raise ValueError("author_result_digest must be a lowercase SHA-256 digest")


def _set_trace_digest(trace: ArtifactTrace) -> None:
    if not trace.trace_digest:
        object.__setattr__(
            trace,
            "trace_digest",
            compute_framed_digest(trace.schema_version, trace.payload()),
        )
    elif not _DIGEST_RE.fullmatch(trace.trace_digest):
        raise ValueError("trace_digest must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ArtifactTrace:
    """Traceability record accompanying a compiled platform artifact."""

    source: Mapping[str, Any]
    binding: Mapping[str, Any]
    compiler: Mapping[str, Any]
    step_map: tuple[ArtifactElementTrace, ...]
    oracle_map: tuple[OracleTrace, ...]
    author_result_digest: str = ""
    schema_version: Literal["artifact-trace-v1"] = TRACE_SCHEMA_VERSION
    trace_digest: str = ""

    def __post_init__(self) -> None:
        _validate_trace_source(self.source)
        _validate_trace_binding(self.binding)
        _validate_trace_compiler(self.compiler)
        _validate_trace_schema(self.schema_version)
        self._normalize()
        _validate_trace_entries(self.step_map, self.oracle_map)
        _validate_optional_author_digest(self.author_result_digest)
        _set_trace_digest(self)

    def _normalize(self) -> None:
        object.__setattr__(self, "source", freeze_value(self.source))
        object.__setattr__(self, "binding", freeze_value(self.binding))
        object.__setattr__(self, "compiler", freeze_value(self.compiler))
        object.__setattr__(self, "step_map", tuple(self.step_map))
        object.__setattr__(self, "oracle_map", tuple(self.oracle_map))

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": dict(self.source),
            "binding": dict(self.binding),
            "compiler": dict(self.compiler),
            "step_map": [item.to_dict() for item in self.step_map],
            "oracle_map": [item.to_dict() for item in self.oracle_map],
            "author_result_digest": self.author_result_digest,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "trace_digest": self.trace_digest}

    def verify(self) -> None:
        expected = compute_framed_digest(self.schema_version, self.payload())
        if self.trace_digest != expected:
            raise ValueError("artifact trace digest does not match its canonical content")


def _validate_receipt_identity(receipt: ObservationReceipt) -> None:
    for name in ("artifact_digest", "projection_semantic_digest", "binding_set_digest"):
        if not getattr(receipt, name).strip():
            raise ValueError(f"observation receipt {name} must not be empty")
        _require_digest(getattr(receipt, name), f"observation receipt {name}")
    if not receipt.execution_id.strip():
        raise ValueError("observation receipt execution_id must not be empty")


def _validate_receipt_result(result: str) -> None:
    if result not in {"satisfied", "not_satisfied", "inconclusive"}:
        raise ValueError("observation receipt oracle_result is not supported")


def _freeze_observations(
    observations: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    frozen: list[Mapping[str, Any]] = []
    for item in observations:
        if not isinstance(item, Mapping):
            raise TypeError("observation receipt entries must be objects")
        frozen.append(freeze_value(item))
    return tuple(frozen)


def _validate_receipt_schema(schema_version: str) -> None:
    if schema_version != RECEIPT_SCHEMA_VERSION:
        raise ValueError("unsupported observation receipt schema_version")


def _set_receipt_times(receipt: ObservationReceipt) -> None:
    now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if not receipt.started_at:
        object.__setattr__(receipt, "started_at", now)
    if not receipt.completed_at:
        object.__setattr__(receipt, "completed_at", now)


def _set_receipt_digest(receipt: ObservationReceipt) -> None:
    if not receipt.receipt_digest:
        object.__setattr__(
            receipt,
            "receipt_digest",
            compute_framed_digest(receipt.schema_version, receipt.payload()),
        )
    elif not _DIGEST_RE.fullmatch(receipt.receipt_digest):
        raise ValueError("receipt_digest must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ObservationReceipt:
    """Append-only runtime output; never a mutation of source artifacts."""

    artifact_digest: str
    projection_semantic_digest: str
    binding_set_digest: str
    execution_id: str
    observations: tuple[Mapping[str, Any], ...] = ()
    oracle_result: str = "inconclusive"
    started_at: str = ""
    completed_at: str = ""
    schema_version: Literal["execution-observation-receipt-v1"] = RECEIPT_SCHEMA_VERSION
    receipt_digest: str = ""

    def __post_init__(self) -> None:
        _validate_receipt_identity(self)
        _validate_receipt_result(self.oracle_result)
        if not isinstance(self.observations, (list, tuple)):
            raise TypeError("observation receipt observations must be an array")
        object.__setattr__(self, "observations", _freeze_observations(self.observations))
        _validate_receipt_schema(self.schema_version)
        _set_receipt_times(self)
        _set_receipt_digest(self)

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_digest": self.artifact_digest,
            "projection_semantic_digest": self.projection_semantic_digest,
            "binding_set_digest": self.binding_set_digest,
            "execution_id": self.execution_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "observations": [dict(observation) for observation in self.observations],
            "oracle_result": self.oracle_result,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "receipt_digest": self.receipt_digest}

    def verify(self) -> None:
        expected = compute_framed_digest(self.schema_version, self.payload())
        if self.receipt_digest != expected:
            raise ValueError("observation receipt digest does not match its canonical content")


__all__ = [
    "ArtifactElementTrace",
    "ArtifactTrace",
    "ObservationReceipt",
    "OracleTrace",
    "RECEIPT_SCHEMA_VERSION",
    "TRACE_SCHEMA_VERSION",
]
