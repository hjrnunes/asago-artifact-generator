from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.core import Finding, PromptPacket
from asago_artifact_generator.authoring.journal import (
    AttemptFailed,
    CorrectionRecorded,
    DispatchRequested,
)
from asago_artifact_generator.authoring.orchestrator import (
    AuthoringOrchestrator,
    _staged,
)
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


def test_failed_correction_stop_classifies_the_recorded_findings(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    pending = [Finding("plan_validation", "pending", "interpretation")]

    unresolved = orchestrator._failed_correction_stop(pending)

    assert (unresolved.status, unresolved.findings) == ("unresolved", tuple(pending))

    recorded = [
        Finding("plan_validation", "kept", "interpretation"),
        Finding("transport_failure", "provider down", "call1"),
    ]
    orchestrator._findings.extend(recorded)

    transport = orchestrator._failed_correction_stop(pending)

    assert (transport.status, transport.findings) == ("transport_failure", (recorded[1],))

    orchestrator._findings.pop()

    assert orchestrator._failed_correction_stop(pending).findings == (recorded[0],)


def _open(
    orchestrator: AuthoringOrchestrator, stage: str, failed_stage: str | None = None
) -> None:
    """Journal one dispatch of ``stage``; a correction also names the stage it replaces."""

    packet = PromptPacket(stage=stage, version="v", system="s", user="u", payload={})
    orchestrator._journal.append(orchestrator._dispatch_opened(packet, 1, "author"))
    if failed_stage is not None:
        orchestrator._journal.append(CorrectionRecorded(failed_stage, "correction", b"x"))


def test_latest_attempt_findings_read_the_latest_dispatch_only(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    fallback = [Finding("fallback", "kept", "")]
    assert orchestrator._latest_attempt_findings(fallback) == fallback

    _open(orchestrator, "call1")
    orchestrator._journal.append(AttemptFailed((Finding("stale", "d", "call1"),)))
    _open(orchestrator, "call2")
    assert orchestrator._latest_attempt_findings(fallback) == fallback

    latest = (Finding("full", "d", "call2", {"k": 1}, stage="artifact"), Finding("second", "d"))
    orchestrator._journal.append(AttemptFailed(latest[:1]), AttemptFailed(latest[1:]))

    findings = orchestrator._latest_attempt_findings(fallback)

    assert [(f.code, f.path, f.details, f.stage) for f in findings] == [
        ("full", "call2", {"k": 1}, "artifact"),
        ("second", "", {}, None),
    ]


@pytest.mark.parametrize(
    ("finding_paths", "attempt", "expected"),
    [
        (["call1"], None, "plan"),
        (["artifact", "plan_review"], None, "plan"),
        (["plan_review", "artifact_review"], None, "artifact"),
        (["call2", "elsewhere"], None, "artifact"),
        (["elsewhere"], None, None),
        (["tool_call_condition_status"], ("call2", None), "plan"),
        ([], ("call1", None), "plan"),
        ([], ("plan_review", None), "plan"),
        ([], ("call2", None), "artifact"),
        ([], ("artifact_review", None), "artifact"),
        ([], ("correction", "call1"), "plan"),
        ([], ("correction", "call2"), "artifact"),
        ([], ("correction", None), "artifact"),
        ([], ("mystery", None), None),
    ],
)
def test_terminal_stage_uses_finding_stages_then_the_last_attempt(
    tmp_path: Path,
    finding_paths: list[str],
    attempt: tuple[str, str | None] | None,
    expected: str | None,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    if attempt is not None:
        _open(orchestrator, *attempt)

    findings = [_staged(Finding("code", "detail", path)) for path in finding_paths]

    assert orchestrator._terminal_stage(findings) == expected


def test_terminal_stage_reads_the_stage_not_the_path(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    _open(orchestrator, "call1")

    checked = Finding("unknown_reference", "d", "selected_evidence[0].ref", stage="artifact")
    unstaged = Finding("code", "d", "call2")

    assert orchestrator._terminal_stage([checked]) == "artifact"
    assert orchestrator._terminal_stage([unstaged]) == "plan"


def test_staged_marks_only_stage_key_paths() -> None:
    assert _staged(Finding("c", "d", "call2")).stage == "artifact"
    assert _staged(Finding("c", "d", "tool_call_condition_status")).stage == "plan"
    assert _staged(Finding("c", "d", "correction")).stage is None
    assert _staged(Finding("c", "d", "runtime_bindings[0]", stage="plan")).stage is None


def test_finish_without_attempts_or_findings_writes_nothing(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)

    assert orchestrator._finish("failed", []) is None
    assert orchestrator._journal.evidence["status"] == "in_progress"


def test_finish_stamps_records_with_the_terminal_status(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    _open(orchestrator, "call1")
    orchestrator._journal.append(AttemptFailed((Finding("late", "d", "call1"),)))

    path = orchestrator._finish("failed", [Finding("early", "d", "call2")])

    evidence = orchestrator._journal.evidence
    assert path is not None and path.exists()
    assert evidence["terminal"] == {"stage": "plan", "attempt_index": 0, "reason": "late"}
    for record in (evidence["attempts"][0], orchestrator._journal.ledger[0]):
        assert (record["terminal_status"], record["stage_status"]) == ("failed", "failed")

    accepted = _orchestrator(tmp_path / "other")
    _open(accepted, "call2")
    accepted._journal.append(AttemptFailed((Finding("stale", "d", "call1"),)))
    accepted._finish("accepted", [])
    assert accepted._journal.evidence["findings"] == []
    assert accepted._journal.evidence["terminal"] == {
        "stage": "artifact",
        "attempt_index": 0,
        "reason": "accepted",
    }


@pytest.mark.parametrize("opened", [(), ("call1",), ("call1", "plan_review")])
def test_set_review_evidence_waits_for_an_open_review_dispatch(
    tmp_path: Path, opened: tuple[str, ...]
) -> None:
    orchestrator = _orchestrator(tmp_path)
    for stage in opened:
        orchestrator._journal.append(DispatchRequested(stage))
        if stage != "plan_review":
            _open(orchestrator, stage)
    packet = PromptPacket(stage="plan_review", version="v", system="s", user="u", payload={})
    events = list(orchestrator._journal.events)

    orchestrator._set_review_evidence(status="accepted", effective_controls={}, packet=packet)

    assert orchestrator._journal.events == events
    assert all("review" not in record for record in orchestrator._journal.ledger)
