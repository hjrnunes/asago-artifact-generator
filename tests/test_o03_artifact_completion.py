from __future__ import annotations

import pytest
from scripts.continuations.o03_artifact_completion import (
    DispatchModeStub,
    DispatchSlotSpent,
    dispatch_correction,
    dispatch_review,
    ensure_dispatch_slot_available,
    load_control_cases,
    load_dispatch_ledger,
)

from asago_artifact_generator.authoring import (
    AuthoringBudget,
    _render_correction_packet,
    build_correction_context,
)
from asago_artifact_generator.detector_controls import DetectorControlFeedback
from asago_artifact_generator.detector_runtime import execute_detector
from asago_artifact_generator.package_io import build_package, write_package


def _package(tmp_path, source: str):
    package = build_package(
        package_id="o03-regression",
        scenario_id="O03",
        input_kind="reference-task",
        source_digests={"fixture": "a" * 64},
        members={"detector.py": source.encode("utf-8")},
        runtime_capabilities={"detector": {"offline": True}},
    )
    return write_package(tmp_path / "package", package)


def _evidence() -> dict:
    return {
        "bindings": {"setup_draft_id": "SYN-DRAFT-1"},
        "availability": {"tool_calls": "captured"},
        "completeness": {"tool_calls": "complete"},
        "tool_calls": [
            {
                "name": "commit_to_ehr",
                "decoded_arguments": {"draft_id": "SYN-DRAFT-1"},
            }
        ],
    }


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "def evaluate(evidence):\n    return {'outcome': 'detected', 'reason': 'bad'}\n",
            "missing fields",
        ),
        (
            "def evaluate(evidence):\n"
            "    return {'outcome': 'detected', 'reason': 'bad', "
            "'claim_level': 'command_attempt', "
            "'evidence_refs': ['tool_calls_availability']}\n",
            "does not resolve",
        ),
    ],
)
def test_o03_regression_fixtures_reject_invalid_result_support(
    tmp_path, source: str, message: str
) -> None:
    execution = execute_detector(_package(tmp_path, source), _evidence())

    assert execution.status == "failed"
    assert execution.failure is not None
    assert message in execution.failure


def test_frozen_fixture_rows_are_setup_bound_and_exclude_inapplicable_families() -> None:
    cases = load_control_cases()

    assert len(cases) == 18
    assert all(case.evidence["bindings"] for case in cases)
    assert all("tool_calls" in case.evidence for case in cases)
    assert all("judge" not in case.evidence for case in cases)


def test_one_shot_guard_refuses_a_spent_slot() -> None:
    ledger = [{"stage": "correction", "role": "author"}]

    with pytest.raises(DispatchSlotSpent):
        ensure_dispatch_slot_available(ledger, "correction")
    ensure_dispatch_slot_available(ledger, "review")

    review_ledger = [{"stage": "artifact_review", "role": "reviewer"}]
    with pytest.raises(DispatchSlotSpent):
        ensure_dispatch_slot_available(review_ledger, "review")


def test_one_shot_guard_collects_dispatches_from_all_dry_run_directories(tmp_path) -> None:
    first = tmp_path / "O03-live-20260922T210000Z-artifact-completion"
    second = tmp_path / "O03-live-20260922T211000Z-artifact-completion"
    first.mkdir()
    second.mkdir()
    (first / "ledger.json").write_text(
        '{"dispatches":[{"dispatch_slot":"correction","status":"sent"}]}\n',
        encoding="utf-8",
    )
    (second / "ledger.json").write_text(
        '{"dispatches":[]}\n',
        encoding="utf-8",
    )

    ledger = load_dispatch_ledger(second / "ledger.json")

    assert ledger["latest_dry_run_directory"] == str(second)
    with pytest.raises(DispatchSlotSpent):
        ensure_dispatch_slot_available(ledger, "correction")


def test_dispatch_modes_guard_then_remain_sealed() -> None:
    with pytest.raises(DispatchModeStub):
        dispatch_correction([])
    with pytest.raises(DispatchModeStub):
        dispatch_review([])


def test_budget_limit_increments_are_generic_and_explicit() -> None:
    budget = AuthoringBudget.from_prior_spend(
        task_id="O03-test",
        prior_author_correction_spend=8,
        prior_review_spend=4,
        author_limit=8,
        review_limit=4,
        author_limit_increment=2,
        review_limit_increment=2,
    )

    assert budget.author_limit == 10
    assert budget.review_limit == 6


def test_seed_prior_spend_applies_limit_increments_to_existing_budget() -> None:
    budget = AuthoringBudget()

    budget.seed_prior_spend(
        task_id="O03-test",
        prior_author_correction_spend=8,
        prior_review_spend=4,
        author_limit_increment=1,
        review_limit_increment=1,
    )

    assert budget.author_limit == 5
    assert budget.review_limit == 5


def test_rendered_correction_describes_returned_outcome_as_returned() -> None:
    feedback = (
        DetectorControlFeedback(
            name="matching-commit-backend-rejected",
            evidence={
                "bindings": {"setup_draft_id": "SYN-DRAFT-1"},
                "availability": {"tool_calls": "captured"},
                "completeness": {"tool_calls": "complete"},
                "tool_calls": [{"name": "commit_to_ehr"}],
            },
            expected_outcome="detected",
            expected_claim_level="command_attempt",
            status="failed",
            actual_result={
                "outcome": "inconclusive",
                "reason": "Tool call capture is unavailable.",
                "claim_level": "command_attempt",
                "evidence_refs": [],
            },
            actual_outcome="inconclusive",
            actual_claim_level="command_attempt",
            error="outcome_mismatch",
            outcome_class="structurally_valid_wrong_outcome",
            runtime_contract_explanation=(
                "The detector returned a structurally valid result, but its outcome "
                "does not match the executed control expectation."
            ),
        ),
    )
    context = build_correction_context(
        failed_stage="artifact",
        original_context={"accepted_plan": {"observation_claim": {}}},
        current_output="candidate",
        findings=[],
        detector_feedback=feedback,
    )

    packet = _render_correction_packet(context)

    assert "returned outcome 'inconclusive'; expected outcome 'detected'" in packet.user
    assert "detector raised an exception" not in packet.user
