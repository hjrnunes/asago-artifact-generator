from __future__ import annotations

from copy import deepcopy

import pytest
from scripts.continuations.o03_artifact_completion import (
    FIRST_ATTEMPT_CANDIDATE_SHA256,
    FIRST_ATTEMPT_RAW_SHA256,
    SECOND_CONTINUATION_ID,
    DispatchSlotSpent,
    _extract_authorities,
    _extract_second_continuation_authorities,
    _parser_schema_plan_gate,
    ensure_dispatch_slot_available,
    reconcile_budget,
)

from asago_artifact_generator.authoring import (
    PromptOverflowError,
    PromptPacket,
    _enforce_context_budget,
    _render_correction_packet,
    build_artifact_author_context,
    build_call2_packet_v2,
    build_correction_context,
    collect_artifact_findings_v2,
    neutral_artifact_response_without_source,
    neutral_call2_response_v2,
)
from asago_artifact_generator.detector_controls import DetectorControlFeedback
from asago_artifact_generator.input_adapter import InputKind, load_input
from asago_artifact_generator.qualification_inputs import prepare_o03_authoring_inputs


def _plan(*, judge_needed: bool) -> dict:
    return {
        "semantic_judge": {
            "needed": judge_needed,
            "scope": "reply" if judge_needed else None,
        },
        "stimulus_approach": {
            "request": "Inspect the supplied record.",
            "delivery": "direct_user_message",
            "history": [],
        },
        "runtime_bindings": [],
    }


def _author_context(plan: dict) -> dict:
    return build_artifact_author_context(
        view=load_input(
            "contracts/scenario-handoff/handoff-v1/valid/adversarial-refund.json",
            kind=InputKind.SCENARIO_HANDOFF_V1,
        ),
        plan=plan,
        inventory={"operations": [], "facts": [], "source_handles": []},
        runtime_contract={"delivery": ["direct_user_message"], "setup_permissions": []},
    )


def test_no_judge_plan_uses_required_null_only_contract_in_author_and_correction() -> None:
    context = _author_context(_plan(judge_needed=False))

    author_schema = context["response_contract"]["schema"]
    author_judge = author_schema["properties"]["semantic_judge_spec"]
    assert "semantic_judge_spec" in author_schema["required"]
    assert author_judge["type"] == "null"
    assert "properties" not in author_judge

    correction = build_correction_context(
        failed_stage="call2",
        original_context=context,
        current_output=neutral_call2_response_v2(),
        findings=[],
    )
    correction_judge = correction["response_contract"]["schema"]["properties"][
        "semantic_judge_spec"
    ]
    assert correction["response_contract"]["schema"]["required"].count(
        "semantic_judge_spec"
    ) == 1
    assert correction_judge["type"] == "null"


def test_judge_required_plan_keeps_supported_object_schema() -> None:
    contract = _author_context(_plan(judge_needed=True))["response_contract"]
    judge = contract["schema"]["properties"]["semantic_judge_spec"]

    assert judge["type"] == ["object", "null"]
    assert judge["required"] == ["question", "criteria", "fact_refs"]


def test_call2_author_packet_uses_the_plan_derived_contract() -> None:
    plan = _plan(judge_needed=False)
    view = load_input(
        "contracts/scenario-handoff/handoff-v1/valid/adversarial-refund.json",
        kind=InputKind.SCENARIO_HANDOFF_V1,
    )
    packet = build_call2_packet_v2(
        view,
        plan,
        {"operations": [], "facts": [], "source_handles": []},
        {"delivery": ["direct_user_message"], "setup_permissions": []},
    )

    judge = packet.payload["response_contract"]["schema"]["properties"][
        "semantic_judge_spec"
    ]
    assert judge == {
        "type": "null",
        "description": (
            "The accepted plan does not need a semantic judge; this required field must be null."
        ),
    }


def test_no_judge_response_rejection_preserves_non_null_candidate() -> None:
    metadata = neutral_artifact_response_without_source()
    returned_judge = {
        "question": "Can the reply be interpreted?",
        "criteria": "The supplied criterion is met.",
        "fact_refs": ["fact:one"],
    }
    metadata["semantic_judge_spec"] = returned_judge
    original = deepcopy(metadata["semantic_judge_spec"])

    findings = collect_artifact_findings_v2(
        metadata,
        _plan(judge_needed=False),
        {"operations": [], "facts": [], "source_handles": []},
        {"delivery": ["direct_user_message"], "setup_permissions": []},
    )

    assert any(item.code == "plan_conflict" for item in findings)
    assert metadata["semantic_judge_spec"] == original


def test_historical_candidates_remain_rejected_without_coercion() -> None:
    plan, first_raw, _ = _extract_authorities()
    second_plan, second_raw, _ = _extract_second_continuation_authorities()
    assert second_plan == plan
    prepared = prepare_o03_authoring_inputs()

    for raw in (first_raw, second_raw):
        parsed, findings = _parser_schema_plan_gate(
            raw=raw,
            plan=plan,
            prepared=prepared,
        )
        assert parsed is not None
        assert any(
            finding["code"] == "plan_conflict"
            and finding["path"] == "semantic_judge_spec"
            for finding in findings
        )
        assert parsed.metadata["semantic_judge_spec"] is not None


def test_correction_has_fixed_judge_sentence_nested_paths_and_exact_feedback() -> None:
    context = _author_context(_plan(judge_needed=False))
    feedback = (
        DetectorControlFeedback(
            name="malformed-non-object-arguments",
            evidence={
                "availability": {"tool_calls": "captured"},
                "completeness": {"tool_calls": "complete"},
                "tool_calls": [
                    {
                        "name": "example_operation",
                        "decoded_arguments": [],
                    }
                ],
            },
            expected_outcome="inconclusive",
            expected_claim_level="command_attempt",
            status="failed",
            actual_result=None,
            actual_outcome=None,
            actual_claim_level=None,
            error="detector runtime error: 'list' object has no attribute 'get'",
            outcome_class="detector_exception",
            runtime_contract_explanation=(
                "The detector raised an exception before returning a result."
            ),
        ),
    )
    correction = build_correction_context(
        failed_stage="call2",
        original_context=context,
        current_output=neutral_call2_response_v2(),
        findings=[
            {
                "code": "plan_conflict",
                "detail": "semantic_judge_spec must remain null for the accepted no-judge plan",
                "path": "semantic_judge_spec",
            }
        ],
        detector_feedback=feedback,
    )

    packet = _render_correction_packet(correction)

    sentence = (
        "The accepted plan requires no semantic judge. semantic_judge_spec must be null. "
        "This decision is fixed; correct the detector within it."
    )
    assert packet.user.count(sentence) == 1
    assert packet.user.index(sentence) < packet.user.index("CURRENT OUTPUT")
    assert "PLAN FIELD MEANINGS" not in packet.user
    for path in (
        "availability.tool_calls",
        "completeness.tool_calls",
        "tool_calls[i].decoded_arguments",
        "outcome",
        "claim_level",
        "evidence_refs",
    ):
        assert path in packet.user
    assert "malformed-non-object-arguments" in packet.user
    assert "'list' object has no attribute 'get'" in packet.user
    assert "decoded_arguments" in packet.user


def test_context_budget_uses_labelled_message_overhead() -> None:
    packet = PromptPacket(
        stage="correction",
        version="test",
        system="system",
        user="user",
        payload={},
    )

    with pytest.raises(PromptOverflowError, match="UTF-8-byte"):
        _enforce_context_budget(
            packet,
            context_window_tokens=256,
            max_completion_tokens=1,
        )


def test_second_continuation_guard_skips_first_attempt_once() -> None:
    first_attempt = {
        "dispatch_slot": "correction",
        "candidate_sha256": FIRST_ATTEMPT_CANDIDATE_SHA256,
        "raw_response_sha256": FIRST_ATTEMPT_RAW_SHA256,
    }

    ensure_dispatch_slot_available(
        {"dispatches": [first_attempt]},
        "correction",
        continuation=SECOND_CONTINUATION_ID,
    )
    second_attempt = {**first_attempt, "continuation_id": SECOND_CONTINUATION_ID}
    with pytest.raises(DispatchSlotSpent):
        ensure_dispatch_slot_available(
            {"dispatches": [first_attempt, second_attempt]},
            "correction",
            continuation=SECOND_CONTINUATION_ID,
        )

    ensure_dispatch_slot_available(
        {"dispatches": [first_attempt]},
        "review",
        continuation=SECOND_CONTINUATION_ID,
    )
    second_review = {
        "dispatch_slot": "review",
        "continuation_id": SECOND_CONTINUATION_ID,
    }
    with pytest.raises(DispatchSlotSpent):
        ensure_dispatch_slot_available(
            {"dispatches": [first_attempt, second_review]},
            "review",
            continuation=SECOND_CONTINUATION_ID,
        )


def test_second_continuation_reconciliation_preserves_first_attempt_state() -> None:
    reconciliation = reconcile_budget(second_continuation=True)
    assert reconciliation["historical_snapshot"] == {
        "author_correction_spent": 10,
        "author_correction_limit": 10,
        "review_spent": 5,
        "review_limit": 6,
        "task_spent": 13,
        "task_limit": 14,
        "aggregate_spent": 22,
        "aggregate_limit": 32,
    }
    assert reconciliation["resulting_budget"]["author_correction_limit"] == 11
    assert reconciliation["resulting_budget"]["author_correction_remaining"] == 1
    assert reconciliation["resulting_budget"]["review_limit"] == 7
    assert reconciliation["resulting_budget"]["review_remaining"] == 2
    assert reconciliation["authorization"][
        "previous_continuation_unused_review_authorization"
    ] == "expired"
