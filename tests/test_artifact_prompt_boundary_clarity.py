from __future__ import annotations

from copy import deepcopy

from asago_artifact_generator.authoring.binding_repair import correction_repair_inputs
from asago_artifact_generator.authoring.core import (
    ARTIFACT_REVIEW_PROMPT_VERSION,
)
from asago_artifact_generator.authoring.correction import (
    _render_correction_packet,
    build_correction_context,
)
from asago_artifact_generator.authoring.prompt_context import build_artifact_author_context
from asago_artifact_generator.authoring.prompt_packets import build_call2_packet_v2
from asago_artifact_generator.authoring.review import (
    build_artifact_review_packet,
    build_artifact_reviewer_context,
)

from .support import world_builders

_inventory, _metadata, _plan, _runtime_contract, _view = world_builders(
    "ehr", "inventory", "metadata", "plan", "runtime_contract", "view"
)

_REQUIRED_FIELDS = [
    "native_id",
    "name",
    "decoded_arguments",
    "decoded_result",
    "status",
]


def _tool_call_shaped_inputs() -> tuple[dict, dict]:
    plan = _plan()
    plan["required_observations"] = {
        "tool_calls": {
            "availability": "captured",
            "completeness": "complete",
            "required_fields": list(_REQUIRED_FIELDS),
        },
        "missing_behavior": "inconclusive",
    }
    runtime = _runtime_contract()
    runtime["observation"]["tool_calls"]["required_fields"] = list(_REQUIRED_FIELDS)
    return plan, runtime


def test_building_the_artifact_prompts_leaves_the_accepted_plan_unchanged() -> None:
    plan, runtime = _tool_call_shaped_inputs()
    original_plan = deepcopy(plan)
    view, inventory = _view(), _inventory()
    context = build_artifact_author_context(view, plan, inventory, runtime)
    build_call2_packet_v2(view, plan, inventory, runtime)
    _render_correction_packet(
        build_correction_context(
            failed_stage="call2",
            original_context=context,
            current_output=b"candidate",
            findings=[],
        ),
        correction_repair_inputs(view, inventory, runtime),
    )

    assert context["runtime_contract"] == runtime
    assert plan == original_plan


def test_artifact_review_does_not_replay_unrelated_judge_facts_or_capabilities() -> None:
    context = build_artifact_reviewer_context(
        _view(),
        _plan(),
        _metadata(),
        _inventory(),
        _runtime_contract(),
    )

    assert context["resolved_runtime_context"]["judge_facts"] == []
    assert context["resolved_runtime_context"]["judge_facts_are_in_authoritative_context"] is False
    assert context["authoritative_context"]["runtime_capabilities"] == {
        "note": (
            "See RUNTIME CAPABILITIES for runtime capabilities and limits; "
            "they are not repeated in this inventory section."
        )
    }


def test_tool_call_plan_renders_its_correction_and_artifact_review() -> None:
    plan, runtime = _tool_call_shaped_inputs()
    view, inventory = _view(), _inventory()
    original_context = build_artifact_author_context(view, plan, inventory, runtime)
    correction_context = build_correction_context(
        failed_stage="call2",
        original_context=original_context,
        current_output=b"candidate",
        findings=[],
    )
    correction = _render_correction_packet(
        correction_context, correction_repair_inputs(view, inventory, runtime)
    )
    review = build_artifact_review_packet(
        view,
        plan,
        _metadata(),
        inventory,
        runtime,
    )
    assert correction.user.count("CURRENT OUTPUT\n") == 1
    assert review.version == ARTIFACT_REVIEW_PROMPT_VERSION


def test_artifact_review_inventory_keeps_only_records_the_plan_cites() -> None:
    from asago_artifact_generator.authoring.review import _artifact_review_authoritative_context

    inventory = {
        "operations": [
            {"name": "lookup", "description": "Look up."},
            {"name": "refund", "description": "Refund."},
            {"name": "unused", "description": "Not cited."},
        ],
        "facts": [
            {"ref": "order:1", "value": 1, "schema": {"type": "integer"}},
            {"ref": "order:2", "value": 2, "schema": {"type": "integer"}},
        ],
        "source_handles": [
            {"ref": "source:case", "meaning": "Case."},
            {"ref": "source:other", "meaning": "Not cited."},
        ],
    }
    plan = {
        "interpretation": {"source_refs": ["source:case", "SC-1"]},
        "assumptions": [{"reason": "an assumption without a ref"}],
        "prerequisites": [{"evidence_refs": ["operation:refund"]}],
        "setup_recipe": [{"name": "lookup"}],
        "selected_evidence": [],
        "runtime_bindings": [],
    }

    context = _artifact_review_authoritative_context(plan, inventory, ["order:2"])

    assert context["facts"] == [{"ref": "order:2", "value": 2, "schema": {"type": "integer"}}]
    assert [operation["name"] for operation in context["operations"]] == ["lookup", "refund"]
    assert context["source_handles"] == [{"ref": "source:case", "meaning": "Case."}]
    assert "not repeated" in context["runtime_capabilities"]["note"]
