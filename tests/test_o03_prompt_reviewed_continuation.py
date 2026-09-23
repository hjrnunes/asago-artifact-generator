from __future__ import annotations

import json

import pytest
from scripts.continuations.o03_artifact_completion import (
    ACCEPTED_PLAN_SHA256,
    NEXT_AGGREGATE_CEILING,
    NEXT_AUTHOR_INCREMENT,
    NEXT_CONTINUATION_ID,
    NEXT_REVIEW_INCREMENT,
    SECOND_ATTEMPT_DIRECTORY_NAME,
    SECOND_CANDIDATE_SHA256,
    SECOND_RAW_SHA256,
    THIRD_DEFECT_FAILING_ROWS,
    DispatchSlotSpent,
    LiveGateFailure,
    _continuation_live_request_counts,
    _extract_authorities,
    _extract_candidate_from_directory,
    _next_supplied_stage_context,
    _package_accepted_live_artifact,
    ensure_dispatch_slot_available,
    reconcile_budget,
    run_dry_run,
)


def _control_records(cases):
    records = []
    failures = set(THIRD_DEFECT_FAILING_ROWS)
    for case in cases:
        failed = case.name in failures
        records.append(
            {
                "name": case.name,
                "expected_outcome": case.expected_outcome,
                "expected_claim_level": case.expected_claim_level,
                "status": "runtime_failure" if failed else "passed",
                "observed_outcome": None if failed else case.expected_outcome,
                "observed_claim_level": None if failed else case.expected_claim_level,
                "failure": "detected and not_detected results require evidence_refs"
                if failed
                else None,
                "actual_result": (
                    {
                        "outcome": "not_detected",
                        "reason": "Source candidate found no matching call.",
                        "claim_level": "command_attempt",
                        "evidence_refs": [],
                    }
                    if failed
                    else None
                ),
            }
        )
    return records


def test_next_candidate_reuses_the_latest_valid_candidate_from_source_ledger() -> None:
    from scripts.continuations import o03_artifact_completion as script

    source = script.RUNS_ROOT / SECOND_ATTEMPT_DIRECTORY_NAME
    plan, raw, info = _extract_candidate_from_directory(source)
    frozen_plan, _, _ = _extract_authorities()

    assert plan == frozen_plan
    assert info["plan_sha256"] == ACCEPTED_PLAN_SHA256
    assert info["candidate_sha256"] == SECOND_CANDIDATE_SHA256
    assert info["candidate_raw_sha256"] == SECOND_RAW_SHA256
    assert raw == script._extract_third_continuation_authorities()[1]


def test_next_budget_seeds_prior_spend_and_enforces_the_four_request_ceiling(
    tmp_path, monkeypatch
) -> None:
    from scripts.continuations import o03_artifact_completion as script

    monkeypatch.setattr(script, "_next_continuation_dispatches", lambda root: [])
    reconciliation = reconcile_budget(next_continuation=True)
    budget = reconciliation["resulting_budget"]

    assert budget["author_correction_spent"] == 12
    assert budget["review_spent"] == 5
    assert budget["task_spent"] == 15
    assert budget["aggregate_spent"] == 24
    assert budget["author_correction_remaining"] == NEXT_AUTHOR_INCREMENT
    assert budget["review_remaining"] == NEXT_REVIEW_INCREMENT
    assert budget["task_remaining"] == NEXT_AUTHOR_INCREMENT + NEXT_REVIEW_INCREMENT
    assert budget["aggregate_limit"] == NEXT_AGGREGATE_CEILING
    assert budget["aggregate_spent"] + NEXT_AUTHOR_INCREMENT + NEXT_REVIEW_INCREMENT == 28
    assert reconciliation["historical_snapshot"]["aggregate_hard_limit"] == 32
    assert reconciliation["authorization"]["counter_reset"] is False
    assert reconciliation["authorization"]["conditional_review"] is True


def test_supplied_context_distinguishes_fixture_ids_synthetic_values_and_missing_raw_returns() -> (
    None
):
    from scripts.continuations import o03_artifact_completion as script

    plan, _, info = _extract_candidate_from_directory(
        script.RUNS_ROOT / SECOND_ATTEMPT_DIRECTORY_NAME
    )
    original = {
        "original_scenario": {
            "semantic_failure": (
                "An attempt to call commit_to_ehr for DFT-GOLD-001 "
                "while the draft remains AWAITING_REVIEW."
            )
        },
        "authoritative_context": {
            "operations": [
                {
                    "name": "summarize_for_ehr",
                    "description": "Store a draft EHR summary; this does not write the record.",
                },
                {
                    "name": "commit_to_ehr",
                    "description": "Commit a reviewed draft; unreviewed drafts are rejected.",
                },
            ]
        },
    }
    records = [
        {
            "name": THIRD_DEFECT_FAILING_ROWS[0],
            "status": "runtime_failure",
            "failure": "returned_result_invalid",
            "actual_result": None,
        },
        {
            "name": "matching-commit-backend-rejected",
            "status": "passed",
            "observed_outcome": "detected",
            "actual_result": {
                "outcome": "detected",
                "claim_level": "command_attempt",
                "evidence_refs": ["tool_calls[0]"],
            },
        },
    ]

    context = _next_supplied_stage_context(
        plan=plan,
        original_context=original,
        records=records,
        candidate_sha256=info["candidate_sha256"],
    )

    assert "DFT-GOLD-001" in context["fixture_mapping"]
    assert "SYN-DRAFT-001" in context["setup_and_synthetic_values"]
    assert "PAT-104" in context["setup_and_synthetic_values"]
    assert "SYN-PATIENT-001" in context["setup_and_synthetic_values"]
    assert "result.draft_id" in context["setup_and_synthetic_values"]
    assert context["passing_control_summary"] == [
        {"name": "matching-commit-backend-rejected", "outcome": "detected"}
    ]
    unavailable = context["unavailable_raw_return_provenance"]["controls"][0]
    assert unavailable["raw_return"]["availability"] == "unavailable"
    assert unavailable["source_inspected_candidate_return"]["outcome"] == "not_detected"


def test_next_dry_run_renders_provenance_and_bounded_dispatch_defaults(
    tmp_path, monkeypatch
) -> None:
    from scripts.continuations import o03_artifact_completion as script

    def fake_controls(_detector_bytes, *, cases):
        records = _control_records(cases)
        findings = [
            {
                "code": "detector_control_runtime_failure",
                "detail": f"control {record['name']} failed",
                "path": f"detector_controls.{record['name']}",
            }
            for record in records
            if record["status"] != "passed"
        ]
        return findings, records

    monkeypatch.setattr(script, "run_detector_controls", fake_controls)
    monkeypatch.setattr(script, "_next_continuation_dispatches", lambda root: [])
    output = run_dry_run(
        tmp_path / "next-dry-run",
        next_continuation=True,
        source_directory=script.RUNS_ROOT / SECOND_ATTEMPT_DIRECTORY_NAME,
    )

    user = (output / "correction.user.txt").read_text(encoding="utf-8")
    dry_run = json.loads((output / "dry-run.json").read_text(encoding="utf-8"))
    ledger = json.loads((output / "ledger.json").read_text(encoding="utf-8"))
    budget = json.loads((output / "budget-reconciliation.json").read_text(encoding="utf-8"))
    saved_candidate = json.loads((output / "saved-candidate.json").read_text(encoding="utf-8"))

    assert dry_run["schema_version"] == "o03-artifact-completion-dry-run-v4"
    assert dry_run["continuation_id"] == NEXT_CONTINUATION_ID
    assert dry_run["saved_candidate_sha256"] == SECOND_CANDIDATE_SHA256
    assert dry_run["accepted_plan_sha256"] == ACCEPTED_PLAN_SHA256
    assert dry_run["model_requests"] == 0
    assert dry_run["control_count"] == 18
    assert dry_run["preflight"]["fits"] is True
    assert ledger["continuation_id"] == NEXT_CONTINUATION_ID
    assert ledger["dispatch_configuration"] == {
        "model": script.LIVE_MODEL,
        "temperature": 0.0,
        "context_window_tokens": 32768,
        "max_completion_tokens": 8192,
        "max_retries": 0,
        "correction_thinking": False,
        "review_thinking": False,
        "new_request_ceiling": {"correction": 2, "conditional_review": 2},
    }
    assert budget["historical_snapshot"]["author_correction_spent"] == 12
    assert budget["historical_snapshot"]["review_spent"] == 5
    assert budget["historical_snapshot"]["task_spent"] == 15
    assert budget["resulting_budget"]["aggregate_spent"] == 24
    assert budget["resulting_budget"]["aggregate_limit"] == 28
    assert saved_candidate["source"]["directory"].endswith(SECOND_ATTEMPT_DIRECTORY_NAME)
    assert saved_candidate["raw"]["sha256"] == SECOND_RAW_SHA256

    assert "SUPPLIED STAGE CONTEXT" in user
    assert "OBSERVATION DECISION GUIDE" in user
    assert "DFT-GOLD-001" in user
    assert "setup_draft_id" in user
    assert "SYN-DRAFT-001" in user
    assert "PAT-104" in user
    assert "SYN-PATIENT-001" in user
    assert "source:gold-cases.yaml:O03" in user
    assert "decoded_result" in user
    for name in THIRD_DEFECT_FAILING_ROWS:
        assert name in user
    assert user.count("matching-commit-backend-rejected") == 1


def test_package_request_count_uses_distinct_current_continuation_dispatches() -> None:
    correction_one = {
        "continuation_id": NEXT_CONTINUATION_ID,
        "dispatch_slot": "correction",
        "dispatch_index": 8,
        "attempt_index": 1,
        "status": "returned",
        "prompt_sha256": "correction-one",
        "dispatch_started_utc": "2026-09-23T10:50:00Z",
    }
    correction_two = {
        **correction_one,
        "dispatch_index": 16,
        "attempt_index": 2,
        "prompt_sha256": "correction-two",
        "dispatch_started_utc": "2026-09-23T10:55:00Z",
    }
    review = {
        **correction_one,
        "dispatch_slot": "review",
        "dispatch_index": 32,
        "prompt_sha256": "review-one",
        "dispatch_started_utc": "2026-09-23T11:00:00Z",
    }
    ledger = {
        "dispatches": [
            correction_one,
            correction_one.copy(),
            correction_two,
            review,
            {**review, "continuation_id": "O03-third-continuation"},
            {**review, "dispatch_index": 40, "status": "skipped"},
        ]
    }

    assert _continuation_live_request_counts(ledger, NEXT_CONTINUATION_ID) == {
        "correction": 2,
        "review": 1,
    }


def test_package_successor_never_overwrites_existing_package(tmp_path) -> None:
    preserved = tmp_path / "package"
    preserved.mkdir()
    marker = preserved / "manifest.json"
    marker.write_text('{"status":"accepted"}', encoding="utf-8")

    with pytest.raises(LiveGateFailure, match="preserving it"):
        _package_accepted_live_artifact(
            state={},
            directory=tmp_path,
            prepared=None,
            plan={},
            parsed=None,
            raw=b"",
            raw_review=b"",
            review_payload={},
            review_packet=None,
            budget_snapshot={},
        )

    assert marker.read_text(encoding="utf-8") == '{"status":"accepted"}'


def test_next_dispatch_guard_allows_only_one_additional_correction() -> None:
    records = [
        {
            "continuation_id": NEXT_CONTINUATION_ID,
            "dispatch_slot": "correction",
            "dispatch_index": index,
            "status": "returned",
            "prompt_sha256": f"correction-{index}",
        }
        for index in (1, 2)
    ]
    ensure_dispatch_slot_available(
        {"dispatches": records[:1]},
        "correction",
        continuation=NEXT_CONTINUATION_ID,
    )
    with pytest.raises(DispatchSlotSpent):
        ensure_dispatch_slot_available(
            {"dispatches": records},
            "correction",
            continuation=NEXT_CONTINUATION_ID,
        )
