"""Pure runtime binding and platform-readiness planning."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ..models._base import freeze_value
from ..models.execution_case import BoundExecutionCase
from ..models.execution_intent import (
    ExecutionIntent,
    SemanticBindingPlaceholder,
    SemanticCondition,
)
from ..models.readiness import (
    ExecutionPlanResult,
    ObserverPlan,
    PlanStep,
    PlatformCapabilities,
    ReadinessDiagnostic,
    ReadyExecutionPlan,
    ResolvedSemanticBinding,
    StimulusPlan,
)
from ..models.runtime_binding import (
    AdversarialStimulusBinding,
    ControlActionBinding,
    RuntimeBindingSet,
    SurfaceBinding,
)


def _diagnostic(
    axis: str,
    code: str,
    message: str,
    path: str = "",
) -> ReadinessDiagnostic:
    return ReadinessDiagnostic(axis=axis, code=code, message=message, path=path)


def _placeholder_requirements(
    intent: ExecutionIntent,
) -> tuple[tuple[str, SemanticBindingPlaceholder], ...]:
    required: list[tuple[str, SemanticBindingPlaceholder]] = []
    for factor in intent.causal_factors:
        if factor.temporal_condition is not None:
            required.extend(
                (factor.factor_id, placeholder)
                for placeholder in factor.temporal_condition.placeholders()
            )
    required.extend(
        (intent.unsafe_outcome.outcome_id, placeholder)
        for placeholder in intent.unsafe_outcome.condition.placeholders()
    )
    return tuple(required)


def _value_matches_placeholder(value: Any, placeholder: SemanticBindingPlaceholder) -> bool:
    if not _placeholder_type_matches(value, placeholder.value_type):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return _placeholder_bounds_match(value, placeholder)


def _placeholder_type_matches(value: Any, value_type: str) -> bool:
    if value_type == "number":
        return type(value) in {int, float}
    return type(value) is {"string": str, "integer": int, "boolean": bool}.get(value_type)


def _placeholder_bounds_match(value: Any, placeholder: SemanticBindingPlaceholder) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return True
    if placeholder.minimum is not None and value < placeholder.minimum:
        return False
    return not (placeholder.maximum is not None and value > placeholder.maximum)


def _resolve_semantic_bindings(
    intent: ExecutionIntent,
    bindings: RuntimeBindingSet | None,
    diagnostics: list[ReadinessDiagnostic],
) -> tuple[str, tuple[ResolvedSemanticBinding, ...], dict[tuple[str, str], Any]]:
    required = _placeholder_requirements(intent)
    provided = _semantic_bindings_by_key(bindings)
    required_keys = {
        (condition_ref, placeholder.binding_ref) for condition_ref, placeholder in required
    }
    unsolicited = _diagnose_unsolicited_semantic_bindings(provided, required_keys, diagnostics)
    if not required:
        return ("incomplete" if unsolicited else "not_required"), (), {}
    resolved, values, complete = _resolve_required_semantic_bindings(
        required, provided, diagnostics
    )
    return ("complete" if complete and not unsolicited else "incomplete"), tuple(resolved), values


def _semantic_bindings_by_key(
    bindings: RuntimeBindingSet | None,
) -> dict[tuple[str, str], Any]:
    return {
        (item.condition_ref, item.binding_ref): item
        for item in (bindings.semantic_bindings if bindings else ())
    }


def _diagnose_unsolicited_semantic_bindings(
    provided: Mapping[tuple[str, str], Any],
    required_keys: set[tuple[str, str]],
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    unsolicited = False
    for key, item in provided.items():
        if key not in required_keys:
            unsolicited = True
            diagnostics.append(
                _diagnostic(
                    "semantic_binding",
                    "unsolicited_semantic_binding",
                    f"binding {item.condition_ref}/{item.binding_ref} is not a "
                    "declared placeholder",
                    "semantic_bindings",
                )
            )
    return unsolicited


def _resolve_required_semantic_bindings(
    required: tuple[tuple[str, SemanticBindingPlaceholder], ...],
    provided: Mapping[tuple[str, str], Any],
    diagnostics: list[ReadinessDiagnostic],
) -> tuple[list[ResolvedSemanticBinding], dict[tuple[str, str], Any], bool]:
    resolved: list[ResolvedSemanticBinding] = []
    values: dict[tuple[str, str], Any] = {}
    complete = True
    for condition_ref, placeholder in required:
        item = provided.get((condition_ref, placeholder.binding_ref))
        if item is None:
            complete = False
            _diagnose_missing_semantic_binding(condition_ref, placeholder, diagnostics)
            continue
        if not _value_matches_placeholder(item.value, placeholder):
            complete = False
            _diagnose_invalid_semantic_binding(condition_ref, placeholder, diagnostics)
            continue
        resolved.append(
            ResolvedSemanticBinding(
                condition_ref=condition_ref,
                binding_ref=placeholder.binding_ref,
                value=item.value,
            )
        )
        values[(condition_ref, placeholder.binding_ref)] = item.value
    return resolved, values, complete


def _diagnose_missing_semantic_binding(
    condition_ref: str,
    placeholder: SemanticBindingPlaceholder,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    diagnostics.append(
        _diagnostic(
            "semantic_binding",
            "semantic_binding_missing",
            f"missing reviewed value for placeholder {placeholder.binding_ref}",
            f"{condition_ref}.{placeholder.binding_ref}",
        )
    )


def _diagnose_invalid_semantic_binding(
    condition_ref: str,
    placeholder: SemanticBindingPlaceholder,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    diagnostics.append(
        _diagnostic(
            "semantic_binding",
            "semantic_binding_value_invalid",
            f"value for {placeholder.binding_ref} does not satisfy its "
            f"{placeholder.value_type} constraint",
            f"{condition_ref}.{placeholder.binding_ref}",
        )
    )


def _required_source_refs(intent: ExecutionIntent) -> tuple[str, ...]:
    """Return only sources actively delivered or invoked by this plan."""
    selected_factors = {item.factor_id for item in intent.stimulus_requirements}
    refs = [
        step.structural_source_id
        for step in intent.steps
        if step.factor_id in selected_factors or step.kind == "UNSAFE_CONTROL_ACTION"
    ]
    return tuple(dict.fromkeys(refs))


def _exported_source_refs(intent: ExecutionIntent) -> set[str]:
    return {step.structural_source_id for step in intent.steps}


def _resolve_surfaces(
    intent: ExecutionIntent,
    bindings: RuntimeBindingSet | None,
    capabilities: PlatformCapabilities | None,
    diagnostics: list[ReadinessDiagnostic],
) -> tuple[str, dict[str, SurfaceBinding]]:
    source_refs = set(_required_source_refs(intent))
    supplied = {item.source_ref: item for item in (bindings.surface_bindings if bindings else ())}
    _diagnose_unsolicited_surfaces(bindings, _exported_source_refs(intent), diagnostics)
    complete = True
    for source_ref in source_refs:
        complete = (
            _resolve_surface(
                source_ref,
                supplied.get(source_ref),
                capabilities,
                diagnostics,
                target_output=source_ref == intent.control_action_id,
            )
            and complete
        )
    return ("complete" if complete else "incomplete"), supplied


def _diagnose_unsolicited_surfaces(
    bindings: RuntimeBindingSet | None,
    source_refs: set[str],
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    for item in bindings.surface_bindings if bindings else ():
        if item.source_ref not in source_refs:
            diagnostics.append(
                _diagnostic(
                    "runtime_binding",
                    "unsolicited_surface_binding",
                    f"surface binding {item.source_ref} is not an exported projection source",
                    "surface_bindings",
                )
            )


def _resolve_surface(
    source_ref: str,
    item: SurfaceBinding | None,
    capabilities: PlatformCapabilities | None,
    diagnostics: list[ReadinessDiagnostic],
    *,
    target_output: bool = False,
) -> bool:
    if item is None:
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "surface_binding_missing",
                f"no runtime surface is bound for {source_ref}",
                source_ref,
            )
        )
        return False
    writable = target_output or _validate_surface_writable(source_ref, item, diagnostics)
    if not target_output:
        _diagnose_unsupported_surface(source_ref, item, capabilities, diagnostics)
    return writable


def _validate_surface_writable(
    source_ref: str, item: SurfaceBinding, diagnostics: list[ReadinessDiagnostic]
) -> bool:
    if item.writable:
        return True
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "surface_not_writable",
            f"surface {item.surface} for {source_ref} is not declared writable",
            source_ref,
        )
    )
    return False


def _diagnose_unsupported_surface(
    source_ref: str,
    item: SurfaceBinding,
    capabilities: PlatformCapabilities | None,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    if capabilities and not capabilities.supports_surface(item.surface):
        diagnostics.append(
            _diagnostic(
                "platform_support",
                "surface_unsupported",
                f"platform {capabilities.platform} cannot write {item.surface}",
                source_ref,
            )
        )


def _resolve_action(
    intent: ExecutionIntent,
    bindings: RuntimeBindingSet | None,
    capabilities: PlatformCapabilities | None,
    diagnostics: list[ReadinessDiagnostic],
) -> ControlActionBinding | None:
    supplied = {
        item.control_action_id: item
        for item in (bindings.control_action_bindings if bindings else ())
    }
    _diagnose_unsolicited_actions(bindings, intent.control_action_id, diagnostics)
    action = supplied.get(intent.control_action_id)
    if action is None:
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "control_action_binding_missing",
                f"no adapter operation is bound for {intent.control_action_id}",
                intent.control_action_id,
            )
        )
        return None
    _diagnose_action_support(intent, action, capabilities, diagnostics)
    _diagnose_tool_action_fields(intent, action, diagnostics)
    return action


def _diagnose_unsolicited_actions(
    bindings: RuntimeBindingSet | None,
    control_action_id: str,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    for item in bindings.control_action_bindings if bindings else ():
        if item.control_action_id != control_action_id:
            diagnostics.append(
                _diagnostic(
                    "runtime_binding",
                    "unsolicited_control_action_binding",
                    f"control action binding {item.control_action_id} is not the "
                    "projection target",
                    "control_action_bindings",
                )
            )


def _diagnose_action_support(
    intent: ExecutionIntent,
    action: ControlActionBinding,
    capabilities: PlatformCapabilities | None,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    if capabilities and action.adapter_operation not in capabilities.invocable_operations:
        diagnostics.append(
            _diagnostic(
                "platform_support",
                "adapter_operation_unsupported",
                f"platform {capabilities.platform} does not advertise {action.adapter_operation}",
                intent.control_action_id,
            )
        )


def _diagnose_tool_action_fields(
    intent: ExecutionIntent,
    action: ControlActionBinding,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    if action.adapter_operation != "tool_call":
        return
    if not action.tool_name:
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "tool_name_missing",
                "tool_call operation requires an explicit tool name",
                intent.control_action_id,
            )
        )
    if not action.tool_schema:
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "tool_schema_missing",
                "tool_call operation requires an explicit tool schema",
                intent.control_action_id,
            )
        )


def _diagnose_surface_action_pair(
    intent: ExecutionIntent,
    surfaces: Mapping[str, SurfaceBinding],
    action: ControlActionBinding | None,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    if action is None:
        return
    surface = surfaces.get(intent.control_action_id)
    if surface is None or _surface_action_pair_is_compatible(
        surface.surface, action.adapter_operation
    ):
        return
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "surface_action_incompatible",
            f"surface {surface.surface} is incompatible with {action.adapter_operation}",
            intent.control_action_id,
        )
    )


def _surface_action_pair_is_compatible(surface: str, operation: str) -> bool:
    if operation == "tool_call":
        return surface in {"tool_call", "environment_event"}
    return surface != "tool_call"


def _diagnose_action_semantics(
    intent: ExecutionIntent,
    action: ControlActionBinding | None,
    bindings: RuntimeBindingSet | None,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    """A convenient adapter cannot substitute a different observation boundary."""
    kind = intent.execution_contract.action_kind.value
    operation = action.adapter_operation if action else None
    if _action_operation_mismatch(kind, operation):
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "action_semantics_mismatch",
                f"producer action {kind} cannot be rebound as {operation}; "
                "retain its actual event boundary",
                intent.control_action_id,
            )
        )
    for observer in bindings.observation_bindings if bindings else ():
        if observer.condition_ref == intent.unsafe_outcome.outcome_id:
            _diagnose_output_observation(kind, observer, diagnostics)


def _action_operation_mismatch(kind: str, operation: str | None) -> bool:
    if operation is None:
        return False
    required = {"model_output": "chat_completion", "tool_call": "tool_call"}.get(kind)
    if required is not None:
        return operation != required
    return operation == "chat_completion"


def _diagnose_output_observation(kind, observer, diagnostics) -> None:
    if observer.observer_kind == "output_text" and kind != "model_output":
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "observation_boundary_mismatch",
                "final-response text cannot establish an internal message, "
                "operation or state event",
                observer.condition_ref,
            )
        )


def _expected_observer_kind(
    condition: SemanticCondition,
    action: ControlActionBinding | None,
) -> str:
    if (
        condition.type in {"action_value", "action_presence"}
        and action is not None
        and action.adapter_operation == "chat_completion"
    ):
        return "output_text"
    return {
        "ordering": "event_order",
        "delay": "elapsed_time",
        "duration": "duration",
        "window": "elapsed_time",
        "absence": "event_absence",
        "action_presence": "tool_call",
        "action_value": "tool_argument",
        "state_value": "state_value",
    }[condition.type]


def _resolve_observer(
    intent: ExecutionIntent,
    bindings: RuntimeBindingSet | None,
    capabilities: PlatformCapabilities | None,
    values: Mapping[tuple[str, str], Any],
    action: ControlActionBinding | None,
    diagnostics: list[ReadinessDiagnostic],
) -> tuple[str, tuple[ObserverPlan, ...]]:
    conditions = _required_observer_conditions(intent)
    supplied = _observations_by_ref(bindings)
    _diagnose_unsolicited_observations(bindings, conditions, diagnostics)
    plans: list[ObserverPlan] = []
    complete = True
    for condition_ref, condition in conditions:
        plan, valid = _resolve_one_observer(
            condition_ref,
            condition,
            supplied.get(condition_ref),
            capabilities,
            values,
            action,
            outcome=(
                intent.unsafe_outcome
                if condition_ref == intent.unsafe_outcome.outcome_id
                else None
            ),
            trace_refs=intent.trace_refs,
            diagnostics=diagnostics,
        )
        complete = valid and complete
        if plan is not None:
            plans.append(plan)
    return ("complete" if complete else "incomplete"), tuple(plans)


def _required_observer_conditions(
    intent: ExecutionIntent,
) -> list[tuple[str, SemanticCondition]]:
    selected_factors = {item.factor_id for item in intent.stimulus_requirements}
    conditions = [
        (factor.factor_id, factor.temporal_condition)
        for factor in intent.causal_factors
        if factor.temporal_condition is not None and factor.factor_id in selected_factors
    ]
    conditions.append((intent.unsafe_outcome.outcome_id, intent.unsafe_outcome.condition))
    return conditions


def _observations_by_ref(bindings: RuntimeBindingSet | None) -> dict[str, Any]:
    return {
        item.condition_ref: item for item in (bindings.observation_bindings if bindings else ())
    }


def _diagnose_unsolicited_observations(
    bindings: RuntimeBindingSet | None,
    conditions: list[tuple[str, SemanticCondition]],
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    condition_refs = {condition_ref for condition_ref, _ in conditions}
    for item in bindings.observation_bindings if bindings else ():
        if item.condition_ref not in condition_refs:
            diagnostics.append(
                _diagnostic(
                    "runtime_binding",
                    "unsolicited_observation_binding",
                    f"observation binding {item.condition_ref} is not a declared condition",
                    "observation_bindings",
                )
            )


def _validate_bound_observer(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    outcome: Any | None,
    action: ControlActionBinding | None,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    valid = _validate_observer(condition_ref, condition, observer, action, diagnostics)
    if not _validate_observer_proposition(condition_ref, observer, outcome, diagnostics):
        valid = False
    return valid


def _observer_lineage(
    outcome: Any | None,
    trace_refs: Any,
) -> tuple[str | None, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if outcome is None:
        return None, (), (), ()
    return (
        outcome.semantic_proposition,
        outcome.hazard_refs,
        outcome.constraint_refs,
        trace_refs.loss_ids,
    )


def _resolve_one_observer(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    capabilities: PlatformCapabilities | None,
    values: Mapping[tuple[str, str], Any],
    action: ControlActionBinding | None,
    outcome: Any | None,
    trace_refs: Any,
    diagnostics: list[ReadinessDiagnostic],
) -> tuple[ObserverPlan | None, bool]:
    if observer is None:
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "observation_binding_missing",
                f"no observer is bound for {condition_ref}",
                condition_ref,
            )
        )
        return None, False
    valid = _validate_bound_observer(
        condition_ref,
        condition,
        observer,
        outcome,
        action,
        diagnostics,
    )
    _diagnose_observer_support(condition_ref, observer, capabilities, diagnostics)
    semantic_proposition, hazard_refs, constraint_refs, loss_refs = _observer_lineage(
        outcome, trace_refs
    )
    return (
        ObserverPlan(
            condition_ref=condition_ref,
            condition_type=condition.type,
            observer_kind=observer.observer_kind,
            event_source=observer.event_source,
            semantic_property=observer.semantic_property,
            field_path=observer.field_path,
            comparison=observer.comparison,
            expected_from=observer.expected_from,
            expected=_condition_expected(condition, values, condition_ref),
            relation=(
                condition.relation
                if condition.type == "ordering" and condition.reference_argument is not None
                else None
            ),
            reference_tool=(
                condition.reference_tool
                if condition.type == "ordering" and condition.reference_argument is not None
                else None
            ),
            semantic_proposition=semantic_proposition,
            hazard_refs=hazard_refs,
            constraint_refs=constraint_refs,
            loss_refs=loss_refs,
        ),
        valid,
    )


def _validate_observer_proposition(
    condition_ref: str,
    observer: Any,
    outcome: Any | None,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    """Require producer meaning before creating an output-text plan."""

    if observer.observer_kind != "output_text":
        return True
    proposition = outcome.semantic_proposition if outcome is not None else None
    if proposition:
        return True
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "semantic_proposition_missing",
            "output-text observers require the producer semantic proposition",
            condition_ref,
        )
    )
    return False


def _validate_observer(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    action: ControlActionBinding | None,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    checks = (
        _validate_observer_kind(condition_ref, condition, observer, action, diagnostics),
        _validate_lifecycle_observation(condition_ref, condition, observer, diagnostics),
        _validate_observer_property(condition_ref, condition, observer, diagnostics),
        _validate_observer_reference(condition_ref, condition, observer, diagnostics),
        _validate_observer_field_path(condition_ref, condition, observer, diagnostics),
        _validate_observer_sources(condition_ref, condition, observer, diagnostics),
        _validate_observer_comparison(condition_ref, condition, observer, diagnostics),
    )
    return all(checks)


def _validate_observer_reference(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    """Keep an event_order observer on the producer's exact reference predicate."""

    if condition.type != "ordering" or condition.reference_argument is None:
        return True
    argument = condition.reference_argument
    if (
        observer.semantic_property == argument.property
        and observer.comparison == argument.operator
    ):
        return True
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "observer_reference_mismatch",
            "event_order observer must preserve the producer reference argument "
            "property and operator",
            condition_ref,
        )
    )
    return False


def _validate_lifecycle_observation(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    """Keep output text from masquerading as typed response absence evidence."""

    if not (
        condition.type == "action_presence"
        and getattr(condition, "expected", None) == "not_provided"
        and observer.observer_kind == "output_text"
    ):
        return True
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "lifecycle_observation_missing",
            "output_text cannot establish action absence; typed "
            "completion/timeout/error lifecycle observation is required",
            condition_ref,
        )
    )
    return False


def _validate_observer_comparison(condition_ref, condition, observer, diagnostics) -> bool:
    operator = getattr(condition, "operator", None)
    if operator is None or observer.comparison == operator:
        return True
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "observer_comparison_mismatch",
            "observer comparison must preserve the producer condition operator",
            condition_ref,
        )
    )
    return False


def _validate_observer_kind(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    action: ControlActionBinding | None,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    expected_kind = _expected_observer_kind(condition, action)
    if observer.observer_kind == expected_kind:
        return True
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "observer_condition_mismatch",
            f"condition {condition.type} requires {expected_kind}, got {observer.observer_kind}",
            condition_ref,
        )
    )
    return False


def _validate_observer_property(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    condition_property = getattr(condition, "property", None)
    if not condition_property or observer.semantic_property == condition_property:
        return True
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "observer_property_mismatch",
            "observer semantic_property must equal the producer condition property",
            condition_ref,
        )
    )
    return False


def _validate_observer_field_path(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    needs_path = (
        getattr(condition, "control_action_id", None) and observer.observer_kind == "tool_argument"
    )
    if not needs_path:
        return True
    if observer.field_path:
        return _validate_argument_field_path(condition_ref, condition, observer, diagnostics)
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "observer_field_path_missing",
            "tool_argument observers require an explicit field path",
            condition_ref,
        )
    )
    return False


def _validate_argument_field_path(condition_ref, condition, observer, diagnostics) -> bool:
    expected = f"arguments.{condition.property}"
    if observer.field_path == expected:
        return True
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "observer_field_path_mismatch",
            f"tool observer must read the producer property at {expected}",
            condition_ref,
        )
    )
    return False


def _validate_observer_sources(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    valid = True
    for placeholder in condition.placeholders():
        expected_ref = f"semantic_binding:{placeholder.binding_ref}"
        if observer.expected_from != expected_ref:
            valid = False
            diagnostics.append(
                _diagnostic(
                    "runtime_binding",
                    "observer_expected_source_mismatch",
                    f"observer must read {expected_ref}",
                    condition_ref,
                )
            )
    return valid


def _diagnose_observer_support(
    condition_ref: str,
    observer: Any,
    capabilities: PlatformCapabilities | None,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    if capabilities and not capabilities.supports_observer(observer.observer_kind):
        diagnostics.append(
            _diagnostic(
                "platform_support",
                "observer_unsupported",
                f"platform {capabilities.platform} does not advertise {observer.observer_kind}",
                condition_ref,
            )
        )


def _condition_expected(
    condition: SemanticCondition,
    values: Mapping[tuple[str, str], Any],
    condition_ref: str,
) -> Any:
    if condition.type in {"delay", "duration"}:
        field = "delay_ms" if condition.type == "delay" else "duration_ms"
        raw = getattr(condition, field)
    elif condition.type == "window":
        raw = {"from_ms": condition.window_from_ms, "to_ms": condition.window_to_ms}
    elif condition.type == "ordering" and condition.reference_argument is not None:
        raw = condition.reference_argument.expected
    else:
        raw = getattr(condition, "expected", None)
    return _replace_placeholder(raw, values, condition_ref)


def _replace_placeholder(
    value: Any,
    values: Mapping[tuple[str, str], Any],
    condition_ref: str,
) -> Any:
    if isinstance(value, SemanticBindingPlaceholder):
        return values.get((condition_ref, value.binding_ref), value)
    if isinstance(value, Mapping):
        return {
            key: _replace_placeholder(item, values, condition_ref) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_replace_placeholder(item, values, condition_ref) for item in value]
    return value


def _check_requirements(
    intent: ExecutionIntent,
    bindings: RuntimeBindingSet | None,
    capabilities: PlatformCapabilities | None,
    surfaces: Mapping[str, SurfaceBinding],
    action: ControlActionBinding | None,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    requirements = intent.execution_requirements
    if capabilities is None:
        _diagnose_indeterminate_capabilities(diagnostics)
        return
    _check_tool_execution_requirement(requirements.requires_tool_execution, action, diagnostics)
    _check_capability_requirements(requirements, capabilities, diagnostics)
    bound_surfaces = {item.surface for item in surfaces.values()}
    _check_surface_categories(
        requirements.required_surface_categories,
        _available_surface_categories(bound_surfaces, action),
        diagnostics,
    )
    _check_persistent_state_requirement(
        requirements.requires_persistent_state, bound_surfaces, diagnostics
    )
    _check_clock_requirement(requirements.requires_real_clock, bindings, diagnostics)


def _available_surface_categories(
    bound_surfaces: set[str], action: ControlActionBinding | None
) -> set[str]:
    """A fixed target declaration satisfies availability, not writable access.

    The compiler emits this exact bound schema in its tools list. Requiring a
    separate writable definition would turn ordinary tool invocation into an
    unsupported ability to modify that tool. Stimulus placement still validates
    its own surface binding independently.
    """
    available = set(bound_surfaces)
    if action is not None and action.adapter_operation == "tool_call":
        if action.tool_name and action.tool_schema:
            available.add("tool_definition")
    return available


def _resolve_stimuli(
    intent: ExecutionIntent,
    bindings: RuntimeBindingSet | None,
    capabilities: PlatformCapabilities,
    surfaces: Mapping[str, SurfaceBinding],
    diagnostics: list[ReadinessDiagnostic],
) -> tuple[str, tuple[StimulusPlan, ...]]:
    supplied = _stimulus_bindings_by_id(bindings)
    required = _stimulus_requirements_by_id(intent)
    plans: list[StimulusPlan] = []
    complete = not _diagnose_unsolicited_stimuli(supplied, required, diagnostics)
    plans, required_complete = _resolve_stimulus_plans(
        intent, supplied, capabilities, surfaces, diagnostics
    )
    complete = required_complete and complete
    return ("complete" if complete else "incomplete"), tuple(plans)


def _stimulus_bindings_by_id(
    bindings: RuntimeBindingSet | None,
) -> dict[str, AdversarialStimulusBinding]:
    return {item.stimulus_id: item for item in (bindings.stimulus_bindings if bindings else ())}


def _stimulus_requirements_by_id(
    intent: ExecutionIntent,
) -> dict[str, Any]:
    return {item.stimulus_id: item for item in intent.stimulus_requirements}


def _resolve_stimulus_plans(
    intent: ExecutionIntent,
    supplied: Mapping[str, AdversarialStimulusBinding],
    capabilities: PlatformCapabilities,
    surfaces: Mapping[str, SurfaceBinding],
    diagnostics: list[ReadinessDiagnostic],
) -> tuple[list[StimulusPlan], bool]:
    plans: list[StimulusPlan] = []
    complete = True
    for requirement in intent.stimulus_requirements:
        valid, plan = _resolve_one_stimulus(
            intent,
            requirement,
            supplied.get(requirement.stimulus_id),
            capabilities,
            surfaces,
            diagnostics,
        )
        complete = valid and complete
        if plan is not None:
            plans.append(plan)
    return plans, complete


def _diagnose_unsolicited_stimuli(
    supplied: Mapping[str, AdversarialStimulusBinding],
    required: Mapping[str, Any],
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    unsolicited = False
    for stimulus_id in sorted(set(supplied) - set(required)):
        unsolicited = True
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "unsolicited_stimulus_binding",
                f"stimulus binding {stimulus_id} is not declared by the projection",
                "stimulus_bindings",
            )
        )
    return unsolicited


def _resolve_one_stimulus(
    intent: ExecutionIntent,
    requirement: Any,
    binding: AdversarialStimulusBinding | None,
    capabilities: PlatformCapabilities,
    surfaces: Mapping[str, SurfaceBinding],
    diagnostics: list[ReadinessDiagnostic],
) -> tuple[bool, StimulusPlan | None]:
    if binding is None:
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "stimulus_binding_missing",
                f"no reviewed delivery is bound for {requirement.stimulus_id}",
                requirement.stimulus_id,
            )
        )
        return False, None
    valid = _validate_stimulus_binding(
        intent, requirement, binding, capabilities, surfaces, diagnostics
    )
    return (valid, _stimulus_plan(requirement, binding) if valid else None)


def _stimulus_plan(requirement: Any, binding: AdversarialStimulusBinding) -> StimulusPlan:
    return StimulusPlan(
        stimulus_id=binding.stimulus_id,
        projection_step_id=binding.projection_step_id,
        factor_id=binding.factor_id,
        content_slot_id=binding.content_slot_id,
        delivery_class=binding.delivery_class,
        surface=binding.surface,
        source_kind=binding.source_kind,
        carrier_tool_name=binding.carrier_tool_name,
        carrier_tool_description=binding.carrier_tool_description,
        carrier_tool_schema=binding.carrier_tool_schema,
        carrier_tool_arguments=binding.carrier_tool_arguments,
        intent=requirement.intent,
        desired_effect=requirement.desired_effect,
        turns=requirement.turns,
    )


def _validate_stimulus_binding(
    intent: ExecutionIntent,
    requirement: Any,
    binding: AdversarialStimulusBinding,
    capabilities: PlatformCapabilities,
    surfaces: Mapping[str, SurfaceBinding],
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    step = _stimulus_step(intent, binding)
    projection_valid = _validate_stimulus_projection(step, requirement, binding, diagnostics)
    surface_valid = _validate_stimulus_surface(step, binding, surfaces, diagnostics)
    platform_valid = _validate_stimulus_platform(binding, capabilities, diagnostics)
    return projection_valid and surface_valid and platform_valid


def _stimulus_step(intent: ExecutionIntent, binding: AdversarialStimulusBinding) -> Any:
    return next(
        (item for item in intent.steps if item.step_id == binding.projection_step_id), None
    )


def _validate_stimulus_projection(
    step: Any,
    requirement: Any,
    binding: AdversarialStimulusBinding,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    if _stimulus_projection_matches(step, requirement, binding):
        return True
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "stimulus_projection_mismatch",
            "stimulus binding must name one eligible causal-factor step",
            binding.stimulus_id,
        )
    )
    return False


def _stimulus_projection_matches(
    step: Any,
    requirement: Any,
    binding: AdversarialStimulusBinding,
) -> bool:
    if step is None:
        return False
    return (
        step.kind == "CAUSAL_FACTOR"
        and step.factor_id == binding.factor_id
        and binding.factor_id == requirement.factor_id
        and binding.delivery_class == requirement.delivery_class
    )


def _validate_stimulus_surface(
    step: Any,
    binding: AdversarialStimulusBinding,
    surfaces: Mapping[str, SurfaceBinding],
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    surface = surfaces.get(step.structural_source_id) if step is not None else None
    if surface is not None and surface.surface == binding.surface:
        return True
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "stimulus_surface_mismatch",
            "stimulus surface must equal the causal step's reviewed surface",
            binding.stimulus_id,
        )
    )
    return False


def _validate_stimulus_platform(
    binding: AdversarialStimulusBinding,
    capabilities: PlatformCapabilities,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    if capabilities.supports_surface(binding.surface):
        return True
    diagnostics.append(
        _diagnostic(
            "platform_support",
            "stimulus_surface_unsupported",
            f"platform {capabilities.platform} cannot deliver {binding.surface}",
            binding.stimulus_id,
        )
    )
    return False


def _diagnose_indeterminate_capabilities(
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    diagnostics.append(
        _diagnostic(
            "platform_support",
            "capabilities_indeterminate",
            "platform capabilities are unavailable",
        )
    )


def _check_tool_execution_requirement(
    required: bool,
    action: ControlActionBinding | None,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    if required and action is not None and action.adapter_operation != "tool_call":
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "tool_execution_operation_mismatch",
                "requires_tool_execution needs a tool_call adapter operation",
                "execution_requirements.requires_tool_execution",
            )
        )


def _check_capability_requirements(
    requirements: Any,
    capabilities: PlatformCapabilities,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    checks = (
        (
            requirements.requires_multi_turn,
            capabilities.supports_multi_turn,
            "multi_turn",
            "multi-turn execution",
        ),
        (
            requirements.requires_persistent_state,
            capabilities.supports_persistent_state,
            "persistent_state",
            "persistent state",
        ),
        (
            requirements.requires_multi_agent,
            capabilities.supports_multi_agent,
            "multi_agent",
            "multi-agent execution",
        ),
        (
            requirements.requires_real_clock,
            capabilities.supports_real_clock,
            "real_clock",
            "a real clock",
        ),
        (
            requirements.requires_state_observation,
            capabilities.supports_state_observation,
            "state_observation",
            "state observation",
        ),
    )
    for required, supported, code, label in checks:
        if required and not supported:
            diagnostics.append(
                _diagnostic(
                    "platform_support",
                    f"{code}_unsupported",
                    f"{capabilities.platform} does not support {label}",
                    "execution_requirements",
                )
            )


_CATEGORY_SURFACES: dict[str, set[str]] = {
    "external_input": {"user_turn", "tool_result", "agent_message"},
    "system_instruction": {"system_prompt"},
    "tool_result": {"tool_result"},
    "tool_definition": {"tool_definition"},
    "persistent_data": {"memory", "data_store"},
    "agent_message": {"agent_message"},
    "environment_event": {"environment_event"},
}


def _check_surface_categories(
    categories: tuple[str, ...],
    bound_surfaces: set[str],
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    for category in categories:
        if not bound_surfaces & _CATEGORY_SURFACES[category]:
            diagnostics.append(
                _diagnostic(
                    "runtime_binding",
                    "required_surface_category_missing",
                    f"no runtime surface is bound for required category {category}",
                    "execution_requirements.required_surface_categories",
                )
            )


def _check_persistent_state_requirement(
    required: bool,
    bound_surfaces: set[str],
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    if required and not bound_surfaces & {"memory", "data_store"}:
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "state_binding_missing",
                "persistent-state requirement needs a memory or data_store binding",
                "execution_requirements.requires_persistent_state",
            )
        )


def _check_clock_requirement(
    required: bool,
    bindings: RuntimeBindingSet | None,
    diagnostics: list[ReadinessDiagnostic],
) -> None:
    if required and (bindings is None or bindings.clock_binding is None):
        diagnostics.append(
            _diagnostic(
                "runtime_binding",
                "clock_binding_missing",
                "real-clock requirement needs an explicit clock binding",
                "clock_binding",
            )
        )


def _overall(
    source_status: str,
    semantic_status: str,
    runtime_status: str,
    platform_status: str,
) -> str:
    if source_status == "invalid":
        return "invalid"
    if semantic_status == "incomplete":
        return "needs_semantic_binding"
    if runtime_status == "incomplete":
        return "needs_runtime_binding"
    if platform_status != "supported":
        return "unsupported"
    return "ready"


def bind_and_plan(
    intent: BoundExecutionCase,
    bindings: RuntimeBindingSet | None,
    capabilities: PlatformCapabilities,
) -> ExecutionPlanResult:
    """Resolve reviewed bindings and adapter facts without side effects.

    The planner only reads already verified values.  It does not parse
    narrative, call a model, load plugins, access a provider, or write files.
    Every applicable diagnostic is retained even when overall precedence picks
    an earlier readiness axis.
    """

    _require_typed_inputs(intent, bindings, capabilities)
    execution_case = intent
    source_intent = execution_case.intent
    diagnostics: list[ReadinessDiagnostic] = []
    source_status = _binding_source_status(source_intent, bindings, diagnostics)
    semantic_status, resolved, values = _resolve_semantic_bindings(
        source_intent, bindings, diagnostics
    )
    surface_status, surfaces = _resolve_surfaces(
        source_intent, bindings, capabilities, diagnostics
    )
    action = _resolve_action(source_intent, bindings, capabilities, diagnostics)
    _diagnose_action_semantics(source_intent, action, bindings, diagnostics)
    _diagnose_surface_action_pair(source_intent, surfaces, action, diagnostics)
    observer_status, observers = _resolve_observer(
        source_intent, bindings, capabilities, values, action, diagnostics
    )
    stimulus_status, stimuli = _resolve_stimuli(
        source_intent, bindings, capabilities, surfaces, diagnostics
    )
    _check_requirements(source_intent, bindings, capabilities, surfaces, action, diagnostics)
    runtime_status = _runtime_status(surface_status, observer_status, stimulus_status, diagnostics)
    platform_status = _platform_status(capabilities, diagnostics)
    overall = _overall(source_status, semantic_status, runtime_status, platform_status)
    if not _can_make_plan(overall, bindings, action):
        return _not_ready_result(
            source_status,
            semantic_status,
            runtime_status,
            platform_status,
            overall,
            diagnostics,
        )
    plan = _ready_plan(
        source_intent,
        bindings,
        capabilities,
        resolved,
        observers,
        stimuli,
        surfaces,
        execution_case=execution_case,
    )
    return ExecutionPlanResult(
        source_status=source_status,
        semantic_binding_status=semantic_status,
        runtime_binding_status=runtime_status,
        platform_support_status=platform_status,
        overall="ready",
        diagnostics=diagnostics,
        plan=plan,
    )


def _require_typed_inputs(
    intent: BoundExecutionCase,
    bindings: RuntimeBindingSet | None,
    capabilities: PlatformCapabilities,
) -> None:
    if not isinstance(intent, BoundExecutionCase):
        raise TypeError("bind_and_plan requires a BoundExecutionCase")
    if bindings is not None and not isinstance(bindings, RuntimeBindingSet):
        raise TypeError("bind_and_plan requires a RuntimeBindingSet, not a raw mapping")
    if not isinstance(capabilities, PlatformCapabilities):
        raise TypeError("bind_and_plan requires PlatformCapabilities, not a raw mapping")


def _binding_source_status(
    intent: ExecutionIntent,
    bindings: RuntimeBindingSet | None,
    diagnostics: list[ReadinessDiagnostic],
) -> str:
    if bindings is None:
        return "valid"
    status = "valid"
    if bindings.projection_semantic_digest != intent.projection_semantic_digest:
        status = "invalid"
        diagnostics.append(
            _diagnostic(
                "source",
                "binding_projection_digest_mismatch",
                "binding set targets a different projection semantic digest",
                "projection_semantic_digest",
            )
        )
    try:
        bindings.verify_digest()
    except ValueError as exc:
        status = "invalid"
        diagnostics.append(
            _diagnostic("source", "binding_digest_mismatch", str(exc), "semantic_digest")
        )
    return status


def _runtime_status(
    surface_status: str,
    observer_status: str,
    stimulus_status: str,
    diagnostics: list[ReadinessDiagnostic],
) -> str:
    if "incomplete" in {surface_status, observer_status, stimulus_status}:
        return "incomplete"
    if any(item.axis == "runtime_binding" for item in diagnostics):
        return "incomplete"
    return "complete"


def _platform_status(
    capabilities: PlatformCapabilities | None,
    diagnostics: list[ReadinessDiagnostic],
) -> str:
    if capabilities is None:
        return "indeterminate"
    if any(item.axis == "platform_support" for item in diagnostics):
        return "unsupported"
    return "supported"


def _can_make_plan(
    overall: str,
    bindings: RuntimeBindingSet | None,
    action: ControlActionBinding | None,
) -> bool:
    return overall == "ready" and bindings is not None and action is not None


def _not_ready_result(
    source_status: str,
    semantic_status: str,
    runtime_status: str,
    platform_status: str,
    overall: str,
    diagnostics: list[ReadinessDiagnostic],
) -> ExecutionPlanResult:
    return ExecutionPlanResult(
        source_status=source_status,
        semantic_binding_status=semantic_status,
        runtime_binding_status=runtime_status,
        platform_support_status=platform_status,
        overall=overall,
        diagnostics=diagnostics,
    )


def _ready_plan(
    intent: ExecutionIntent,
    bindings: RuntimeBindingSet,
    capabilities: PlatformCapabilities,
    resolved: tuple[ResolvedSemanticBinding, ...],
    observers: tuple[ObserverPlan, ...],
    stimuli: tuple[StimulusPlan, ...],
    surfaces: Mapping[str, SurfaceBinding],
    execution_case: BoundExecutionCase,
) -> ReadyExecutionPlan:
    steps, trace_map = _plan_steps(intent, surfaces, bindings, capabilities, stimuli)
    return ReadyExecutionPlan(
        bundle_digest=intent.bundle_digest,
        projection_semantic_digest=intent.projection_semantic_digest,
        scenario_content_sha256=intent.source_file_digests["scenario"],
        projection_content_sha256=intent.source_file_digests["projection"],
        binding_set_id=bindings.binding_set_id,
        binding_set_digest=bindings.semantic_digest,
        run_id=intent.run_id,
        scenario_id=intent.scenario_id,
        candidate_id=intent.candidate_id,
        ica_slot_id=intent.ica_slot_id,
        ica_id=intent.ica_id,
        controller_id=intent.controller_id,
        control_action_id=intent.control_action_id,
        uca_type=intent.uca_type,
        platform=capabilities.platform,
        adapter_version=capabilities.adapter_version,
        steps=steps,
        resolved_semantic_bindings=resolved,
        observers=observers,
        stimuli=stimuli,
        clock_binding=bindings.clock_binding,
        state_channels=_surface_refs(surfaces, {"memory", "data_store"}),
        agent_channels=_surface_refs(surfaces, {"agent_message"}),
        content_slots=_content_slots(steps),
        trace_map=freeze_value(trace_map),
        presentation_context=intent.presentation_context,
        case_id=execution_case.case_id,
        case_digest=execution_case.case_digest,
        execution_classification_digest=execution_case.execution_classification.classification_digest,
        binding_completeness=execution_case.binding_completeness.value,
        environment_basis=execution_case.environment_basis.value,
        profile_fit=execution_case.profile_fit.value,
        claim_scope=execution_case.claim_scope.value,
        source_binding_completeness=(
            execution_case.execution_classification.binding_completeness.value
        ),
        source_environment_basis=(execution_case.execution_classification.environment_basis.value),
        source_profile_fit=execution_case.execution_classification.profile_fit.value,
        source_claim_scope=execution_case.execution_classification.claim_scope.value,
        selected_profile_id=execution_case.selected_profile_id,
        selected_profile_basis=execution_case.selected_profile_basis,
        target_environment_id=execution_case.target_environment_id,
        target_profile_digest=execution_case.target_profile_digest,
        target_realization_digest=execution_case.target_realization_digest,
        inventory_authority=execution_case.inventory_authority,
        semantic_authority=execution_case.semantic_authority,
        selected_simulation_resources=execution_case.selected_simulation_resources,
    )


def _surface_refs(surfaces: Mapping[str, SurfaceBinding], allowed: set[str]) -> tuple[str, ...]:
    return tuple(source_ref for source_ref, item in surfaces.items() if item.surface in allowed)


def _content_slots(steps: tuple[PlanStep, ...]) -> tuple[str, ...]:
    return tuple(step.content_slot_id for step in steps if step.content_slot_id)


def _plan_steps(
    intent: ExecutionIntent,
    surfaces: Mapping[str, SurfaceBinding],
    bindings: RuntimeBindingSet,
    capabilities: PlatformCapabilities,
    stimuli: tuple[StimulusPlan, ...],
) -> tuple[tuple[PlanStep, ...], dict[str, Any]]:
    action = _action_for_plan(intent, bindings)
    stimulus_slots = {item.projection_step_id: item.content_slot_id for item in stimuli}
    steps = _build_plan_steps(intent, surfaces, stimulus_slots, action)
    return steps, _plan_trace(intent, steps)


def _build_plan_steps(
    intent: ExecutionIntent,
    surfaces: Mapping[str, SurfaceBinding],
    stimulus_slots: Mapping[str, str],
    action: ControlActionBinding,
) -> tuple[PlanStep, ...]:
    steps: list[PlanStep] = []
    for index, projection_step in enumerate(_active_steps(intent), start=1):
        step = _plan_step(
            f"plan-{index}",
            index,
            projection_step,
            surfaces[projection_step.structural_source_id],
            intent.control_action_id,
            action,
            stimulus_slots.get(projection_step.step_id),
        )
        steps.append(step)
    return tuple(steps)


def _plan_trace(
    intent: ExecutionIntent,
    steps: tuple[PlanStep, ...],
) -> dict[str, Any]:
    trace_map = {
        step.plan_step_id: {
            "projection_step_id": projection_step.step_id,
            "factor_id": projection_step.factor_id,
            "structural_source_id": projection_step.structural_source_id,
        }
        for step, projection_step in zip(steps, _active_steps(intent), strict=True)
    }
    trace_map["oracle-1"] = {
        "condition_ref": intent.unsafe_outcome.outcome_id,
        "condition_type": intent.unsafe_outcome.condition.type,
        "semantic_proposition": intent.unsafe_outcome.semantic_proposition,
        "hazard_refs": list(intent.unsafe_outcome.hazard_refs),
        "constraint_refs": list(intent.unsafe_outcome.constraint_refs),
        "loss_refs": list(intent.trace_refs.loss_ids),
    }
    active_ids = {step.projection_step_id for step in steps}
    trace_map["provenance_factors"] = [
        {
            "projection_step_id": step.step_id,
            "factor_id": step.factor_id,
            "structural_source_id": step.structural_source_id,
        }
        for step in intent.steps
        if step.kind == "CAUSAL_FACTOR" and step.step_id not in active_ids
    ]
    return trace_map


def _active_steps(intent: ExecutionIntent) -> tuple[Any, ...]:
    """Keep provenance-only factors out of executable prompt steps."""
    selected_factors = {item.factor_id for item in intent.stimulus_requirements}
    return tuple(
        step
        for step in intent.steps
        if step.kind == "UNSAFE_CONTROL_ACTION" or step.factor_id in selected_factors
    )


def _action_for_plan(intent: ExecutionIntent, bindings: RuntimeBindingSet) -> ControlActionBinding:
    for action in bindings.control_action_bindings:
        if action.control_action_id == intent.control_action_id:
            return action
    raise ValueError("ready plan requires its target action binding")


def _plan_step(
    plan_id: str,
    index: int,
    projection_step: Any,
    surface: SurfaceBinding,
    control_action_id: str,
    action: ControlActionBinding,
    content_slot_id: str | None,
) -> PlanStep:
    is_target = projection_step.kind == "UNSAFE_CONTROL_ACTION"
    action_fields = _plan_step_action_fields(
        is_target,
        projection_step.step_id,
        control_action_id,
        action,
        content_slot_id,
    )
    return PlanStep(
        plan_step_id=plan_id,
        projection_step_id=projection_step.step_id,
        order=index,
        kind=projection_step.kind,
        factor_id=projection_step.factor_id,
        structural_source_id=projection_step.structural_source_id,
        surface=surface.surface,
        locator=surface.locator,
        **action_fields,
    )


def _plan_step_action_fields(
    is_target: bool,
    projection_step_id: str,
    control_action_id: str,
    action: ControlActionBinding,
    content_slot_id: str | None,
) -> dict[str, Any]:
    if is_target:
        return {
            "control_action_id": control_action_id,
            "adapter_operation": action.adapter_operation,
            "tool_name": action.tool_name,
            "tool_description": action.tool_description,
            "tool_schema": action.tool_schema,
            "safe_arguments": action.safe_defaults,
            "tool_choice": action.tool_choice,
            "tool_choice_reason": action.tool_choice_reason,
            "content_slot_id": None,
        }
    return {
        "control_action_id": None,
        "adapter_operation": None,
        "tool_name": "",
        "tool_description": None,
        "tool_schema": {},
        "safe_arguments": {},
        "tool_choice": "auto",
        "tool_choice_reason": None,
        "content_slot_id": content_slot_id or f"content:{projection_step_id}",
    }


__all__ = ["bind_and_plan"]
