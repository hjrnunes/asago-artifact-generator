"""Consumer-owned runtime bindings and review evidence.

Bindings are explicit deployment facts.  No value in this module is inferred
from scenario prose, taxonomy identifiers, model output, or platform defaults.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ._base import (
    ImmutableModel,
    SHA256Digest,
    canonical_json_bytes,
    compute_framed_digest,
    freeze_value,
)

RUNTIME_BINDING_SCHEMA_VERSION = "runtime-binding-set-v1"
RUNTIME_SURFACES = (
    "system_prompt",
    "user_turn",
    "assistant_turn",
    "tool_call",
    "tool_result",
    "tool_definition",
    "memory",
    "data_store",
    "agent_message",
    "environment_event",
)


class RuntimeBindingValidationError(ValueError):
    """Raised when a consumer-owned binding document is malformed or forged."""


OBSERVER_KINDS = (
    "tool_call",
    "tool_argument",
    "output_text",
    "state_value",
    "event_presence",
    "event_absence",
    "event_order",
    "elapsed_time",
    "duration",
)


class ReviewEvidence(ImmutableModel):
    """Human review attestation required for runtime and semantic bindings."""

    reviewed_by: StrictStr = Field(min_length=1)
    reviewed_at: StrictStr = Field(min_length=1)
    rationale: StrictStr = Field(min_length=1)
    evidence_refs: tuple[StrictStr, ...] = Field(min_length=1)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _refs_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("evidence_refs must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _unique_refs(self) -> ReviewEvidence:
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs must be unique")
        return self


class SemanticBinding(ImmutableModel):
    """One reviewed value for one exact producer condition placeholder."""

    condition_ref: StrictStr = Field(min_length=1)
    binding_ref: StrictStr = Field(min_length=1)
    value: StrictStr | StrictInt | StrictFloat | StrictBool
    reviewed_by: StrictStr = Field(min_length=1)
    rationale: StrictStr = Field(min_length=1)
    evidence_refs: tuple[StrictStr, ...] = Field(min_length=1)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _refs_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise TypeError("semantic binding evidence_refs must be an array")
        return tuple(value)


class SurfaceBinding(ImmutableModel):
    """Explicit mapping from an exported structural source to one surface."""

    source_ref: StrictStr = Field(min_length=1)
    surface: Literal[
        "system_prompt",
        "user_turn",
        "assistant_turn",
        "tool_call",
        "tool_result",
        "tool_definition",
        "memory",
        "data_store",
        "agent_message",
        "environment_event",
    ]
    locator: StrictStr = Field(min_length=1)
    writable: StrictBool


class ControlActionBinding(ImmutableModel):
    """Concrete adapter operation and deployment-owned tool declaration."""

    control_action_id: StrictStr = Field(min_length=1)
    adapter_operation: StrictStr = Field(min_length=1)
    tool_name: StrictStr = ""
    tool_schema: Mapping[str, Any] = Field(default_factory=dict)
    safe_defaults: Mapping[str, Any] = Field(default_factory=dict)
    secret_handles: tuple[StrictStr, ...] = ()

    @field_validator("tool_schema", "safe_defaults", mode="before")
    @classmethod
    def _mapping_value(cls, value: Any) -> Mapping[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError("tool_schema and safe_defaults must be objects")
        return freeze_value(value)

    @field_validator("secret_handles", mode="before")
    @classmethod
    def _handles_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("secret_handles must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _reject_inline_secrets(self) -> ControlActionBinding:
        secret_names = ("password", "passwd", "secret", "token", "api_key", "credential")
        for key in self.safe_defaults:
            if any(name in str(key).lower() for name in secret_names):
                raise ValueError("safe_defaults must not contain credentials or secret values")
        return self


class ObservationBinding(ImmutableModel):
    """Concrete observer for one producer-owned semantic condition."""

    condition_ref: StrictStr = Field(min_length=1)
    observer_kind: Literal[
        "tool_call",
        "tool_argument",
        "output_text",
        "state_value",
        "event_presence",
        "event_absence",
        "event_order",
        "elapsed_time",
        "duration",
    ]
    event_source: StrictStr = Field(min_length=1)
    semantic_property: StrictStr = Field(min_length=1)
    field_path: StrictStr = ""
    comparison: StrictStr = Field(min_length=1)
    expected_from: StrictStr = Field(min_length=1)


class ClockBinding(ImmutableModel):
    """Deployment clock used for real delay, duration, window, or absence."""

    clock_kind: StrictStr = Field(min_length=1)
    resolution_ms: StrictInt = Field(gt=0)
    event_timestamp_source: StrictStr = Field(min_length=1)


class RuntimeBindingSet(ImmutableModel):
    """Closed binding set that is valid for exactly one projection digest."""

    schema_version: Literal["runtime-binding-set-v1"] = RUNTIME_BINDING_SCHEMA_VERSION
    binding_set_id: StrictStr = Field(min_length=1)
    projection_semantic_digest: SHA256Digest
    target_environment_id: StrictStr = Field(min_length=1)
    semantic_bindings: tuple[SemanticBinding, ...] = ()
    surface_bindings: tuple[SurfaceBinding, ...] = ()
    control_action_bindings: tuple[ControlActionBinding, ...] = ()
    observation_bindings: tuple[ObservationBinding, ...] = ()
    clock_binding: ClockBinding | None = None
    review: ReviewEvidence
    semantic_digest: SHA256Digest | None = None

    @field_validator(
        "semantic_bindings",
        "surface_bindings",
        "control_action_bindings",
        "observation_bindings",
        mode="before",
    )
    @classmethod
    def _bindings_as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("binding collections must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _unique_binding_keys(self) -> RuntimeBindingSet:
        keys = _runtime_binding_keys(self)
        if len(keys) != len(set(keys)):
            raise ValueError("runtime binding identifiers must be unique")
        return self

    def digest_payload(self) -> dict[str, Any]:
        """Return the complete canonical document excluding its digest."""

        return self.model_dump(mode="json", exclude={"semantic_digest"})

    def compute_semantic_digest(self) -> str:
        """Compute the v1 binding-set framed digest."""

        return compute_framed_digest(RUNTIME_BINDING_SCHEMA_VERSION, self.digest_payload())

    def verify_digest(self) -> RuntimeBindingSet:
        """Raise when the persisted binding-set digest is not self-consistent."""

        expected = self.compute_semantic_digest()
        if self.semantic_digest is None or self.semantic_digest != expected:
            raise ValueError(
                "runtime binding semantic_digest does not match its canonical content"
            )
        return self

    def canonical_bytes(self) -> bytes:
        """Return canonical JSON bytes for persistence or trace hashing."""

        return canonical_json_bytes(self.model_dump(mode="json"))

    def with_computed_digest(self) -> RuntimeBindingSet:
        """Return a copy with the canonical semantic digest attached."""

        return self.model_copy(update={"semantic_digest": self.compute_semantic_digest()})

    @classmethod
    def create(
        cls,
        *,
        binding_set_id: str,
        projection_semantic_digest: str,
        target_environment_id: str,
        review: ReviewEvidence,
        semantic_bindings: tuple[SemanticBinding, ...] | list[SemanticBinding] = (),
        surface_bindings: tuple[SurfaceBinding, ...] | list[SurfaceBinding] = (),
        control_action_bindings: tuple[ControlActionBinding, ...]
        | list[ControlActionBinding] = (),
        observation_bindings: tuple[ObservationBinding, ...] | list[ObservationBinding] = (),
        clock_binding: ClockBinding | None = None,
    ) -> RuntimeBindingSet:
        """Build and attest a binding set without a placeholder digest."""

        value = cls(
            binding_set_id=binding_set_id,
            projection_semantic_digest=projection_semantic_digest,
            target_environment_id=target_environment_id,
            semantic_bindings=semantic_bindings,
            surface_bindings=surface_bindings,
            control_action_bindings=control_action_bindings,
            observation_bindings=observation_bindings,
            clock_binding=clock_binding,
            review=review,
        )
        return value.with_computed_digest()


def _runtime_binding_keys(value: RuntimeBindingSet) -> tuple[tuple[str, ...], ...]:
    return (
        _semantic_binding_keys(value.semantic_bindings)
        + _surface_binding_keys(value.surface_bindings)
        + _action_binding_keys(value.control_action_bindings)
        + _observation_binding_keys(value.observation_bindings)
    )


def _semantic_binding_keys(
    bindings: tuple[SemanticBinding, ...],
) -> tuple[tuple[str, ...], ...]:
    return tuple(("semantic", item.condition_ref, item.binding_ref) for item in bindings)


def _surface_binding_keys(bindings: tuple[SurfaceBinding, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(("surface", item.source_ref) for item in bindings)


def _action_binding_keys(
    bindings: tuple[ControlActionBinding, ...],
) -> tuple[tuple[str, ...], ...]:
    return tuple(("action", item.control_action_id) for item in bindings)


def _observation_binding_keys(
    bindings: tuple[ObservationBinding, ...],
) -> tuple[tuple[str, ...], ...]:
    return tuple(("observation", item.condition_ref) for item in bindings)


def parse_runtime_binding_set(value: Mapping[str, Any]) -> RuntimeBindingSet:
    """Parse and verify one strict runtime-binding mapping."""

    if not isinstance(value, Mapping):
        raise RuntimeBindingValidationError("runtime binding root must be an object")
    required = {
        "schema_version",
        "binding_set_id",
        "projection_semantic_digest",
        "target_environment_id",
        "semantic_bindings",
        "surface_bindings",
        "control_action_bindings",
        "observation_bindings",
        "clock_binding",
        "review",
        "semantic_digest",
    }
    unknown = set(value) - required
    missing = required - set(value)
    if unknown:
        raise RuntimeBindingValidationError(
            f"runtime binding has unexpected fields: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise RuntimeBindingValidationError(
            f"runtime binding is missing fields: {', '.join(sorted(missing))}"
        )
    try:
        parsed = RuntimeBindingSet.model_validate(dict(value))
        parsed.verify_digest()
    except (ValueError, TypeError) as exc:
        raise RuntimeBindingValidationError(str(exc)) from exc
    return parsed


def load_runtime_binding_set(path: str | Path) -> RuntimeBindingSet:
    """Read one YAML/JSON binding document and verify its semantic digest."""

    binding_path = Path(path)
    try:
        text = binding_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeBindingValidationError(str(exc)) from exc
    try:
        value = (
            json.loads(text) if binding_path.suffix.lower() == ".json" else yaml.safe_load(text)
        )
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise RuntimeBindingValidationError(f"invalid runtime binding document: {exc}") from exc
    return parse_runtime_binding_set(value)


__all__ = [
    "ClockBinding",
    "ControlActionBinding",
    "OBSERVER_KINDS",
    "ObservationBinding",
    "ReviewEvidence",
    "RUNTIME_BINDING_SCHEMA_VERSION",
    "RUNTIME_SURFACES",
    "RuntimeBindingSet",
    "RuntimeBindingValidationError",
    "SemanticBinding",
    "SurfaceBinding",
    "load_runtime_binding_set",
    "parse_runtime_binding_set",
]
