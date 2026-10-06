from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.core import Finding
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
    orchestrator._journal.evidence["attempts"] = [
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

    orchestrator._journal.evidence["attempts"] = [{"findings": ["not a mapping"]}]
    assert orchestrator._latest_attempt_findings(fallback) == fallback
    orchestrator._journal.evidence["attempts"] = [{"findings": []}]
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
        orchestrator._journal.evidence["attempts"] = [attempt]

    findings = [Finding("code", "detail", path) for path in finding_paths]

    assert orchestrator._terminal_stage(findings) == expected


def test_finish_without_attempts_or_findings_writes_nothing(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)

    assert orchestrator._finish("failed", []) is None
    assert orchestrator._journal.evidence["status"] == "in_progress"


def test_finish_stamps_records_with_the_terminal_status(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator._journal.evidence["attempts"] = [
        {"stage": "call1", "findings": [{"code": "late", "detail": "d", "path": "call1"}]},
    ]
    orchestrator._journal.ledger.append({})

    path = orchestrator._finish("failed", [Finding("early", "d", "call2")])

    evidence = orchestrator._journal.evidence
    assert path is not None and path.exists()
    assert evidence["terminal"] == {"stage": "plan", "attempt_index": 0, "reason": "late"}
    for record in (evidence["attempts"][0], orchestrator._journal.ledger[0]):
        assert (record["terminal_status"], record["stage_status"]) == ("failed", "failed")

    accepted = _orchestrator(tmp_path / "other")
    accepted._journal.evidence["attempts"] = [
        {"stage": "call2", "findings": [{"code": "stale", "detail": "d", "path": "call1"}]},
    ]
    accepted._finish("accepted", [])
    assert accepted._journal.evidence["findings"] == []
    assert accepted._journal.evidence["terminal"] == {
        "stage": "artifact",
        "attempt_index": 0,
        "reason": "accepted",
    }
