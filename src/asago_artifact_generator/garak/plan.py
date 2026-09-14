"""Typed Garak planning from a ready, platform-neutral execution plan.

No source projection, narrative parser, taxonomy inference, or provider is
consulted here.  The planner consumes the frozen ``ReadyExecutionPlan``
produced by the platform-neutral readiness seam and merely chooses Garak's
wire representation for those already-resolved values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..authoring import PresentationSlot
from ..models._base import freeze_value
from ..models.readiness import ReadyExecutionPlan
from ..platforms.base import PlatformPlanError

_ROLE_BY_SURFACE = {
    "system_prompt": "system",
    "user_turn": "user",
    "assistant_turn": "assistant",
    "tool_call": "assistant",
    "environment_event": "assistant",
    "tool_result": "tool",
}


def _validate_step_values(step: GarakPlanStep) -> None:
    if not isinstance(step.tool_schema, Mapping) or not isinstance(step.arguments, Mapping):
        raise TypeError("Garak step tool schema and arguments must be objects")


def _validate_step_surface(surface: str, role: str) -> None:
    if surface not in _ROLE_BY_SURFACE:
        raise PlatformPlanError(f"Garak cannot render runtime surface {surface!r}")
    if role != _ROLE_BY_SURFACE[surface]:
        raise PlatformPlanError(f"Garak role {role!r} does not match surface {surface!r}")


@dataclass(frozen=True, slots=True)
class GarakPlanStep:
    """One ready-plan step rendered as a Garak transcript element."""

    plan_step_id: str
    projection_step_id: str
    order: int
    kind: str
    factor_id: str | None
    surface: str
    locator: str
    role: str
    control_action_id: str | None = None
    adapter_operation: str | None = None
    tool_name: str = ""
    tool_description: str | None = None
    tool_schema: Mapping[str, Any] | None = None
    arguments: Mapping[str, Any] | None = None
    tool_choice: str = "auto"
    tool_choice_reason: str | None = None
    content_slot_id: str | None = None

    def __post_init__(self) -> None:
        _validate_step_values(self)
        object.__setattr__(self, "tool_schema", freeze_value(self.tool_schema or {}))
        object.__setattr__(self, "arguments", freeze_value(self.arguments or {}))
        _validate_step_surface(self.surface, self.role)

    @property
    def trace_handle(self) -> str:
        return self.plan_step_id


@dataclass(frozen=True, slots=True)
class GarakPlan:
    """Complete deterministic Garak plan derived from a ready plan."""

    ready: ReadyExecutionPlan
    steps: tuple[GarakPlanStep, ...]
    tool_definitions: tuple[Mapping[str, Any], ...]
    content_slots: tuple[PresentationSlot, ...]

    def __post_init__(self) -> None:
        _validate_plan_ready(self.ready)
        self._normalize()
        _validate_plan_collections(self.steps, self.tool_definitions, self.content_slots)

    def _normalize(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(
            self,
            "tool_definitions",
            tuple(freeze_value(item) for item in self.tool_definitions),
        )
        object.__setattr__(self, "content_slots", tuple(self.content_slots))

    @property
    def bundle_digest(self) -> str:
        return self.ready.bundle_digest

    @property
    def projection_semantic_digest(self) -> str:
        return self.ready.projection_semantic_digest

    @property
    def binding_set_id(self) -> str:
        return self.ready.binding_set_id

    @property
    def binding_set_digest(self) -> str:
        return self.ready.binding_set_digest

    @property
    def adapter_version(self) -> str:
        return self.ready.adapter_version

    @property
    def scenario_id(self) -> str:
        return self.ready.scenario_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "garak-execution-plan-v1",
            "platform": "garak",
            "adapter_version": self.ready.adapter_version,
            "case_id": self.ready.case_id,
            "case_digest": self.ready.case_digest,
            "execution_classification_digest": self.ready.execution_classification_digest,
            "binding_completeness": self.ready.binding_completeness,
            "environment_basis": self.ready.environment_basis,
            "profile_fit": self.ready.profile_fit,
            "claim_scope": self.ready.claim_scope,
            "source_binding_completeness": self.ready.source_binding_completeness,
            "source_environment_basis": self.ready.source_environment_basis,
            "source_profile_fit": self.ready.source_profile_fit,
            "source_claim_scope": self.ready.source_claim_scope,
            "selected_simulation_resources": [
                item.model_dump(mode="json") for item in self.ready.selected_simulation_resources
            ],
            "selected_profile_id": self.ready.selected_profile_id,
            "selected_profile_basis": self.ready.selected_profile_basis,
            "target_environment_id": self.ready.target_environment_id,
            "target_profile_digest": self.ready.target_profile_digest,
            "target_realization_digest": self.ready.target_realization_digest,
            "inventory_authority": (
                self.ready.inventory_authority.value
                if self.ready.inventory_authority is not None
                else None
            ),
            "semantic_authority": (
                self.ready.semantic_authority.value
                if self.ready.semantic_authority is not None
                else None
            ),
            "bundle_digest": self.bundle_digest,
            "projection_semantic_digest": self.projection_semantic_digest,
            "binding_set_id": self.binding_set_id,
            "binding_set_digest": self.binding_set_digest,
            "steps": [
                {
                    "plan_step_id": step.plan_step_id,
                    "projection_step_id": step.projection_step_id,
                    "order": step.order,
                    "kind": step.kind,
                    "factor_id": step.factor_id,
                    "surface": step.surface,
                    "locator": step.locator,
                    "role": step.role,
                    "control_action_id": step.control_action_id,
                    "adapter_operation": step.adapter_operation,
                    "tool_name": step.tool_name,
                    "tool_description": step.tool_description,
                    "tool_schema": dict(step.tool_schema or {}),
                    "arguments": dict(step.arguments or {}),
                    "tool_choice": step.tool_choice,
                    "tool_choice_reason": step.tool_choice_reason,
                    "content_slot_id": step.content_slot_id,
                }
                for step in self.steps
            ],
            "tool_definitions": [dict(item) for item in self.tool_definitions],
            "content_slots": [slot.slot_id for slot in self.content_slots],
        }


def load_execution_plan(path):
    """Load one execution-plan document, dispatching on its schema version.

    ``execution-plan-v1`` documents remain legacy ``ReadyExecutionPlan``
    values; ``artifact-design-plan-v1`` documents are the handoff design
    path's plans. Unknown versions fail closed. Runners should load plans
    through this dispatcher so both paths replay without case changes.
    """

    import json
    from pathlib import Path

    from ..design.records import DESIGN_PLAN_SCHEMA_VERSION, ArtifactDesignPlan
    from ..platforms.base import PlatformPlanError

    document = json.loads(Path(path).read_text(encoding="utf-8"))
    version = document.get("schema_version") if isinstance(document, dict) else None
    if version == DESIGN_PLAN_SCHEMA_VERSION:
        return ArtifactDesignPlan.model_validate(document)
    if version == "execution-plan-v1":
        return ReadyExecutionPlan.model_validate(document)
    raise PlatformPlanError(f"unsupported execution plan schema version: {version!r}")


def _validate_plan_ready(ready: ReadyExecutionPlan) -> None:
    if not isinstance(ready, ReadyExecutionPlan):
        raise TypeError("Garak plan requires a ReadyExecutionPlan")


def _validate_plan_collections(
    steps: tuple[GarakPlanStep, ...],
    tool_definitions: tuple[Mapping[str, Any], ...],
    content_slots: tuple[PresentationSlot, ...],
) -> None:
    _validate_plan_steps(steps)
    _validate_plan_tools(tool_definitions)
    _validate_plan_slots(content_slots)


def _validate_plan_steps(steps: tuple[GarakPlanStep, ...]) -> None:
    if any(not isinstance(item, GarakPlanStep) for item in steps):
        raise TypeError("Garak plan steps must be GarakPlanStep values")


def _validate_plan_tools(tool_definitions: tuple[Mapping[str, Any], ...]) -> None:
    if any(not isinstance(item, Mapping) for item in tool_definitions):
        raise TypeError("Garak tool definitions must be objects")


def _validate_plan_slots(content_slots: tuple[PresentationSlot, ...]) -> None:
    if any(not isinstance(item, PresentationSlot) for item in content_slots):
        raise TypeError("Garak content slots must be PresentationSlot values")


def _tool_definition(step: Any) -> Mapping[str, Any] | None:
    if not step.tool_name:
        return None
    if not isinstance(step.tool_schema, Mapping):
        raise PlatformPlanError(f"{step.plan_step_id} tool schema must be an object")
    schema = dict(step.tool_schema or {})
    function: dict[str, Any] = {
        "name": step.tool_name,
        "parameters": schema,
    }
    if step.tool_description is not None:
        function["description"] = step.tool_description
    return {
        "type": "function",
        "function": function,
    }


def _require_garak_ready(ready: ReadyExecutionPlan) -> None:
    if not isinstance(ready, ReadyExecutionPlan):
        raise TypeError("Garak planning requires a ReadyExecutionPlan")
    if ready.platform != "garak":
        raise PlatformPlanError(f"ready plan targets {ready.platform!r}, not the Garak adapter")
    if not ready.steps:
        raise PlatformPlanError("ready plan must contain at least one execution step")


def _build_plan_step(source: Any) -> GarakPlanStep:
    role = _source_role(source)
    return GarakPlanStep(
        plan_step_id=source.plan_step_id,
        projection_step_id=source.projection_step_id,
        order=source.order,
        kind=source.kind,
        factor_id=source.factor_id,
        surface=source.surface,
        locator=source.locator,
        role=role,
        control_action_id=source.control_action_id,
        adapter_operation=source.adapter_operation,
        tool_name=source.tool_name,
        tool_description=source.tool_description,
        tool_schema=source.tool_schema,
        arguments=source.safe_arguments,
        tool_choice=source.tool_choice,
        tool_choice_reason=source.tool_choice_reason,
        content_slot_id=source.content_slot_id,
    )


def _source_role(source: Any) -> str:
    role = _ROLE_BY_SURFACE.get(source.surface)
    if role is None:
        raise PlatformPlanError(f"Garak does not support source surface {source.surface!r}")
    return role


def _register_definition(
    source: Any,
    definitions: list[Mapping[str, Any]],
    definition_names: set[str],
) -> None:
    definition = _tool_definition(source)
    if definition is not None and source.tool_name not in definition_names:
        definition_names.add(source.tool_name)
        definitions.append(definition)


def _register_content_slot(
    source: Any,
    role: str,
    requested_slots: set[str],
    slot_by_id: dict[str, PresentationSlot],
) -> None:
    if not source.content_slot_id:
        return
    if source.content_slot_id not in requested_slots:
        raise PlatformPlanError(
            f"step {source.plan_step_id} names an undeclared content slot "
            f"{source.content_slot_id!r}"
        )
    slot_by_id[source.content_slot_id] = PresentationSlot(
        slot_id=source.content_slot_id,
        purpose=f"Presentation text for projection step {source.projection_step_id}",
        allowed_role=role,
    )


def _complete_content_slots(
    declared_slots: tuple[str, ...],
    slot_by_id: dict[str, PresentationSlot],
) -> tuple[PresentationSlot, ...]:
    for slot_id in declared_slots:
        slot_by_id.setdefault(
            slot_id,
            PresentationSlot(
                slot_id=slot_id,
                purpose="Presentation text for a compiler-designated execution slot",
                allowed_role="user",
            ),
        )
    return tuple(slot_by_id[slot_id] for slot_id in declared_slots)


def build_garak_plan(ready: ReadyExecutionPlan) -> GarakPlan:
    """Translate one ready plan to an immutable Garak plan.

    ``ReadyExecutionPlan`` is deliberately the only accepted input.  Calling
    this with an intent, a partial binding, or a non-ready result is a type and
    contract error rather than an opportunity to infer missing execution data.
    """

    _require_garak_ready(ready)

    steps: list[GarakPlanStep] = []
    definitions: list[Mapping[str, Any]] = []
    definition_names: set[str] = set()
    slot_by_id: dict[str, PresentationSlot] = {}
    requested_slots = set(ready.content_slots)

    for source in ready.steps:
        role = _source_role(source)
        steps.append(_build_plan_step(source))
        _register_definition(source, definitions, definition_names)
        _register_content_slot(source, role, requested_slots, slot_by_id)

    return GarakPlan(
        ready=ready,
        steps=tuple(steps),
        tool_definitions=tuple(definitions),
        content_slots=_complete_content_slots(ready.content_slots, slot_by_id),
    )


__all__ = [
    "GarakPlan",
    "GarakPlanStep",
    "build_garak_plan",
    "load_execution_plan",
]
