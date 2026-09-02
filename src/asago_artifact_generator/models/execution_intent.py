"""Immutable inward representation of an STPA execution projection.

The models in this module deliberately describe semantic intent only.  They
do not contain platform roles, tool schemas, detector prompts, provider
clients, or runtime observations.  The bundle loader is responsible for
constructing these values only after validating the producer wire contract.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ._base import ImmutableModel, SHA256Digest, freeze_value
from .semantic_conditions import (
    CONDITION_TYPES,
    OPERATORS,
    SemanticBindingPlaceholder,
    SemanticCondition,
    SemanticValue,
)

UCAType = Literal["NOT_PROVIDED", "INCORRECT", "WRONG_TIMING", "WRONG_DURATION"]
CausalFactorKind = Literal[
    "PROCESS_MODEL_FLAW", "FEEDBACK_DELAY", "SENSOR_ANOMALY", "ACTUATOR_ANOMALY"
]
CausalEvidenceStatus = Literal["structural_failure", "reachable_capability", "bounded_assumption"]
SURFACE_CATEGORIES = (
    "external_input",
    "system_instruction",
    "tool_result",
    "tool_definition",
    "persistent_data",
    "agent_message",
    "environment_event",
)


class CausalFactor(ImmutableModel):
    """One ordered producer-owned causal factor."""

    factor_id: StrictStr = Field(min_length=1, pattern=r"^CF-\d+$")
    order: StrictInt = Field(ge=1)
    kind: CausalFactorKind
    structural_source_id: StrictStr = Field(min_length=1)
    description: StrictStr = Field(min_length=1)
    evidence_status: CausalEvidenceStatus
    capability_refs: tuple[StrictStr, ...] = ()
    access_refs: tuple[StrictStr, ...] = ()
    bounded_assumption: StrictStr | None = None
    temporal_condition: SemanticCondition | None = None

    @field_validator("capability_refs", "access_refs", mode="before")
    @classmethod
    def _refs_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("reference fields must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _validate_source_and_evidence(self) -> CausalFactor:
        _validate_factor_namespace(self.kind, self.structural_source_id)
        _validate_factor_evidence(
            self.evidence_status,
            self.capability_refs,
            self.access_refs,
            self.bounded_assumption,
        )
        return self


class ExecutionStep(ImmutableModel):
    """One canonical ordered projection step."""

    step_id: StrictStr = Field(min_length=1, pattern=r"^S-\d+$")
    order: StrictInt = Field(ge=1)
    kind: Literal["CAUSAL_FACTOR", "UNSAFE_CONTROL_ACTION"]
    factor_id: StrictStr | None = Field(default=None, pattern=r"^CF-\d+$")
    structural_source_id: StrictStr = Field(min_length=1)


class UnsafeOutcome(ImmutableModel):
    """The explicit semantic condition that makes the control action unsafe."""

    outcome_id: StrictStr = Field(min_length=1, pattern=r"^OUTCOME-[A-Za-z0-9._-]+$")
    control_action_id: StrictStr = Field(min_length=1)
    uca_type: UCAType
    condition: SemanticCondition
    semantic_binding_required: StrictBool
    hazard_refs: tuple[StrictStr, ...] = ()
    constraint_refs: tuple[StrictStr, ...] = ()

    @field_validator("hazard_refs", "constraint_refs", mode="before")
    @classmethod
    def _refs_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("outcome references must be arrays")
        return tuple(value)

    @model_validator(mode="after")
    def _check_condition_compatibility(self) -> UnsafeOutcome:
        _validate_uca_condition_type(self.uca_type, self.condition.type)
        _validate_outcome_binding_state(self.semantic_binding_required, self.condition)
        _validate_outcome_action(self.control_action_id, self.condition)
        return self


_UCA_CONDITION_TYPES = {
    "NOT_PROVIDED": frozenset({"action_presence"}),
    "INCORRECT": frozenset({"action_value", "state_value"}),
    "WRONG_TIMING": frozenset({"ordering", "delay", "window", "absence"}),
    "WRONG_DURATION": frozenset({"duration"}),
}
_FACTOR_NAMESPACE = {
    "PROCESS_MODEL_FLAW": "PM",
    "FEEDBACK_DELAY": "FB",
    "SENSOR_ANOMALY": "FB",
    "ACTUATOR_ANOMALY": "CA",
}
_FACTOR_REFERENCE = re.compile(r"^(?:PM|FB|CA)-\d+(?:-\d+)?$")


def _validate_uca_condition_type(uca_type: UCAType, condition_type: str) -> None:
    if condition_type not in _UCA_CONDITION_TYPES[uca_type]:
        raise ValueError(
            f"condition type {condition_type!r} is incompatible with UCA {uca_type!r}"
        )


def _validate_factor_namespace(kind: CausalFactorKind, source_id: str) -> None:
    if not _FACTOR_REFERENCE.fullmatch(source_id) or not source_id.startswith(
        f"{_FACTOR_NAMESPACE[kind]}-"
    ):
        raise ValueError("structural_source_id does not use the namespace required by factor kind")


def _validate_factor_evidence(
    status: CausalEvidenceStatus,
    capability_refs: tuple[str, ...],
    access_refs: tuple[str, ...],
    bounded_assumption: str | None,
) -> None:
    _validate_capability_evidence(status, capability_refs, access_refs)
    _validate_assumption_evidence(status, bounded_assumption)


def _validate_capability_evidence(
    status: CausalEvidenceStatus,
    capability_refs: tuple[str, ...],
    access_refs: tuple[str, ...],
) -> None:
    if status == "reachable_capability":
        if not capability_refs or not access_refs:
            raise ValueError("reachable_capability factors require capability and access refs")
    elif capability_refs or access_refs:
        raise ValueError("capability/access refs require reachable_capability evidence")


def _validate_assumption_evidence(
    status: CausalEvidenceStatus, bounded_assumption: str | None
) -> None:
    if status == "bounded_assumption" and (
        bounded_assumption is None or not bounded_assumption.strip()
    ):
        raise ValueError("bounded_assumption evidence requires bounded_assumption text")
    if status != "bounded_assumption" and bounded_assumption is not None:
        raise ValueError("bounded_assumption text requires bounded_assumption evidence")


def _validate_outcome_binding_state(
    semantic_binding_required: bool, condition: SemanticCondition
) -> None:
    if semantic_binding_required != bool(condition.placeholders()):
        raise ValueError("semantic_binding_required does not match condition placeholders")


def _validate_outcome_action(control_action_id: str, condition: SemanticCondition) -> None:
    condition_action = getattr(condition, "control_action_id", None)
    if condition_action and condition_action != control_action_id:
        raise ValueError("unsafe condition control_action_id does not match outcome")


class ExecutionRequirements(ImmutableModel):
    """Neutral adapter capabilities needed by the projection."""

    requires_multi_turn: StrictBool = False
    requires_tool_execution: StrictBool = False
    requires_persistent_state: StrictBool = False
    requires_multi_agent: StrictBool = False
    requires_real_clock: StrictBool = False
    requires_state_observation: StrictBool = False
    required_surface_categories: tuple[
        Literal[
            "external_input",
            "system_instruction",
            "tool_result",
            "tool_definition",
            "persistent_data",
            "agent_message",
            "environment_event",
        ],
        ...,
    ] = ()

    @field_validator("required_surface_categories", mode="before")
    @classmethod
    def _categories_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("required_surface_categories must be an array")
        return tuple(value)

    @model_validator(mode="after")
    def _unique_categories(self) -> ExecutionRequirements:
        if len(self.required_surface_categories) != len(set(self.required_surface_categories)):
            raise ValueError("required_surface_categories must be unique")
        return self


class TraceReferences(ImmutableModel):
    """Producer provenance retained for artifact traceability only."""

    obligation_ids: tuple[StrictStr, ...] = ()
    risk_ids: tuple[StrictStr, ...] = ()
    attack_pattern_ids: tuple[StrictStr, ...] = ()
    technique_ids: tuple[StrictStr, ...] = ()
    loss_ids: tuple[StrictStr, ...] = ()
    hazard_ids: tuple[StrictStr, ...] = ()
    constraint_ids: tuple[StrictStr, ...] = ()
    source_pins: Mapping[str, SHA256Digest]

    @field_validator(
        "obligation_ids",
        "risk_ids",
        "attack_pattern_ids",
        "technique_ids",
        "loss_ids",
        "hazard_ids",
        "constraint_ids",
        mode="before",
    )
    @classmethod
    def _trace_ids_as_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("trace references must be arrays")
        return tuple(value)

    @field_validator("source_pins", mode="before")
    @classmethod
    def _source_pins_mapping(cls, value: Any) -> Mapping[str, str]:
        if not isinstance(value, Mapping):
            raise TypeError("source_pins must be an object")
        frozen = freeze_value(value)
        expected = {
            "control_structure",
            "loss_analysis",
            "ica_enumeration",
            "scenario_context",
        }
        if set(frozen) != expected:
            raise ValueError("source_pins must contain exactly the four producer digests")
        return frozen


class ExecutionIntent(ImmutableModel):
    """Closed immutable pair-normalised execution intent.

    ``presentation_context`` is deliberately excluded from semantic digest
    calculations and planning.  It is supplied only to an optional authoring
    seam after readiness succeeds.
    """

    bundle_schema_version: Literal["stpa-execution-bundle-v1"] = "stpa-execution-bundle-v1"
    projection_schema_version: Literal["stpa-execution-projection-v2"] = (
        "stpa-execution-projection-v2"
    )
    bundle_digest: SHA256Digest
    projection_semantic_digest: SHA256Digest
    run_id: StrictStr = Field(min_length=1)
    scenario_id: StrictStr = Field(min_length=1)
    candidate_id: StrictStr = Field(min_length=1)
    ica_slot_id: StrictStr = Field(min_length=1)
    ica_id: StrictStr = Field(min_length=1)
    controller_id: StrictStr = Field(min_length=1)
    control_action_id: StrictStr = Field(min_length=1)
    uca_type: UCAType
    causal_factors: tuple[CausalFactor, ...] = ()
    steps: tuple[ExecutionStep, ...] = ()
    unsafe_outcome: UnsafeOutcome
    execution_requirements: ExecutionRequirements
    trace_refs: TraceReferences
    presentation_context: Mapping[str, Any] = Field(default_factory=dict)
    source_file_digests: Mapping[str, SHA256Digest]

    @field_validator("presentation_context", mode="before")
    @classmethod
    def _context_mapping(cls, value: Any) -> Mapping[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError("presentation_context must be an object")
        return freeze_value(value)

    @field_validator("source_file_digests", mode="before")
    @classmethod
    def _source_file_digests_mapping(cls, value: Any) -> Mapping[str, str]:
        if not isinstance(value, Mapping):
            raise TypeError("source_file_digests must be an object")
        frozen = freeze_value(value)
        if set(frozen) != {"scenario", "projection"}:
            raise ValueError("source_file_digests must contain scenario and projection digests")
        return frozen

    @model_validator(mode="after")
    def _validate_identity_and_sequences(self) -> ExecutionIntent:
        _validate_intent_identity(self)
        _validate_intent_outcome(self)
        _validate_intent_sequences(self)
        _validate_intent_placeholder_refs(self)
        return self

    @classmethod
    def from_projection(
        cls,
        projection: Mapping[str, Any],
        *,
        bundle_digest: str,
        scenario_content_sha256: str,
        projection_content_sha256: str,
        presentation_context: Mapping[str, Any] | None = None,
    ) -> ExecutionIntent:
        """Create an inward intent from a validated projection document."""

        data = dict(projection)
        data["bundle_schema_version"] = "stpa-execution-bundle-v1"
        data["projection_schema_version"] = data.pop("schema_version")
        data["bundle_digest"] = bundle_digest
        data["projection_semantic_digest"] = data.pop("semantic_digest")
        data["presentation_context"] = presentation_context or {}
        data["source_file_digests"] = {
            "scenario": scenario_content_sha256,
            "projection": projection_content_sha256,
        }
        return cls.model_validate(data)


def _validate_intent_identity(value: ExecutionIntent) -> None:
    expected_candidate = f"EXEC:{value.controller_id}:{value.control_action_id}:{value.uca_type}"
    if value.candidate_id != expected_candidate:
        raise ValueError("candidate_id does not match controller, control action and UCA type")
    expected_slot = f"{value.controller_id}:{value.control_action_id}:{value.uca_type}"
    if value.ica_slot_id != expected_slot:
        raise ValueError("ica_slot_id does not match controller, control action and UCA type")
    if not value.ica_id.startswith(f"{value.ica_slot_id}:"):
        raise ValueError("ica_id does not belong to ica_slot_id")


def _validate_intent_outcome(value: ExecutionIntent) -> None:
    if value.unsafe_outcome.control_action_id != value.control_action_id:
        raise ValueError("unsafe outcome control action does not match intent")
    if value.unsafe_outcome.uca_type != value.uca_type:
        raise ValueError("unsafe outcome UCA type does not match intent")


def _validate_intent_sequences(value: ExecutionIntent) -> None:
    if not value.causal_factors:
        raise ValueError("published execution intent requires causal_factors")
    if not value.steps:
        raise ValueError("published execution intent requires ordered steps")
    _validate_factor_sequence(value.causal_factors)
    _validate_step_sequence(value.steps)
    _validate_final_uca_step(value.steps, value.control_action_id)
    _validate_causal_step_sequence(value.causal_factors, value.steps)


def _validate_factor_sequence(factors: tuple[CausalFactor, ...]) -> None:
    for index, factor in enumerate(factors, start=1):
        if factor.factor_id != f"CF-{index}" or factor.order != index:
            raise ValueError("causal factors must have contiguous CF-* IDs and order")


def _validate_step_sequence(steps: tuple[ExecutionStep, ...]) -> None:
    for index, step in enumerate(steps, start=1):
        if step.step_id != f"S-{index}" or step.order != index:
            raise ValueError("steps must have contiguous S-* IDs and order")


def _validate_final_uca_step(steps: tuple[ExecutionStep, ...], control_action_id: str) -> None:
    final = steps[-1]
    if final.kind != "UNSAFE_CONTROL_ACTION" or final.structural_source_id != control_action_id:
        raise ValueError("the final step must be the target unsafe control action")
    if final.factor_id is not None:
        raise ValueError("unsafe control action step must not name a causal factor")


def _validate_causal_step_sequence(
    factors: tuple[CausalFactor, ...], steps: tuple[ExecutionStep, ...]
) -> None:
    causal_steps = steps[:-1]
    if len(causal_steps) != len(factors):
        raise ValueError("each causal factor must have exactly one causal-factor step")
    for factor, step in zip(factors, causal_steps, strict=True):
        _validate_causal_step(factor, step)


def _validate_causal_step(factor: CausalFactor, step: ExecutionStep) -> None:
    if step.kind != "CAUSAL_FACTOR" or step.factor_id != factor.factor_id:
        raise ValueError("causal-factor steps must map one-to-one in factor order")
    if step.structural_source_id != factor.structural_source_id:
        raise ValueError("causal-factor step source must match its factor")


def _validate_intent_placeholder_refs(value: ExecutionIntent) -> None:
    refs = _intent_placeholder_refs(value)
    if len(refs) != len(set(refs)):
        raise ValueError("semantic binding references must be unique within a projection")


def _intent_placeholder_refs(value: ExecutionIntent) -> tuple[str, ...]:
    factor_refs = tuple(
        placeholder.binding_ref
        for factor in value.causal_factors
        if factor.temporal_condition is not None
        for placeholder in factor.temporal_condition.placeholders()
    )
    outcome_refs = tuple(
        placeholder.binding_ref for placeholder in value.unsafe_outcome.condition.placeholders()
    )
    return factor_refs + outcome_refs


__all__ = [
    "CONDITION_TYPES",
    "OPERATORS",
    "SURFACE_CATEGORIES",
    "UCAType",
    "CausalFactor",
    "ExecutionIntent",
    "ExecutionRequirements",
    "ExecutionStep",
    "SemanticBindingPlaceholder",
    "SemanticCondition",
    "SemanticValue",
    "TraceReferences",
    "UnsafeOutcome",
]
