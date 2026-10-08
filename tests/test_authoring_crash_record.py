"""An exception the state machine does not handle leaves a terminal failure record."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

import pytest

from asago_artifact_generator.authoring import orchestrator as orchestrator_module

from .policy_support import NO_REVIEW, ONE_PLAN_CORRECTION, run_refund
from .support import ScriptedAuthoringTransport, load_failure_evidence, world_builders

_framed, _plan = world_builders("refund", "framed", "plan")

CRASH_CODE = "authoring_crashed"


def _raising(exc: Exception) -> Any:
    def boom(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise exc

    return boom


def _sidecar(tmp_path: Path) -> dict[str, Any]:
    return load_failure_evidence(tmp_path / "package.failure-evidence.json")


def _crash_finding(detail: str, stage: str | None) -> dict[str, Any]:
    return {"code": CRASH_CODE, "detail": detail, "path": "run", "stage": stage}


def test_a_crash_before_the_first_dispatch_ends_the_sidecar_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        orchestrator_module,
        "build_call1_packet_v2",
        _raising(KeyError("establish_context")),
    )

    with pytest.raises(KeyError, match="establish_context"):
        run_refund(tmp_path, ScriptedAuthoringTransport([]))

    evidence = _sidecar(tmp_path)
    assert evidence["status"] == "failed"
    assert evidence["attempts"] == []
    assert evidence["findings"] == [_crash_finding("KeyError: 'establish_context'", "plan")]
    assert evidence["terminal"] == {
        "stage": "plan",
        "attempt_index": None,
        "reason": CRASH_CODE,
    }
    assert not (tmp_path / "package").exists()


def test_a_crash_after_a_failed_attempt_reports_the_crash_not_the_earlier_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        orchestrator_module, "build_correction_context", _raising(RuntimeError("no context"))
    )

    with pytest.raises(RuntimeError, match="no context"):
        run_refund(tmp_path, ScriptedAuthoringTransport([b"{}"]), ONE_PLAN_CORRECTION)

    evidence = _sidecar(tmp_path)
    assert len(evidence["attempts"]) == 1
    assert evidence["attempts"][0]["findings"]
    assert evidence["status"] == "failed"
    assert evidence["findings"] == [_crash_finding("RuntimeError: no context", "plan")]
    assert evidence["terminal"] == {
        "stage": "plan",
        "attempt_index": 0,
        "reason": CRASH_CODE,
    }
    assert {attempt["terminal_status"] for attempt in evidence["attempts"]} == {"failed"}


def test_a_crash_while_assembling_the_package_names_the_artifact_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        orchestrator_module, "_package_from_responses", _raising(ValueError("bad member"))
    )
    transport = ScriptedAuthoringTransport([json.dumps(_plan()), _framed()])

    with pytest.raises(ValueError, match="bad member"):
        run_refund(tmp_path, transport, NO_REVIEW)

    evidence = _sidecar(tmp_path)
    assert evidence["status"] == "failed"
    assert evidence["terminal"] == {
        "stage": "artifact",
        "attempt_index": 1,
        "reason": CRASH_CODE,
    }
    assert [finding["code"] for finding in evidence["findings"]] == [CRASH_CODE]
    assert not (tmp_path / "package").exists()


def test_a_crash_after_the_first_stage_keeps_the_stage_it_was_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        orchestrator_module, "build_call2_packet_v2", _raising(KeyError("turn_purpose"))
    )

    with pytest.raises(KeyError):
        run_refund(tmp_path, ScriptedAuthoringTransport([json.dumps(_plan())]), NO_REVIEW)

    evidence = _sidecar(tmp_path)
    assert evidence["terminal"] == {
        "stage": "artifact",
        "attempt_index": 0,
        "reason": CRASH_CODE,
    }


def test_a_failing_sidecar_write_keeps_the_original_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = KeyError("establish_context")
    holder: dict[str, Any] = {}

    def crash_and_break_the_journal(*_args: Any, **_kwargs: Any) -> NoReturn:
        holder["journal"].flush = _raising(OSError("disk full"))
        raise original

    monkeypatch.setattr(orchestrator_module, "build_call1_packet_v2", crash_and_break_the_journal)
    real_init = orchestrator_module.AuthoringOrchestrator.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        holder["journal"] = self._journal

    monkeypatch.setattr(orchestrator_module.AuthoringOrchestrator, "__init__", init)

    with pytest.raises(KeyError) as raised:
        run_refund(tmp_path, ScriptedAuthoringTransport([]))

    assert raised.value is original
    assert _sidecar(tmp_path)["status"] == "in_progress"


def test_the_crash_detail_is_redacted_and_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    message = "cannot reach https://models.example/v1?api_key=sk-secret " + "x" * 400
    monkeypatch.setattr(
        orchestrator_module, "build_call1_packet_v2", _raising(RuntimeError(message))
    )

    with pytest.raises(RuntimeError):
        run_refund(tmp_path, ScriptedAuthoringTransport([]))

    (finding,) = _sidecar(tmp_path)["findings"]
    detail = finding["detail"]
    assert detail.startswith("RuntimeError: cannot reach <redacted-url>")
    assert "sk-secret" not in detail and "models.example" not in detail
    assert len(detail) <= len("RuntimeError: ") + 200


def test_a_run_that_does_not_crash_is_unchanged(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan()), _framed()])

    result = run_refund(tmp_path, transport, NO_REVIEW)

    assert result.status == "accepted"
    assert _sidecar(tmp_path)["findings"] == []
