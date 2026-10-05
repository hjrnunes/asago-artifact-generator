from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.core import Finding, PromptPacket
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import AuthoringPolicy

from .support import ScriptedAuthoringTransport


def _orchestrator(tmp_path: Path, **overrides: Any) -> AuthoringOrchestrator:
    arguments: dict[str, Any] = {
        "transport": ScriptedAuthoringTransport([]),
        "package_dir": tmp_path / "package",
        "task_id": "state",
        "policy": AuthoringPolicy(),
    }
    return AuthoringOrchestrator(**{**arguments, **overrides})


class _RetryingTransport(ScriptedAuthoringTransport):
    max_retries = 2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"transport": _RetryingTransport([])}, "max_retries=0"),
        ({"discovery_provenance": ["not", "a", "mapping"]}, "must be a mapping"),
        ({"policy": object()}, "AuthoringPolicy instance"),
    ],
)
def test_orchestrator_rejects_invalid_arguments(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _orchestrator(tmp_path, **overrides)


@pytest.mark.parametrize(
    ("codes", "expected_status", "expected_codes"),
    [
        (["other"], None, None),
        (["transport_failure"], "transport_failure", ["transport_failure"]),
        (
            ["transport_failure", "budget_exhausted", "budget_exhausted"],
            "budget_exhausted",
            ["budget_exhausted", "budget_exhausted"],
        ),
        (
            ["budget_exhausted", "prompt_overflow", "transport_failure"],
            "prompt_overflow",
            ["prompt_overflow"],
        ),
        (
            ["correction_dispatch_failed", "transport_failure"],
            "transport_failure",
            ["correction_dispatch_failed", "transport_failure"],
        ),
    ],
)
def test_stop_for_author_findings_prefers_overflow_then_budget(
    codes: list[str], expected_status: str | None, expected_codes: list[str] | None
) -> None:
    stop = AuthoringOrchestrator._stop_for_author_findings(
        [Finding(code, "detail", "path") for code in codes]
    )
    if expected_status is None:
        assert stop is None
        return
    assert stop is not None
    assert stop.status == expected_status
    assert [finding.code for finding in stop.findings] == expected_codes


def test_latest_attempt_findings_skips_malformed_records(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    fallback = [Finding("fallback", "kept", "")]
    orchestrator._failure_evidence["attempts"] = [
        {
            "findings": [
                "not a mapping",
                {"code": 3, "detail": "bad code"},
                {"code": "no_detail"},
                {"code": "bad_path", "detail": "d", "path": 7, "details": ["x"]},
                {"code": "full", "detail": "d", "path": "call2", "details": {"k": 1}},
            ]
        }
    ]

    findings = orchestrator._latest_attempt_findings(fallback)

    assert [(f.code, f.path, f.details) for f in findings] == [
        ("bad_path", "", {}),
        ("full", "call2", {"k": 1}),
    ]

    orchestrator._failure_evidence["attempts"] = [{"findings": ["not a mapping"]}]
    assert orchestrator._latest_attempt_findings(fallback) == fallback
    orchestrator._failure_evidence["attempts"] = [{"findings": []}]
    assert orchestrator._latest_attempt_findings(fallback) == fallback


@pytest.mark.parametrize(
    ("finding_paths", "attempt", "expected"),
    [
        (["call1"], None, "plan"),
        (["artifact", "plan_review"], None, "plan"),
        (["plan_review", "artifact_review"], None, "artifact"),
        (["call2", "elsewhere"], None, "artifact"),
        (["elsewhere"], None, None),
        ([], {"stage": "call1"}, "plan"),
        ([], {"stage": "plan_review"}, "plan"),
        ([], {"stage": "call2"}, "artifact"),
        ([], {"stage": "artifact_review"}, "artifact"),
        ([], {"stage": "correction", "failed_stage": "call1"}, "plan"),
        ([], {"stage": "correction", "failed_stage": "call2"}, "artifact"),
        ([], {"stage": "mystery"}, None),
        ([], {}, None),
    ],
)
def test_terminal_stage_uses_finding_paths_then_the_last_attempt(
    tmp_path: Path, finding_paths: list[str], attempt: dict[str, Any] | None, expected: str | None
) -> None:
    orchestrator = _orchestrator(tmp_path)
    if attempt is not None:
        orchestrator._failure_evidence["attempts"] = [attempt]

    findings = [Finding("code", "detail", path) for path in finding_paths]

    assert orchestrator._terminal_stage(findings) == expected


def test_finish_failure_evidence_without_attempts_or_findings_writes_nothing(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(tmp_path)

    assert orchestrator._finish_failure_evidence("failed", []) is None
    assert orchestrator._failure_evidence["status"] == "in_progress"


def test_finish_failure_evidence_stamps_records_and_closes_the_aggregate(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator._failure_evidence["attempts"] = [
        {"stage": "call1", "findings": [{"code": "late", "detail": "d", "path": "call1"}]},
    ]
    orchestrator._failure_evidence["aggregate"] = {"spent_before": 4}
    orchestrator._ledger.append({})

    path = orchestrator._finish_failure_evidence("failed", [Finding("early", "d", "call2")])

    evidence = orchestrator._failure_evidence
    assert path is not None and path.exists()
    assert evidence["aggregate"]["spent_after"] == 5
    assert evidence["terminal"] == {"stage": "plan", "attempt_index": 0, "reason": "late"}
    for record in (evidence["attempts"][0], orchestrator._ledger[0]):
        assert (record["terminal_status"], record["stage_status"]) == ("failed", "failed")

    packaged = _orchestrator(tmp_path / "other")
    packaged._failure_evidence["attempts"] = [{"stage": "call2"}]
    packaged._finish_failure_evidence("packaged", [])
    assert packaged._failure_evidence["findings"] == []
    assert packaged._failure_evidence["terminal"] == {
        "stage": "artifact",
        "attempt_index": 0,
        "reason": "packaged",
    }


def _review_packet(stage: str) -> PromptPacket:
    return PromptPacket(
        stage=stage,
        version="review-test",
        system="system",
        user="user",
        payload={"candidate_plan": {"a": 1}, "scenario": "s"},
    )


def test_set_review_evidence_copies_only_the_decision_fields(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator._ledger.append({})
    orchestrator._failure_evidence["attempts"] = [{}]
    review = {
        "decision": "revise",
        "original_decision": "revise",
        "decision_after_scope_filter": "accept",
        "summary": "scripted",
        "findings": [{"question": "q"}],
        "out_of_scope_findings": [],
        "question_ids": ["q"],
        "unrelated": "ignored",
    }

    orchestrator._set_review_evidence(
        status="reviewed",
        effective_controls={"thinking": "off"},
        packet=_review_packet("plan_review"),
        review=review,
    )

    evidence = orchestrator._ledger[-1]["review"]
    assert {key: evidence[key] for key in review if key != "unrelated"} == {
        key: value for key, value in review.items() if key != "unrelated"
    }
    assert "unrelated" not in evidence
    assert evidence["status"] == "reviewed"
    assert evidence["effective_controls"] == {"thinking": "off"}
    assert evidence["reviewed_candidate_sha256"] == evidence["candidate_bytes_sha256"]
    assert evidence["reviewed_input_sha256"] != evidence["reviewed_candidate_sha256"]
    assert orchestrator._failure_evidence["attempts"][-1]["review"] == evidence
    assert orchestrator._review_evidence["plan"] == evidence
    assert evidence["findings"] is not review["findings"]

    orchestrator._set_review_evidence(
        status="unavailable",
        effective_controls={},
        packet=_review_packet("artifact_review"),
    )
    assert orchestrator._review_evidence["artifact"]["status"] == "unavailable"
    assert "decision" in orchestrator._review_evidence["artifact"]


def test_set_review_evidence_keeps_pinned_digests_and_records_the_raw_response(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator._ledger.append(
        {
            "reviewed_input_sha256": "input",
            "reviewed_candidate_sha256": "candidate",
            "candidate_bytes_sha256": "bytes",
            "raw_response_key": "review-plan",
        }
    )
    orchestrator._failure_evidence["attempts"] = [{}]
    orchestrator._raw_responses["review-plan"] = b"raw"

    orchestrator._set_review_evidence(
        status="reviewed", effective_controls={}, packet=_review_packet("plan_review")
    )

    evidence = orchestrator._ledger[-1]["review"]
    assert evidence["reviewed_input_sha256"] == "input"
    assert evidence["reviewed_candidate_sha256"] == "candidate"
    assert evidence["candidate_bytes_sha256"] == "bytes"
    assert evidence["raw_response_key"] == "review-plan"
    assert evidence["raw_response_bytes"] == 3


@pytest.mark.parametrize("missing", ["ledger", "attempts", "dispatch"])
def test_set_review_evidence_waits_for_a_recorded_dispatch(tmp_path: Path, missing: str) -> None:
    orchestrator = _orchestrator(tmp_path)
    if missing != "ledger":
        orchestrator._ledger.append({})
    if missing != "attempts":
        orchestrator._failure_evidence["attempts"] = [{}]
    orchestrator._dispatch_recorded = missing != "dispatch"

    orchestrator._set_review_evidence(
        status="reviewed", effective_controls={}, packet=_review_packet("plan_review")
    )

    assert all("review" not in record for record in orchestrator._ledger)
    assert orchestrator._review_evidence == {}
