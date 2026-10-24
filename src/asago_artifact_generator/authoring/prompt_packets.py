"""Rendered Call 1 (plan) and Call 2 (artifact) prompt packets."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..input_adapter import InputView
from .context_budget import _enforce_prompt_size
from .core import (
    CALL1_PROMPT_VERSION_V26,
    CALL2_PROMPT_VERSION_V27,
    MAX_RENDERED_PROMPT_BYTES,
    PromptPacket,
)
from .prompt_context import (
    _ARTIFACT_AUTHOR_GUIDANCE,
    _plan_semantic_judge_needed,
    _render_sections,
    _scenario_design_prompt_view,
    build_artifact_author_context,
    build_plan_author_context,
)
from .prompt_safety import assert_no_prompt_secrets, prompt_data_urls
from .sequential_turns import multi_turn_sections
from .templates import prose


def _artifact_response_contract_for_prompt(
    contract: dict[str, Any],
    *,
    correction: bool = False,
) -> dict[str, Any]:
    """Drop the neutral example, and for corrections the stage-only guidance."""

    result = deepcopy(contract)
    result.pop("neutral_example", None)
    if correction:
        for key in (
            "semantic_judging",
            "plan_owned_field_descriptions",
            "interface_version",
            "plan_owned_fields",
        ):
            result.pop(key, None)
    return result


_BINDING_RULE_REFERENCES = {
    "source_ref": "source_ref_rule",
    "selector": "selector_rule",
    "consumers": "consumer_rule",
}


def _call1_response_contract_prompt_view(contract: dict[str, Any]) -> dict[str, Any]:
    """Render each binding rule once, in binding_declaration, and reference it elsewhere."""

    result = deepcopy(contract)
    declaration = result.get("binding_declaration")
    if not isinstance(declaration, dict):
        return result
    for key in ("selector_rule", "consumer_rule"):
        if result.get(key) == declaration.get(key):
            result.pop(key, None)
    bindings = result.get("schema", {}).get("properties", {}).get("runtime_bindings")
    items = bindings.get("items") if isinstance(bindings, dict) else None
    properties = items.get("properties") if isinstance(items, dict) else None
    if isinstance(properties, dict):
        for field, rule in _BINDING_RULE_REFERENCES.items():
            item = properties.get(field)
            if isinstance(item, dict) and item.get("description") == declaration.get(rule):
                item["description"] = f"See binding_declaration.{rule}."
    return result


def build_call1_packet_v2(
    view: InputView,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    max_prompt_bytes: int = MAX_RENDERED_PROMPT_BYTES,
) -> PromptPacket:
    """Render the v3 plan-author prompt over the unchanged v2 response wire."""

    context = build_plan_author_context(view, inventory, runtime_contract)
    sections = (
        (
            ("TASK", context["task"]),
            ("SCENARIO DESIGN", _scenario_design_prompt_view(context["scenario_design"])),
        )
        + multi_turn_sections(view, runtime_contract)
        + (("SOURCE CONTEXT", context["source_context"]),)
        + (("EVIDENCE REFERENCES", context["evidence_references"]),)
        + (
            ("EXECUTION CAPABILITIES", context["execution_capabilities"]),
            ("FIELD GUIDE", context["field_guide"]),
            ("PLAN FIELD MEANINGS", context["plan_field_meanings"]),
            ("NEUTRAL OUTCOME EXAMPLE", context["neutral_outcome_example"]),
            (
                "RESPONSE CONTRACT",
                _call1_response_contract_prompt_view(context["response_contract"]),
            ),
        )
    )
    assert_no_prompt_secrets(dict(sections))
    packet = PromptPacket(
        stage="call1",
        version=CALL1_PROMPT_VERSION_V26,
        system=_CALL1_SYSTEM_V3,
        user=_render_sections(
            sections, compact_titles=frozenset({"FIELD GUIDE", "SOURCE CONTEXT"})
        ),
        payload={},
    )
    _enforce_prompt_size(packet, max_prompt_bytes, allowed_urls=prompt_data_urls(view))
    return packet


def build_call2_packet_v2(
    view: InputView,
    plan: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    max_prompt_bytes: int = MAX_RENDERED_PROMPT_BYTES,
) -> PromptPacket:
    """Render the v3 artifact-author prompt over the unchanged v2 response wire."""

    context = build_artifact_author_context(view, plan, inventory, runtime_contract)
    sections: tuple[tuple[str, Any], ...] = (
        (
            (
                "ORIGINAL SCENARIO AND SOURCE CONTEXT",
                {
                    "scenario": context["original_scenario"],
                    "authoritative_context": context["authoritative_context"],
                },
            ),
        )
        + (
            ("PLAN FIELD MEANINGS", context["plan_field_meanings"]),
            ("ACCEPTED PLAN — immutable", context["accepted_plan"]),
        )
        + multi_turn_sections(view, runtime_contract, plan)
        + (("RUNTIME CAPABILITIES", context["runtime_contract"]),)
    )
    if _plan_semantic_judge_needed(plan):
        sections += (
            (
                "SEMANTIC JUDGE FACT REFERENCE GUIDANCE",
                context["semantic_judge_fact_ref_guidance"],
            ),
        )
    sections += (
        (
            "OUTPUT CONTRACT AND ONE NEUTRAL EXAMPLE",
            {
                "response_contract": _artifact_response_contract_for_prompt(
                    context["response_contract"]
                ),
                "neutral_example": context["neutral_example"],
            },
        ),
    )
    assert_no_prompt_secrets(dict(sections))
    packet = PromptPacket(
        stage="call2",
        version=CALL2_PROMPT_VERSION_V27,
        system=_CALL2_SYSTEM,
        # Same fit as call1's SOURCE CONTEXT: compact JSON keeps every value and
        # drops only indentation, which otherwise pushes large inventories past
        # the context budget.
        user=_render_sections(
            sections,
            compact_titles=frozenset({"ORIGINAL SCENARIO AND SOURCE CONTEXT"}),
        ),
        # The duplicate-candidate scan reads the accepted plan from here.
        payload={"accepted_plan": deepcopy(plan)},
    )
    _enforce_prompt_size(packet, max_prompt_bytes, allowed_urls=prompt_data_urls(view, plan))
    return packet


_CALL1_SYSTEM_V3 = prose("prompt_packets/call1_system_v3")
_CALL2_SYSTEM = prose("prompt_packets/call2_system") + " " + _ARTIFACT_AUTHOR_GUIDANCE
