"""Pure runtime binding and platform-readiness planning."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ..models._base import freeze_value
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
)
from ..models.runtime_binding import (
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
    refs: list[str] = []
    for factor in intent.causal_factors:
        refs.append(factor.structural_source_id)
    refs.extend(step.structural_source_id for step in intent.steps)
    return tuple(dict.fromkeys(refs))


def _resolve_surfaces(
    intent: ExecutionIntent,
    bindings: RuntimeBindingSet | None,
    capabilities: PlatformCapabilities | None,
    diagnostics: list[ReadinessDiagnostic],
) -> tuple[str, dict[str, SurfaceBinding]]:
    source_refs = set(_required_source_refs(intent))
    supplied = {item.source_ref: item for item in (bindings.surface_bindings if bindings else ())}
    _diagnose_unsolicited_surfaces(bindings, source_refs, diagnostics)
    complete = True
    for source_ref in source_refs:
        complete = (
            _resolve_surface(source_ref, supplied.get(source_ref), capabilities, diagnostics)
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
    writable = _validate_surface_writable(source_ref, item, diagnostics)
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
        return surface == "tool_call"
    return surface != "tool_call"


def _expected_observer_kind(condition: SemanticCondition) -> str:
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
            diagnostics,
        )
        complete = valid and complete
        if plan is not None:
            plans.append(plan)
    return ("complete" if complete else "incomplete"), tuple(plans)


def _required_observer_conditions(
    intent: ExecutionIntent,
) -> list[tuple[str, SemanticCondition]]:
    conditions = [
        (factor.factor_id, factor.temporal_condition)
        for factor in intent.causal_factors
        if factor.temporal_condition is not None
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


def _resolve_one_observer(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    capabilities: PlatformCapabilities | None,
    values: Mapping[tuple[str, str], Any],
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
    valid = _validate_observer(condition_ref, condition, observer, diagnostics)
    _diagnose_observer_support(condition_ref, observer, capabilities, diagnostics)
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
        ),
        valid,
    )


def _validate_observer(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    checks = (
        _validate_observer_kind(condition_ref, condition, observer, diagnostics),
        _validate_observer_property(condition_ref, condition, observer, diagnostics),
        _validate_observer_field_path(condition_ref, condition, observer, diagnostics),
        _validate_observer_sources(condition_ref, condition, observer, diagnostics),
    )
    return all(checks)


def _validate_observer_kind(
    condition_ref: str,
    condition: SemanticCondition,
    observer: Any,
    diagnostics: list[ReadinessDiagnostic],
) -> bool:
    expected_kind = _expected_observer_kind(condition)
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
    if not needs_path or observer.field_path:
        return True
    diagnostics.append(
        _diagnostic(
            "runtime_binding",
            "observer_field_path_missing",
            "tool_argument observers require an explicit field path",
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
        requirements.required_surface_categories, bound_surfaces, diagnostics
    )
    _check_persistent_state_requirement(
        requirements.requires_persistent_state, bound_surfaces, diagnostics
    )
    _check_clock_requirement(requirements.requires_real_clock, bindings, diagnostics)


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
    "external_input": {"user_turn", "agent_message"},
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
    intent: ExecutionIntent,
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
    diagnostics: list[ReadinessDiagnostic] = []
    source_status = _binding_source_status(intent, bindings, diagnostics)
    semantic_status, resolved, values = _resolve_semantic_bindings(intent, bindings, diagnostics)
    surface_status, surfaces = _resolve_surfaces(intent, bindings, capabilities, diagnostics)
    action = _resolve_action(intent, bindings, capabilities, diagnostics)
    _diagnose_surface_action_pair(intent, surfaces, action, diagnostics)
    observer_status, observers = _resolve_observer(
        intent, bindings, capabilities, values, diagnostics
    )
    _check_requirements(intent, bindings, capabilities, surfaces, action, diagnostics)
    runtime_status = _runtime_status(surface_status, observer_status, diagnostics)
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
    plan = _ready_plan(intent, bindings, capabilities, resolved, observers, surfaces)
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
    intent: ExecutionIntent,
    bindings: RuntimeBindingSet | None,
    capabilities: PlatformCapabilities,
) -> None:
    if not isinstance(intent, ExecutionIntent):
        raise TypeError("bind_and_plan requires an ExecutionIntent, not a raw mapping")
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
    diagnostics: list[ReadinessDiagnostic],
) -> str:
    if surface_status == "incomplete" or observer_status == "incomplete":
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
    surfaces: Mapping[str, SurfaceBinding],
) -> ReadyExecutionPlan:
    steps, trace_map = _plan_steps(intent, surfaces, bindings, capabilities)
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
        clock_binding=bindings.clock_binding,
        state_channels=_surface_refs(surfaces, {"memory", "data_store"}),
        agent_channels=_surface_refs(surfaces, {"agent_message"}),
        content_slots=_content_slots(steps),
        trace_map=freeze_value(trace_map),
        presentation_context=intent.presentation_context,
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
) -> tuple[tuple[PlanStep, ...], dict[str, Any]]:
    action = _action_for_plan(intent, bindings)
    steps: list[PlanStep] = []
    trace_map: dict[str, Any] = {}
    for index, projection_step in enumerate(intent.steps, start=1):
        plan_id = f"plan-{index}"
        step = _plan_step(
            plan_id,
            index,
            projection_step,
            surfaces[projection_step.structural_source_id],
            intent.control_action_id,
            action,
        )
        steps.append(step)
        trace_map[plan_id] = {
            "projection_step_id": projection_step.step_id,
            "factor_id": projection_step.factor_id,
            "structural_source_id": projection_step.structural_source_id,
        }
    trace_map["oracle-1"] = {
        "condition_ref": intent.unsafe_outcome.outcome_id,
        "condition_type": intent.unsafe_outcome.condition.type,
    }
    return tuple(steps), trace_map


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
) -> PlanStep:
    is_target = projection_step.kind == "UNSAFE_CONTROL_ACTION"
    action_fields = _plan_step_action_fields(
        is_target, projection_step.step_id, control_action_id, action
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
) -> dict[str, Any]:
    if is_target:
        return {
            "control_action_id": control_action_id,
            "adapter_operation": action.adapter_operation,
            "tool_name": action.tool_name,
            "tool_schema": action.tool_schema,
            "safe_arguments": action.safe_defaults,
            "content_slot_id": None,
        }
    return {
        "control_action_id": None,
        "adapter_operation": None,
        "tool_name": "",
        "tool_schema": {},
        "safe_arguments": {},
        "content_slot_id": f"content:{projection_step_id}",
    }


__all__ = ["bind_and_plan"]
