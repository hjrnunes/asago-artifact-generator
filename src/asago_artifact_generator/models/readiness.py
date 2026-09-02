"""Independent readiness axes and immutable platform-plan values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ._base import ImmutableModel, SHA256Digest, freeze_value
from .execution_intent import UCAType
from .runtime_binding import ClockBinding

SourceStatus = Literal["valid", "invalid"]
SemanticBindingStatus = Literal["complete", "incomplete", "not_required"]
RuntimeBindingStatus = Literal["complete", "incomplete"]
PlatformSupportStatus = Literal["supported", "unsupported", "indeterminate"]
ArtifactStatus = Literal["not_attempted", "generated", "failed"]
OverallReadiness = Literal[
    "invalid",
    "needs_semantic_binding",
    "needs_runtime_binding",
    "unsupported",
    "ready",
]


class PlatformCapabilities(ImmutableModel):
    """Immutable facts declared by one execution adapter."""

    platform: StrictStr = Field(min_length=1)
    adapter_version: StrictStr = Field(min_length=1)
    writable_surfaces: tuple[
        Literal[
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
        ],
        ...,
    ] = ()
    invocable_operations: tuple[StrictStr, ...] = ()
    observer_kinds: tuple[
        Literal[
            "tool_call",
            "tool_argument",
            "output_text",
            "state_value",
            "event_presence",
            "event_absence",
            "event_order",
            "elapsed_time",
            "duration",
        ],
        ...,
    ] = ()
    supports_multi_turn: StrictBool = False
    supports_persistent_state: StrictBool = False
    supports_multi_agent: StrictBool = False
    supports_real_clock: StrictBool = False
    supports_state_observation: StrictBool = False

    @field_validator("writable_surfaces", "invocable_operations", "observer_kinds", mode="before")
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("capability collections must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _unique_capabilities(self) -> PlatformCapabilities:
        for name in ("writable_surfaces", "invocable_operations", "observer_kinds"):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must contain unique values")
        return self

    def supports_surface(self, surface: str) -> bool:
        """Return whether the adapter advertises one concrete surface."""

        return surface in self.writable_surfaces

    def supports_observer(self, observer_kind: str) -> bool:
        """Return whether the adapter advertises one observer kind."""

        return observer_kind in self.observer_kinds


class ReadinessDiagnostic(ImmutableModel):
    """One deterministic diagnostic retained regardless of overall precedence."""

    axis: Literal["source", "semantic_binding", "runtime_binding", "platform_support", "artifact"]
    code: StrictStr = Field(min_length=1)
    message: StrictStr = Field(min_length=1)
    path: StrictStr = ""


class ResolvedSemanticBinding(ImmutableModel):
    """A placeholder plus its reviewed value in a ready plan."""

    condition_ref: StrictStr = Field(min_length=1)
    binding_ref: StrictStr = Field(min_length=1)
    value: StrictStr | StrictInt | StrictFloat | StrictBool | None


class PlanStep(ImmutableModel):
    """Fixed execution step resolved to a concrete runtime surface."""

    plan_step_id: StrictStr = Field(min_length=1)
    projection_step_id: StrictStr = Field(min_length=1)
    order: int = Field(ge=1)
    kind: Literal["CAUSAL_FACTOR", "UNSAFE_CONTROL_ACTION"]
    factor_id: StrictStr | None = None
    structural_source_id: StrictStr = Field(min_length=1)
    surface: StrictStr = Field(min_length=1)
    locator: StrictStr = Field(min_length=1)
    control_action_id: StrictStr | None = None
    adapter_operation: StrictStr | None = None
    tool_name: StrictStr = ""
    tool_schema: Mapping[str, Any] = Field(default_factory=dict)
    safe_arguments: Mapping[str, Any] = Field(default_factory=dict)
    content_slot_id: StrictStr | None = None

    @field_validator("tool_schema", "safe_arguments", mode="before")
    @classmethod
    def _freeze_mapping(cls, value: Any) -> Mapping[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError("plan mappings must be objects")
        return freeze_value(value)


class ObserverPlan(ImmutableModel):
    """Fixed oracle binding derived from the producer unsafe condition."""

    condition_ref: StrictStr = Field(min_length=1)
    condition_type: StrictStr = Field(min_length=1)
    observer_kind: StrictStr = Field(min_length=1)
    event_source: StrictStr = Field(min_length=1)
    semantic_property: StrictStr = Field(min_length=1)
    field_path: StrictStr = ""
    comparison: StrictStr = Field(min_length=1)
    expected_from: StrictStr = Field(min_length=1)
    expected: Any = None


class StimulusPlan(ImmutableModel):
    """One reviewed adversarial content slot in the prompt-side conversation."""

    stimulus_id: StrictStr = Field(min_length=1)
    projection_step_id: StrictStr = Field(min_length=1)
    factor_id: StrictStr = Field(min_length=1)
    content_slot_id: StrictStr = Field(min_length=1)
    delivery_class: Literal["direct_prompt", "indirect_content", "conversation_context"]
    surface: Literal["system_prompt", "user_turn", "assistant_turn", "tool_result"]
    source_kind: Literal[
        "user_authored", "tool_output", "retrieved_document", "conversation_history"
    ]
    carrier_tool_name: StrictStr = ""
    carrier_tool_schema: Mapping[str, Any] = Field(default_factory=dict)
    carrier_tool_arguments: Mapping[str, Any] = Field(default_factory=dict)
    intent: StrictStr = Field(min_length=1)
    desired_effect: StrictStr = Field(min_length=1)

    @field_validator("carrier_tool_schema", "carrier_tool_arguments", mode="before")
    @classmethod
    def _freeze_carrier_mapping(cls, value: Any) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise TypeError("carrier tool values must be objects")
        return freeze_value(value)


class ReadyExecutionPlan(ImmutableModel):
    """Complete platform plan; the only accepted input to a compiler."""

    schema_version: Literal["execution-plan-v1"] = "execution-plan-v1"
    bundle_digest: SHA256Digest
    projection_semantic_digest: SHA256Digest
    scenario_content_sha256: SHA256Digest
    projection_content_sha256: SHA256Digest
    binding_set_id: StrictStr = Field(min_length=1)
    binding_set_digest: SHA256Digest
    run_id: StrictStr = Field(min_length=1)
    scenario_id: StrictStr = Field(min_length=1)
    candidate_id: StrictStr = Field(min_length=1)
    ica_slot_id: StrictStr = Field(min_length=1)
    ica_id: StrictStr = Field(min_length=1)
    controller_id: StrictStr = Field(min_length=1)
    control_action_id: StrictStr = Field(min_length=1)
    uca_type: UCAType
    platform: StrictStr = Field(min_length=1)
    adapter_version: StrictStr = Field(min_length=1)
    steps: tuple[PlanStep, ...] = ()
    resolved_semantic_bindings: tuple[ResolvedSemanticBinding, ...] = ()
    observers: tuple[ObserverPlan, ...] = ()
    stimuli: tuple[StimulusPlan, ...] = ()
    clock_binding: ClockBinding | None = None
    state_channels: tuple[StrictStr, ...] = ()
    agent_channels: tuple[StrictStr, ...] = ()
    content_slots: tuple[StrictStr, ...] = ()
    trace_map: Mapping[str, Any] = Field(default_factory=dict)
    presentation_context: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator(
        "resolved_semantic_bindings",
        "observers",
        "stimuli",
        "state_channels",
        "agent_channels",
        "content_slots",
        mode="before",
    )
    @classmethod
    def _as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("plan collections must be arrays")
        return tuple(value)

    @field_validator("trace_map", "presentation_context", mode="before")
    @classmethod
    def _freeze_mapping(cls, value: Any) -> Mapping[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError("plan mappings must be objects")
        return freeze_value(value)

    @model_validator(mode="after")
    def _dense_steps(self) -> ReadyExecutionPlan:
        for index, step in enumerate(self.steps, start=1):
            if step.order != index or step.plan_step_id != f"plan-{index}":
                raise ValueError("ready plan steps must be ordered as plan-1, plan-2, ...")
        return self


class ExecutionPlanResult(ImmutableModel):
    """Readiness result preserving every independent axis and diagnostic."""

    source_status: SourceStatus
    semantic_binding_status: SemanticBindingStatus
    runtime_binding_status: RuntimeBindingStatus
    platform_support_status: PlatformSupportStatus
    artifact_status: ArtifactStatus = "not_attempted"
    overall: OverallReadiness
    diagnostics: tuple[ReadinessDiagnostic, ...] = ()
    plan: ReadyExecutionPlan | None = None

    @field_validator("diagnostics", mode="before")
    @classmethod
    def _diagnostics_as_tuple(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("diagnostics must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _plan_matches_overall(self) -> ExecutionPlanResult:
        _require_ready_plan(self.overall, self.plan)
        _reject_non_ready_plan(self.overall, self.plan)
        return self

    @property
    def ready(self) -> bool:
        """Convenience predicate for orchestration code."""

        return self.overall == "ready"


def _require_ready_plan(overall: OverallReadiness, plan: ReadyExecutionPlan | None) -> None:
    if overall == "ready" and plan is None:
        raise ValueError("ready result must carry a ReadyExecutionPlan")


def _reject_non_ready_plan(overall: OverallReadiness, plan: ReadyExecutionPlan | None) -> None:
    if overall != "ready" and plan is not None:
        raise ValueError("non-ready result must not carry a ReadyExecutionPlan")


__all__ = [
    "ArtifactStatus",
    "ExecutionPlanResult",
    "OverallReadiness",
    "ObserverPlan",
    "PlanStep",
    "PlatformCapabilities",
    "PlatformSupportStatus",
    "ReadinessDiagnostic",
    "ReadyExecutionPlan",
    "ResolvedSemanticBinding",
    "StimulusPlan",
    "RuntimeBindingStatus",
    "SemanticBindingStatus",
    "SourceStatus",
]
