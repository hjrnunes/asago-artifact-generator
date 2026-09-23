from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from scripts.continuations.o03_artifact_completion import (
    ACCEPTED_PLAN_SHA256,
    AUTHOR_INCREMENT,
    LIVE_TASK_ID,
    REVIEW_INCREMENT,
    SECOND_ATTEMPT_DIRECTORY_NAME,
    SECOND_CANDIDATE_SHA256,
    SECOND_CONTINUATION_ID,
    SECOND_RAW_SHA256,
    THIRD_CONTINUATION_ID,
    THIRD_DEFECT_FAILING_ROWS,
    DispatchSlotSpent,
    _continuation_thinking,
    _dispatch_transport_for,
    _extract_authorities,
    _extract_third_continuation_authorities,
    _final_output_failure,
    _live_controls,
    _pinned_controls,
    _thinking_extra_body,
    _third_continuation_contract_findings,
    _third_continuation_intervening_dispatches,
    ensure_dispatch_slot_available,
    reconcile_budget,
    run_dry_run,
)

from asago_artifact_generator.authoring import (
    AUTHORING_THINKING_EXTRA_BODY,
    AuthoringBudget,
)

FIRST_ATTEMPT_CANDIDATE = "4ff4763e9110c549f6e5aeee2aa61512426545ac0f2a7bad9a418611ef0dda11"
FIRST_ATTEMPT_RAW = "ec7c42ea70bddd68165e062cbc87d68ffdf865874ebfee2666510d340dd47d05"


def _first_attempt_dispatch() -> dict:
    return {
        "dispatch_slot": "correction",
        "candidate_sha256": FIRST_ATTEMPT_CANDIDATE,
        "raw_response_sha256": FIRST_ATTEMPT_RAW,
        "stage": "correction",
        "role": "author",
        "status": "returned",
    }


def _second_attempt_dispatch() -> dict:
    return {
        "dispatch_slot": "correction",
        "continuation_id": SECOND_CONTINUATION_ID,
        "candidate_sha256": SECOND_CANDIDATE_SHA256,
        "raw_response_sha256": SECOND_RAW_SHA256,
        "stage": "correction",
        "role": "author",
        "status": "returned",
    }


def _third_attempt_dispatch(slot: str = "correction") -> dict:
    return {
        "dispatch_slot": slot,
        "continuation_id": THIRD_CONTINUATION_ID,
        "stage": "correction" if slot == "correction" else "artifact_review",
        "role": "author" if slot == "correction" else "reviewer",
        "status": "returned",
    }


def _failing_record(name: str) -> dict:
    return {
        "name": name,
        "expected_outcome": "not_detected",
        "expected_claim_level": "command_attempt",
        "status": "runtime_failure",
        "observed_outcome": None,
        "observed_claim_level": None,
        "failure": "detected and not_detected results require evidence_refs",
        "actual_result": None,
    }


def _nine_failing_records() -> list[dict]:
    return [_failing_record(name) for name in THIRD_DEFECT_FAILING_ROWS]


def test_third_continuation_candidate_extraction_verifies_digests() -> None:
    plan, candidate_raw, info = _extract_third_continuation_authorities()

    assert info["candidate_raw_sha256"] == SECOND_RAW_SHA256
    assert info["candidate_sha256"] == SECOND_CANDIDATE_SHA256
    assert info["plan_sha256"] == ACCEPTED_PLAN_SHA256
    assert info["source_directory"].endswith(SECOND_ATTEMPT_DIRECTORY_NAME)
    frozen_plan, _, _ = _extract_authorities()
    assert plan == frozen_plan
    assert len(candidate_raw) == 4888


def test_third_continuation_guard_treats_prior_continuations_as_history() -> None:
    ledger = {"dispatches": [_first_attempt_dispatch(), _second_attempt_dispatch()]}

    ensure_dispatch_slot_available(ledger, "correction", continuation=THIRD_CONTINUATION_ID)
    ensure_dispatch_slot_available(ledger, "review", continuation=THIRD_CONTINUATION_ID)


def test_third_continuation_guard_refuses_second_dispatch_of_either_slot() -> None:
    ledger = {
        "dispatches": [
            _first_attempt_dispatch(),
            _second_attempt_dispatch(),
            _third_attempt_dispatch("correction"),
        ]
    }
    with pytest.raises(DispatchSlotSpent):
        ensure_dispatch_slot_available(ledger, "correction", continuation=THIRD_CONTINUATION_ID)

    review_ledger = {
        "dispatches": [
            _first_attempt_dispatch(),
            _second_attempt_dispatch(),
            _third_attempt_dispatch("review"),
        ]
    }
    with pytest.raises(DispatchSlotSpent):
        ensure_dispatch_slot_available(review_ledger, "review", continuation=THIRD_CONTINUATION_ID)


def test_second_continuation_guard_still_skips_only_first_attempt() -> None:
    ledger = {"dispatches": [_first_attempt_dispatch(), _second_attempt_dispatch()]}
    with pytest.raises(DispatchSlotSpent):
        ensure_dispatch_slot_available(ledger, "correction", continuation=SECOND_CONTINUATION_ID)


def test_third_continuation_reconciliation_extends_exhausted_limits() -> None:
    reconciliation = reconcile_budget(third_continuation=True)

    assert reconciliation["historical_snapshot"] == {
        "author_correction_spent": 11,
        "author_correction_limit": 11,
        "review_spent": 5,
        "review_limit": 7,
        "task_spent": 14,
        "task_limit": 14,
        "aggregate_spent": 23,
        "aggregate_limit": 32,
    }
    assert reconciliation["intervening_spend_found"] is False
    resulting = reconciliation["resulting_budget"]
    assert resulting["author_correction_limit"] == 11 + AUTHOR_INCREMENT
    assert resulting["author_correction_remaining"] == 1
    assert resulting["review_limit"] == 7 + REVIEW_INCREMENT
    assert resulting["task_limit"] == 16
    assert resulting["task_remaining"] == 2
    assert resulting["aggregate_limit"] == 32
    assert resulting["aggregate_spent"] == 23
    authorization = reconciliation["authorization"]
    assert authorization["author_correction_increment"] == AUTHOR_INCREMENT
    assert authorization["review_increment"] == REVIEW_INCREMENT
    assert authorization["counter_reset"] is False
    assert authorization["borrowed_slots"] is False
    assert authorization["effective_new_correction_slots"] == 1
    assert authorization["effective_new_review_slots"] == 1


def test_third_continuation_intervening_scan_records_post_cutoff_spend(tmp_path) -> None:
    before = tmp_path / "O03-live-20260923T080000Z-artifact-completion"
    after_spent = tmp_path / "O03-live-20260923T100000Z-artifact-completion"
    after_idle = tmp_path / "O03-live-20260923T110000Z-artifact-completion"
    for directory in (before, after_spent, after_idle):
        directory.mkdir()
    (before / "live-evidence.json").write_text(
        json.dumps(
            {
                "ledger": {
                    "dispatches": [
                        {
                            "stage": "correction",
                            "role": "author",
                            "status": "returned",
                            "candidate_sha256": SECOND_CANDIDATE_SHA256,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (after_spent / "live-evidence.json").write_text(
        json.dumps(
            {
                "ledger": {
                    "dispatches": [
                        {
                            "stage": "correction",
                            "role": "author",
                            "status": "returned",
                            "candidate_sha256": SECOND_CANDIDATE_SHA256,
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    found = _third_continuation_intervening_dispatches(tmp_path)

    assert len(found) == 1
    assert found[0]["stage"] == "correction"
    assert found[0]["candidate_sha256"] == SECOND_CANDIDATE_SHA256
    assert "20260923T100000Z" in found[0]["path"]


def test_third_continuation_findings_state_both_error_layers() -> None:
    findings = _third_continuation_contract_findings(_nine_failing_records())

    assert len(findings) == 4
    detail_text = " ".join(item["detail"] for item in findings)
    for row in THIRD_DEFECT_FAILING_ROWS:
        assert row in detail_text
    assert "Defect A" in detail_text
    assert "Defect B" in detail_text
    assert "Defect C" in detail_text
    # Both layers are stated: the validation rejection of empty refs and the
    # underlying verdict error.
    assert "empty evidence_refs" in detail_text
    assert "the absence verdict itself is correct" in detail_text
    assert "never not_detected" in detail_text
    assert "not validly established" in detail_text
    assert "availability.tool_calls" in detail_text
    assert "completeness.tool_calls" in detail_text
    assert "Repair all three defects together" in detail_text


def test_third_continuation_findings_reject_a_changed_failure_set() -> None:
    records = _nine_failing_records()[:-1]
    with pytest.raises(ValueError, match="three-defect diagnosis"):
        _third_continuation_contract_findings(records)


def test_thinking_extra_body_is_parametrized_per_dispatch() -> None:
    assert AUTHORING_THINKING_EXTRA_BODY == {"chat_template_kwargs": {"enable_thinking": False}}
    assert _thinking_extra_body(True) == {"chat_template_kwargs": {"enable_thinking": True}}
    assert _thinking_extra_body(False) == AUTHORING_THINKING_EXTRA_BODY
    assert AUTHORING_THINKING_EXTRA_BODY["chat_template_kwargs"]["enable_thinking"] is False


def test_third_correction_dispatch_uses_thinking_and_review_does_not() -> None:
    assert _continuation_thinking(THIRD_CONTINUATION_ID, "correction") is True
    assert _continuation_thinking(THIRD_CONTINUATION_ID, "review") is False
    assert _continuation_thinking(SECOND_CONTINUATION_ID, "correction") is False
    assert _continuation_thinking(None, "correction") is False

    profile = SimpleNamespace(
        base_url="http://127.0.0.1:1",
        api_key="unused",
        model="gemma-4-26b-a4b-it",
        name="gemma4-oc",
    )
    correction_transport = _dispatch_transport_for(profile, enable_thinking=True)
    review_transport = _dispatch_transport_for(profile, enable_thinking=False)
    assert correction_transport.extra_body == {"chat_template_kwargs": {"enable_thinking": True}}
    assert correction_transport.max_retries == 0
    assert correction_transport.temperature == 0.0
    assert review_transport.extra_body == {"chat_template_kwargs": {"enable_thinking": False}}


def test_live_controls_record_the_effective_thinking_flag() -> None:
    placeholder = SimpleNamespace(model="gemma-4-26b-a4b-it", temperature=0.0)
    enabled = _live_controls(
        transport=placeholder,
        supplied={"extra_body": _thinking_extra_body(True)},
    )
    assert enabled["thinking"] is True
    assert enabled["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}

    pinned = _pinned_controls(thinking=True)
    assert pinned["thinking"] is True
    assert pinned["max_retries"] == 0
    assert pinned["temperature"] == 0.0
    assert _pinned_controls(thinking=False)["thinking"] is False


def test_final_output_failures_end_the_correction_allowance() -> None:
    empty = _final_output_failure(b"", None)
    assert empty is not None
    assert empty["code"] == "empty_final_output"

    truncated = _final_output_failure(
        b"partial",
        {"finish_reason": {"state": "value", "value": "length"}},
    )
    assert truncated is not None
    assert truncated["code"] == "truncated_final_output"

    complete = _final_output_failure(
        b"complete",
        {"finish_reason": {"state": "value", "value": "stop"}},
    )
    assert complete is None


def _third_continuation_replacement_budget() -> AuthoringBudget:
    snapshot = reconcile_budget(third_continuation=True)["resulting_budget"]
    return AuthoringBudget(
        aggregate_limit=snapshot["aggregate_limit"],
        task_limit=snapshot["task_limit"],
        author_limit=snapshot["author_correction_limit"],
        review_limit=snapshot["review_limit"],
        total_dispatched=snapshot["aggregate_spent"],
        dispatched_by_task={LIVE_TASK_ID: snapshot["task_spent"]},
        dispatched_by_task_role={
            LIVE_TASK_ID: {
                "author": snapshot["author_correction_spent"],
                "reviewer": snapshot["review_spent"],
            }
        },
    )


def test_third_continuation_budget_reserves_correction_then_review() -> None:
    budget = _third_continuation_replacement_budget()

    budget.reserve(LIVE_TASK_ID, role="author")
    assert budget.snapshot(LIVE_TASK_ID)["task_spent"] == 15
    assert budget.snapshot(LIVE_TASK_ID)["author_correction_spent"] == 12

    budget.reserve(LIVE_TASK_ID, role="reviewer")
    assert budget.snapshot(LIVE_TASK_ID)["task_spent"] == 16
    assert budget.snapshot(LIVE_TASK_ID)["review_spent"] == 6
    assert budget.snapshot(LIVE_TASK_ID)["aggregate_spent"] == 25


def test_third_dry_run_renders_fitting_packet(tmp_path, monkeypatch) -> None:
    from scripts.continuations import o03_artifact_completion as script

    live_evidence = json.loads(
        (script.RUNS_ROOT / SECOND_ATTEMPT_DIRECTORY_NAME / "live-evidence.json").read_text(
            encoding="utf-8"
        )
    )
    records = live_evidence["replacement_controls"]["records"]

    def fake_controls(detector_bytes, *, cases):
        findings = [
            {
                "code": "detector_control_runtime_failure",
                "detail": (
                    f"independent detector control {record['name']!r} reported "
                    f"{record['failure']!r}"
                ),
                "path": f"detector_controls.{record['name']}",
            }
            for record in records
            if record["status"] != "passed"
        ]
        return findings, records

    monkeypatch.setattr(script, "run_detector_controls", fake_controls)

    output = run_dry_run(tmp_path / "third-dry-run", third_continuation=True)

    dry_run = json.loads((output / "dry-run.json").read_text(encoding="utf-8"))
    assert dry_run["schema_version"] == "o03-artifact-completion-dry-run-v3"
    assert dry_run["continuation_id"] == THIRD_CONTINUATION_ID
    assert dry_run["model_requests"] == 0
    assert dry_run["saved_candidate_sha256"] == SECOND_CANDIDATE_SHA256
    assert dry_run["accepted_plan_sha256"] == ACCEPTED_PLAN_SHA256
    assert dry_run["control_failures"] == list(THIRD_DEFECT_FAILING_ROWS)
    assert dry_run["preflight"]["fits"] is True
    assert dry_run["preflight"]["estimator"] == "utf8_bytes_conservative_prompt_estimate"

    ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["continuation_id"] == THIRD_CONTINUATION_ID
    assert ledger["model_requests"] == 0

    reconciliation = json.loads(
        (output / "budget-reconciliation.json").read_text(encoding="utf-8")
    )
    assert reconciliation["schema_version"] == "authoring-budget-reconciliation-v3"
    assert reconciliation["historical_snapshot"]["author_correction_spent"] == 11
    assert reconciliation["resulting_budget"]["author_correction_limit"] == 12

    rendered = json.loads((output / "rendered-correction.json").read_text(encoding="utf-8"))
    assert rendered["response_contract_occurrences"] == 1
    assert rendered["candidate_python_occurrences"] == 1

    feedback = json.loads((output / "feedback.json").read_text(encoding="utf-8"))
    assert len(feedback) == 18

    user = (output / "correction.user.txt").read_text(encoding="utf-8")
    assert "PLAN FIELD MEANINGS" not in user
    assert (
        "The accepted plan requires no semantic judge. semantic_judge_spec must be null. "
        "This decision is fixed; correct the detector within it." in user
    )
    for row in THIRD_DEFECT_FAILING_ROWS:
        assert row in user
    assert "Repair all three defects together" in user
    assert "{{setup_draft_id}}" in user

    saved = json.loads((output / "saved-candidate.json").read_text(encoding="utf-8"))
    assert saved["raw"]["sha256"] == SECOND_RAW_SHA256
    assert base64.b64decode(saved["raw"]["base64"], validate=True)
