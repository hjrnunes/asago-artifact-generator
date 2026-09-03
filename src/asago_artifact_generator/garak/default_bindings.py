"""Deterministic Garak-owned completion of routine runtime bindings."""

from __future__ import annotations

from collections.abc import Iterable

from ..models.execution_case import BoundExecutionCase
from ..models.execution_classification import (
    ExecutionTargetProfile,
    ResolvedExecutionBinding,
    TargetProfileOperation,
    TargetProfileResource,
)
from ..models.execution_intent import (
    AdversarialStimulusRequirement,
    ExecutionIntent,
    ExecutionStep,
)
from ..models.runtime_binding import (
    AdversarialStimulusBinding,
    ControlActionBinding,
    ObservationBinding,
    ReviewEvidence,
    RuntimeBindingSet,
    SurfaceBinding,
)
from .capabilities import GARAK_ADAPTER_VERSION


def complete_garak_runtime_bindings(
    execution_case: BoundExecutionCase,
    explicit: RuntimeBindingSet | None = None,
    target_profile: ExecutionTargetProfile | None = None,
) -> RuntimeBindingSet:
    """Fill only mechanics owned by the Garak conversation adapter.

    Explicit values retain authority for their exact key.  The adapter never
    invents semantic placeholder values, target resources, carrier arguments,
    clocks, state channels, credentials, or endpoints.
    """

    _require_inputs(execution_case, explicit, target_profile)
    if explicit is not None and not _explicit_is_usable(execution_case, explicit):
        return explicit
    review = _review(execution_case, explicit)
    surfaces = _merge_by(
        _explicit_values(explicit, "surface_bindings"),
        _default_surfaces(execution_case),
        lambda item: item.source_ref,
    )
    actions = _merge_by(
        _explicit_values(explicit, "control_action_bindings"),
        _default_actions(execution_case, target_profile),
        lambda item: item.control_action_id,
    )
    observations = _merge_by(
        _explicit_values(explicit, "observation_bindings"),
        _default_observations(execution_case),
        lambda item: item.condition_ref,
    )
    stimuli = _merge_by(
        _explicit_values(explicit, "stimulus_bindings"),
        _default_stimuli(execution_case, review),
        lambda item: (item.stimulus_id, item.content_slot_id),
    )
    return RuntimeBindingSet.create(
        binding_set_id=_binding_set_id(execution_case, explicit),
        projection_semantic_digest=execution_case.intent.projection_semantic_digest,
        target_environment_id=_environment_id(execution_case, explicit),
        review=review,
        semantic_bindings=_explicit_values(explicit, "semantic_bindings"),
        surface_bindings=surfaces,
        control_action_bindings=actions,
        observation_bindings=observations,
        stimulus_bindings=stimuli,
        clock_binding=getattr(explicit, "clock_binding", None),
    )


def _explicit_is_usable(
    execution_case: BoundExecutionCase,
    explicit: RuntimeBindingSet,
) -> bool:
    try:
        explicit.verify_digest()
    except ValueError:
        return False
    return explicit.projection_semantic_digest == execution_case.intent.projection_semantic_digest


def _explicit_values(
    explicit: RuntimeBindingSet | None,
    field_name: str,
) -> tuple:
    return getattr(explicit, field_name) if explicit is not None else ()


def _review(
    execution_case: BoundExecutionCase,
    explicit: RuntimeBindingSet | None,
) -> ReviewEvidence:
    return explicit.review if explicit is not None else _adapter_evidence(execution_case)


def _require_inputs(
    execution_case: BoundExecutionCase,
    explicit: RuntimeBindingSet | None,
    target_profile: ExecutionTargetProfile | None,
) -> None:
    if not isinstance(execution_case, BoundExecutionCase):
        raise TypeError("Garak binding completion requires a BoundExecutionCase")
    execution_case.assert_integrity()
    _require_optional_type(explicit, RuntimeBindingSet, "explicit bindings")
    _require_optional_type(target_profile, ExecutionTargetProfile, "target_profile")
    _verify_profile(target_profile)
    _verify_profile_matches_case(execution_case, target_profile)


def _require_optional_type(value: object | None, expected: type, label: str) -> None:
    if value is not None and not isinstance(value, expected):
        raise TypeError(f"{label} must be a {expected.__name__}")


def _verify_profile(target_profile: ExecutionTargetProfile | None) -> None:
    if target_profile is not None:
        target_profile.assert_integrity()


def _verify_profile_matches_case(
    execution_case: BoundExecutionCase,
    target_profile: ExecutionTargetProfile | None,
) -> None:
    expected = execution_case.target_profile_digest
    if target_profile is not None and expected is not None:
        if target_profile.semantic_digest != expected:
            raise ValueError("target profile does not match the bound execution case")


def _adapter_evidence(execution_case: BoundExecutionCase) -> ReviewEvidence:
    return ReviewEvidence(
        reviewed_by=f"platform:{GARAK_ADAPTER_VERSION}",
        reviewed_at="deterministic",
        rationale="Routine chat surfaces and observers are fixed by the Garak adapter.",
        evidence_refs=(f"bound-case:{execution_case.case_digest}",),
    )


def _binding_set_id(
    execution_case: BoundExecutionCase,
    explicit: RuntimeBindingSet | None,
) -> str:
    suffix = f":{execution_case.case_digest[:16]}"
    if explicit is None:
        return f"garak-defaults{suffix}"
    if explicit.binding_set_id.endswith(suffix):
        return explicit.binding_set_id
    return f"{explicit.binding_set_id}{suffix}"


def _environment_id(
    execution_case: BoundExecutionCase,
    explicit: RuntimeBindingSet | None,
) -> str:
    if explicit is not None:
        return explicit.target_environment_id
    return execution_case.target_environment_id or "garak:model"


def _merge_by(explicit: Iterable, defaults: Iterable, key) -> tuple:
    values = {key(item): item for item in defaults}
    values.update({key(item): item for item in explicit})
    return tuple(values[item] for item in sorted(values, key=str))


def _selected_stimulus_steps(
    execution_case: BoundExecutionCase,
) -> dict[str, ExecutionStep]:
    intent = execution_case.intent
    steps_by_factor = {
        step.factor_id: step
        for step in intent.steps
        if step.kind == "CAUSAL_FACTOR" and step.factor_id is not None
    }
    return {
        item.stimulus_id: steps_by_factor[item.factor_id]
        for item in intent.stimulus_requirements
        if item.factor_id in steps_by_factor
    }


def _default_surfaces(execution_case: BoundExecutionCase) -> tuple[SurfaceBinding, ...]:
    intent = execution_case.intent
    stimulus_steps = _selected_stimulus_steps(execution_case)
    values = tuple(
        surface
        for requirement in intent.stimulus_requirements
        if (
            surface := _stimulus_surface(
                requirement.delivery_class,
                stimulus_steps.get(requirement.stimulus_id),
            )
        )
        is not None
    )
    target = _target_surface(intent)
    return _unique_surfaces((*values, *((target,) if target is not None else ())))


def _stimulus_surface(
    delivery_class: object,
    step: ExecutionStep | None,
) -> SurfaceBinding | None:
    if delivery_class not in {"direct_prompt", "conversation_context"} or step is None:
        return None
    return SurfaceBinding(
        source_ref=step.structural_source_id,
        surface="user_turn",
        locator="conversation.messages",
        writable=True,
    )


def _target_surface(intent: ExecutionIntent) -> SurfaceBinding | None:
    surface = {
        "model_output": "assistant_turn",
        "tool_call": "tool_call",
    }.get(intent.execution_contract.action_kind.value)
    if surface is None:
        return None
    locators = {"assistant_turn": "target.response", "tool_call": "target.tool_calls"}
    return SurfaceBinding(
        source_ref=intent.control_action_id,
        surface=surface,
        locator=locators[surface],
        writable=False,
    )


def _unique_surfaces(values: tuple[SurfaceBinding, ...]) -> tuple[SurfaceBinding, ...]:
    by_source = {item.source_ref: item for item in values}
    return tuple(by_source[key] for key in sorted(by_source))


def _default_stimuli(
    execution_case: BoundExecutionCase,
    review: ReviewEvidence,
) -> tuple[AdversarialStimulusBinding, ...]:
    steps = _selected_stimulus_steps(execution_case)
    return tuple(
        stimulus
        for requirement in execution_case.intent.stimulus_requirements
        if (
            stimulus := _default_stimulus(
                requirement,
                steps.get(requirement.stimulus_id),
                review,
            )
        )
        is not None
    )


def _default_stimulus(
    requirement: AdversarialStimulusRequirement,
    step: ExecutionStep | None,
    review: ReviewEvidence,
) -> AdversarialStimulusBinding | None:
    sources = {
        "direct_prompt": "user_authored",
        "conversation_context": "conversation_history",
    }
    source_kind = sources.get(requirement.delivery_class)
    if source_kind is None or step is None:
        return None
    return AdversarialStimulusBinding(
        stimulus_id=requirement.stimulus_id,
        projection_step_id=step.step_id,
        factor_id=requirement.factor_id,
        content_slot_id=f"stimulus:{requirement.stimulus_id}",
        delivery_class=requirement.delivery_class,
        surface="user_turn",
        source_kind=source_kind,
        review=review,
    )


def _default_actions(
    execution_case: BoundExecutionCase,
    target_profile: ExecutionTargetProfile | None,
) -> tuple[ControlActionBinding, ...]:
    intent = execution_case.intent
    action_kind = intent.execution_contract.action_kind
    if action_kind == "model_output":
        return (
            ControlActionBinding(
                control_action_id=intent.control_action_id,
                adapter_operation="chat_completion",
            ),
        )
    if action_kind != "tool_call":
        return ()
    target = _resolved_target_operation(execution_case, target_profile)
    if target is None:
        return ()
    resource, operation = target
    return (
        ControlActionBinding(
            control_action_id=intent.control_action_id,
            adapter_operation="tool_call",
            tool_name=operation.operation_id,
            tool_schema=resource.interface_schema,
        ),
    )


def _resolved_target_operation(
    execution_case: BoundExecutionCase,
    profile: ExecutionTargetProfile | None,
) -> tuple[TargetProfileResource, TargetProfileOperation] | None:
    if profile is None:
        return None
    target_binding = _target_binding(execution_case)
    return _resolved_profile_target(profile, target_binding)


def _resolved_profile_target(
    profile: ExecutionTargetProfile,
    target_binding: ResolvedExecutionBinding | None,
) -> tuple[TargetProfileResource, TargetProfileOperation] | None:
    if target_binding is None:
        return None
    resource = _profile_resource(profile, target_binding.resource_id)
    if resource is None:
        return None
    operation = _profile_operation(resource, target_binding.operation_id)
    return (resource, operation) if operation is not None else None


def _target_binding(
    execution_case: BoundExecutionCase,
) -> ResolvedExecutionBinding | None:
    requirements = {
        item.requirement_id: item
        for item in execution_case.intent.execution_contract.resource_requirements
    }
    return next(
        (
            item
            for item in execution_case.resolved_bindings
            if requirements[item.requirement_id].purpose == "target_action"
        ),
        None,
    )


def _profile_resource(
    profile: ExecutionTargetProfile,
    resource_id: str,
) -> TargetProfileResource | None:
    return next(
        (item for item in profile.resources if item.resource_id == resource_id),
        None,
    )


def _profile_operation(
    resource: TargetProfileResource,
    operation_id: str,
) -> TargetProfileOperation | None:
    return next(
        (item for item in resource.operations if item.operation_id == operation_id),
        None,
    )


def _default_observations(
    execution_case: BoundExecutionCase,
) -> tuple[ObservationBinding, ...]:
    intent = execution_case.intent
    condition = intent.unsafe_outcome.condition
    if intent.execution_contract.action_kind != "model_output":
        return ()
    if condition.type not in {"action_value", "action_presence"}:
        return ()
    placeholders = condition.placeholders()
    expected_from = (
        f"semantic_binding:{placeholders[0].binding_ref}" if placeholders else "projection"
    )
    return (
        ObservationBinding(
            condition_ref=intent.unsafe_outcome.outcome_id,
            observer_kind="output_text",
            event_source="target.response",
            semantic_property=getattr(condition, "property", "action_presence"),
            comparison=getattr(condition, "operator", "equals"),
            expected_from=expected_from,
        ),
    )


__all__ = ["complete_garak_runtime_bindings"]
