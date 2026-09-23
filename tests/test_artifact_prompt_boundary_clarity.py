from __future__ import annotations

import json
from copy import deepcopy

from asago_artifact_generator.authoring import (
    ARTIFACT_REVIEW_PROMPT_VERSION,
    ARTIFACT_REVIEW_PROMPT_VERSION_V3,
    CALL2_PROMPT_VERSION_V7,
    CORRECTION_PROMPT_VERSION_V7,
    _render_correction_packet,
    artifact_observation_guide,
    build_artifact_author_context,
    build_artifact_review_packet,
    build_call2_packet_v2,
    build_correction_context,
)
from tests.test_versioned_prompt_roles import (
    _inventory,
    _metadata,
    _plan,
    _runtime_contract,
    _source,
    _view,
)

_REQUIRED_FIELDS = [
    "native_id",
    "name",
    "decoded_arguments",
    "decoded_result",
    "status",
]


def _o03_shaped_inputs() -> tuple[dict, dict]:
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


def _interface_section(user: str) -> str:
    marker = "RUNTIME EVIDENCE INTERFACE\n"
    assert user.count(marker) == 1
    return user.split(marker, 1)[1].split("\n\n", 1)[0]


def test_artifact_prompt_separates_capture_inventory_from_branch_requirements() -> None:
    plan, runtime = _o03_shaped_inputs()
    original_plan = deepcopy(plan)
    view, inventory = _view(), _inventory()
    context = build_artifact_author_context(view, plan, inventory, runtime)
    guide = artifact_observation_guide(plan, runtime)
    author = build_call2_packet_v2(view, plan, inventory, runtime)
    correction = _render_correction_packet(
        build_correction_context(
            failed_stage="call2",
            original_context=context,
            current_output="candidate",
            findings=[],
        )
    )

    assert guide["fixed_claim_level"] == "command_attempt"
    assert guide["expected_capture_inventory"]["plan"] == (
        "accepted_plan.required_observations.tool_calls"
    )
    assert guide["expected_capture_inventory"]["runtime_contract"] == (
        "runtime_contract.observation.tool_calls"
    )
    assert "missing decoded_result" in guide["outcome_requirements"]["detected"]
    assert (
        "availability is captured and completeness is complete"
        in guide["outcome_requirements"]["not_detected"]
    )
    assert "prerequisite" in guide["outcome_requirements"]["inconclusive"]
    assert "OBSERVATION DECISION GUIDE" in author.user
    assert "OBSERVATION DECISION GUIDE" in correction.user
    assert "needs_plan_revision" not in author.system + author.user
    assert "needs_plan_revision" not in correction.system + correction.user
    assert plan == original_plan
    assert author.version == CALL2_PROMPT_VERSION_V7
    assert correction.version == CORRECTION_PROMPT_VERSION_V7


def test_runtime_interface_has_one_path_table_and_a_valid_absence_result_example() -> None:
    plan, runtime = _o03_shaped_inputs()
    packet = build_call2_packet_v2(_view(), plan, _inventory(), runtime)
    interface = _interface_section(packet.user)

    assert '"synthetic_excerpt"' not in interface
    assert '"bindings.<name>"' not in interface
    assert '"outcomes":' not in interface
    assert '"claim_level":["command_attempt"]' in interface
    assert '"complete_absence_example"' in interface
    assert (
        json.dumps(
            ["tool_calls", "availability.tool_calls", "completeness.tool_calls"],
            separators=(",", ":"),
        )
        in interface
    )


def test_artifact_prompt_explains_reference_namespaces_and_runtime_bindings() -> None:
    plan, runtime = _o03_shaped_inputs()
    packet = build_call2_packet_v2(_view(), plan, _inventory(), runtime)

    assert "Plan source handles and prerequisite source citations are provenance" in packet.system
    assert "evidence.bindings.<declared name>" in packet.system
    assert "Detector result evidence_refs must resolve within the actual evidence" in packet.system
    assert "Synthetic examples and controls substitute their own values" in packet.system


def test_correction_renders_optional_stage_context_and_current_review_view() -> None:
    plan, runtime = _o03_shaped_inputs()
    view, inventory = _view(), _inventory()
    original_context = build_artifact_author_context(view, plan, inventory, runtime)
    correction_context = build_correction_context(
        failed_stage="call2",
        original_context=original_context,
        current_output="candidate",
        findings=[],
    )
    correction_context["supplied_stage_context"] = {
        "fixture_identity": "scenario provenance; experiment uses its declared binding",
        "control_feedback_provenance": "raw result unavailable; verdict read from source",
    }
    correction = _render_correction_packet(correction_context)
    review = build_artifact_review_packet(
        view,
        plan,
        _metadata(),
        _source(),
        [],
        inventory,
        runtime,
    )
    sealed_review = build_artifact_review_packet(
        view,
        plan,
        _metadata(),
        _source(),
        [],
        inventory,
        runtime,
        sealed_version=ARTIFACT_REVIEW_PROMPT_VERSION_V3,
    )

    assert correction.user.count("SUPPLIED STAGE CONTEXT\n") == 1
    assert "raw result unavailable; verdict read from source" in correction.user
    assert correction.user.count("OBSERVATION DECISION GUIDE\n") == 1
    assert review.version == ARTIFACT_REVIEW_PROMPT_VERSION
    assert "OBSERVATION DECISION GUIDE\n" in review.user
    assert sealed_review.version == ARTIFACT_REVIEW_PROMPT_VERSION_V3
    assert "OBSERVATION DECISION GUIDE\n" not in sealed_review.user
