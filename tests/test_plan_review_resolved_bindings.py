from __future__ import annotations

from asago_artifact_generator.authoring.core import PLAN_REVIEW_PROMPT_VERSION_V20
from asago_artifact_generator.authoring.review import (
    build_plan_review_packet,
    build_plan_reviewer_context,
)

from .support import world_builders

_inventory, _plan, _runtime_contract, _view = world_builders(
    "ehr", "inventory", "plan", "runtime_contract", "view"
)


def _plan_with_bindings() -> dict:
    plan = _plan()
    plan["runtime_bindings"] = [
        {
            "name": "target_record_id",
            "source_kind": "supplied_input",
            "source_ref": "facts:reservation:RES-201",
            "selector": "value.guest_id",
            "expected_type": "string",
            "on_missing": "stop",
            "consumers": ["judge.target_record_id"],
        },
        {
            "name": "draft_id",
            "source_kind": "setup_output",
            "source_ref": "setup:summarize_for_ehr",
            "selector": "result.draft_id",
            "expected_type": "string",
            "on_missing": "stop",
            "consumers": ["judge.draft_id"],
        },
    ]
    return plan


def test_plan_reviewer_sees_resolved_supplied_binding_values() -> None:
    plan = _plan_with_bindings()

    context = build_plan_reviewer_context(_view(), plan, _inventory(), _runtime_contract())
    resolved = context["resolved_supplied_binding_values"]

    assert resolved["values"] == [
        {
            "name": "target_record_id",
            "source_ref": "facts:reservation:RES-201",
            "selector": "value.guest_id",
            "resolved_value": "GST002",
        }
    ]
    assert resolved["resolved_at_run_time"] == ["draft_id"]
    assert "value_meaning" in resolved["reviewer_instruction"]
    assert "record identifier" in resolved["reviewer_instruction"]


def test_plan_review_packet_renders_resolved_values_after_candidate_plan() -> None:
    plan = _plan_with_bindings()

    packet = build_plan_review_packet(_view(), plan, _inventory(), _runtime_contract())

    assert packet.version == PLAN_REVIEW_PROMPT_VERSION_V20
    section = packet.user.index("RESOLVED SUPPLIED BINDING VALUES")
    assert packet.user.index("CANDIDATE PLAN") < section
    assert section < packet.user.index("MECHANICAL GUARANTEES (NOT REVIEW QUESTIONS)")
    assert '"resolved_value": "GST002"' in packet.user[section:]
    assert packet.payload["resolved_supplied_binding_values"]["values"][0]["name"] == (
        "target_record_id"
    )


def test_keyed_record_shorthand_shows_canonical_path_and_selected_field() -> None:
    inventory = _inventory()
    record_schema = {
        "type": "object",
        "properties": {"owner_id": {"type": "string"}, "status": {"type": "string"}},
    }
    inventory["facts"].extend(
        [
            {
                "ref": "state:records",
                "value": {"REC-2": {"owner_id": "OWN-2", "status": "open"}},
                "schema": {"type": "object", "properties": {"REC-2": record_schema}},
                "provenance": "seeded state",
            },
            {
                "ref": "state:records:records",
                "value": {"REC-2": {"record_key": "REC-2"}},
                "schema": {
                    "type": "object",
                    "properties": {
                        "REC-2": {
                            "type": "object",
                            "properties": {"record_key": {"type": "string"}},
                        }
                    },
                },
                "provenance": "derived record keys",
            },
        ]
    )
    plan = _plan()
    plan["runtime_bindings"] = [
        {
            "name": "target_record_id",
            "source_kind": "supplied_input",
            "source_ref": "facts:state:records:REC-2:owner_id",
            "selector": "value",
            "expected_type": "string",
            "on_missing": "stop",
            "consumers": ["judge.target_record_id"],
        }
    ]

    context = build_plan_reviewer_context(_view(), plan, inventory, _runtime_contract())

    assert context["resolved_supplied_binding_values"]["values"] == [
        {
            "name": "target_record_id",
            "source_ref": "facts:state:records",
            "selector": "value.REC-2.owner_id",
            "resolved_value": "OWN-2",
        }
    ]


def test_plan_without_bindings_has_empty_resolved_values() -> None:
    context = build_plan_reviewer_context(_view(), _plan(), _inventory(), _runtime_contract())

    assert context["resolved_supplied_binding_values"]["values"] == []
    assert context["resolved_supplied_binding_values"]["resolved_at_run_time"] == []
