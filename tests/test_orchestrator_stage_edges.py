from __future__ import annotations

import copy
import json
from pathlib import Path

from asago_artifact_generator.authoring.core import PromptOverflowError, TransportResponse
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import AuthoringPolicy, AuthoringResult
from asago_artifact_generator.failure_evidence import load_failure_evidence

from .support import ScriptedAuthoringTransport
from .test_versioned_authoring_wire import (
    _framed,
    _inventory,
    _plan,
    _runtime_contract,
    _view,
)

_NO_REVIEW = AuthoringPolicy(
    plan_max_corrections=0,
    artifact_max_corrections=0,
    review_plan=False,
    review_artifact=False,
)
_PLAN_REVIEW = AuthoringPolicy(
    plan_max_corrections=0,
    artifact_max_corrections=0,
    review_plan=True,
    review_artifact=False,
)


def _review(decision: str = "accept") -> bytes:
    return json.dumps(
        {"decision": decision, "summary": f"scripted {decision}", "findings": []}
    ).encode()


def _run(
    tmp_path: Path,
    transport: ScriptedAuthoringTransport,
    policy: AuthoringPolicy = _NO_REVIEW,
) -> AuthoringResult:
    return AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="stage-edges",
        policy=policy,
    ).run(_view(), _inventory(), _runtime_contract())


def _codes(result: AuthoringResult) -> list[str]:
    return [finding.code for finding in result.findings]


def test_undecodable_plan_bytes_record_a_parse_error_and_skip_plan_checks(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, ScriptedAuthoringTransport([b"\xff"]))

    assert result.status == "unresolved"
    assert _codes(result) == ["response_parse_error", "correction_limit_exhausted"]
    assert "invalid start byte" in result.ledger[0]["parse_error"]
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert "plan_validation" in json.dumps(evidence["attempts"][0])


def test_secret_bearing_plan_metadata_is_rejected_before_plan_checks(tmp_path: Path) -> None:
    plan = copy.deepcopy(_plan())
    plan["api_key"] = "redacted"

    result = _run(tmp_path, ScriptedAuthoringTransport([json.dumps(plan)]))

    assert result.status == "unresolved"
    assert _codes(result)[0] == "secret_in_response"
    assert "validation" not in result.ledger[0]


def test_plan_that_is_not_an_object_fails_framing(tmp_path: Path) -> None:
    result = _run(tmp_path, ScriptedAuthoringTransport([b"[1]"]))

    assert result.status == "unresolved"
    assert _codes(result)[0] == "json_object_required"
    assert result.ledger[0]["framing_findings"][0]["code"] == "json_object_required"


def test_blocked_plan_that_fails_its_checks_is_still_retained_as_blocked(
    tmp_path: Path,
) -> None:
    plan = copy.deepcopy(_plan())
    plan["unresolved_requirements"] = [{"name": "fresh_order", "essential": True, "reason": "r"}]
    plan["selected_evidence"] = [{"ref": "missing", "role": "x"}]

    result = _run(tmp_path, ScriptedAuthoringTransport([json.dumps(plan)]))

    assert result.status == "blocked"
    assert result.ledger[0]["findings"]
    blocked = json.loads((tmp_path / "package.blocked.json").read_text())
    assert blocked["plan"] == plan
    assert not (tmp_path / "package").exists()


def test_transport_failure_keeps_the_controls_the_transport_reports(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([RuntimeError("provider down")])
    transport.last_controls = {"max_retries": 0, "temperature": 0}

    result = _run(tmp_path, transport)

    assert result.status == "transport_failure"
    assert result.ledger[0]["controls"] == {"max_retries": 0, "temperature": 0}
    evidence = load_failure_evidence(result.failure_evidence_path)
    controls = evidence["attempts"][0]["controls"]
    assert controls["value"] == {"max_retries": 0, "temperature": 0}


def test_oversized_plan_review_prompt_stops_before_the_review_dispatch(
    tmp_path: Path,
) -> None:
    plan = copy.deepcopy(_plan())
    plan["interpretation"]["failure"] = "The command exceeds the balance. " * 36_000
    transport = ScriptedAuthoringTransport([json.dumps(plan)])

    result = _run(tmp_path, transport, _PLAN_REVIEW)

    assert result.status == "prompt_overflow"
    assert _codes(result) == ["prompt_overflow"]
    assert result.findings[0].path == "plan_review"
    assert len(transport.requests) == 1


def test_review_context_overflow_records_prompt_overflow_status(tmp_path: Path) -> None:
    class _ReviewOverflowTransport(ScriptedAuthoringTransport):
        def preflight_context_budget(self, packet):
            if packet.stage == "plan_review":
                raise PromptOverflowError("plan_review exceeds the context window")

    transport = _ReviewOverflowTransport([json.dumps(_plan())])

    result = _run(tmp_path, transport, _PLAN_REVIEW)

    assert result.status == "prompt_overflow"
    assert result.review_status["plan"] == "prompt_overflow"
    assert len(transport.requests) == 1


def test_review_response_capture_is_kept_on_the_ledger_and_attempt(tmp_path: Path) -> None:
    capture = {"finish_reason": "stop"}
    transport = ScriptedAuthoringTransport(
        [
            json.dumps(_plan()),
            TransportResponse(raw=_review(), response_capture=capture),
            _framed(),
        ]
    )

    result = _run(tmp_path, transport, _PLAN_REVIEW)

    assert result.status == "accepted"
    assert result.ledger[1]["stage"] == "plan_review"
    assert result.ledger[1]["response_capture"] == capture


def test_fenced_review_response_records_its_transformation(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport(
        [json.dumps(_plan()), b"```json\n" + _review() + b"\n```", _framed()]
    )

    result = _run(tmp_path, transport, _PLAN_REVIEW)

    assert result.status == "accepted"
    assert result.ledger[1]["transformation"] == "outer_fence_removed"
    assert "outer_fence_removed" in result.transformations
