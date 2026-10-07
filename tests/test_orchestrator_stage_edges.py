from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import orchestrator
from asago_artifact_generator.authoring.core import (
    ArtifactValidationError,
    PromptOverflowError,
    PromptPreflightError,
    TransportResponse,
)
from asago_artifact_generator.authoring.policy import AuthoringPolicy, AuthoringResult

from .policy_support import ONE_PLAN_CORRECTION, PLAN_REVIEW, run_refund
from .support import (
    ScriptedAuthoringTransport,
    load_failure_evidence,
    review_response,
    world_builders,
)

_framed, _inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract", "view"
)


def _codes(result: AuthoringResult) -> list[str]:
    return [finding.code for finding in result.findings]


def test_undecodable_plan_bytes_record_a_parse_error_and_skip_plan_checks(
    tmp_path: Path,
) -> None:
    result = run_refund(tmp_path, ScriptedAuthoringTransport([b"\xff"]))

    assert result.status == "unresolved"
    assert _codes(result) == ["response_parse_error", "correction_limit_exhausted"]
    assert "invalid start byte" in result.ledger[0]["parse_error"]
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert "plan_validation" in json.dumps(evidence["attempts"][0])


def test_secret_bearing_plan_metadata_is_rejected_before_plan_checks(tmp_path: Path) -> None:
    plan = copy.deepcopy(_plan())
    plan["api_key"] = "redacted"

    result = run_refund(tmp_path, ScriptedAuthoringTransport([json.dumps(plan)]))

    assert result.status == "unresolved"
    assert _codes(result)[0] == "secret_in_response"
    assert "validation" not in result.ledger[0]


def test_plan_that_is_not_an_object_fails_framing(tmp_path: Path) -> None:
    result = run_refund(tmp_path, ScriptedAuthoringTransport([b"[1]"]))

    assert result.status == "unresolved"
    assert _codes(result)[0] == "json_object_required"
    assert result.ledger[0]["framing_findings"][0]["code"] == "json_object_required"


def test_blocked_plan_that_fails_its_checks_is_still_retained_as_blocked(
    tmp_path: Path,
) -> None:
    plan = copy.deepcopy(_plan())
    plan["unresolved_requirements"] = [{"name": "fresh_order", "essential": True, "reason": "r"}]
    plan["selected_evidence"] = [{"ref": "missing", "role": "x"}]

    result = run_refund(tmp_path, ScriptedAuthoringTransport([json.dumps(plan)]))

    assert result.status == "blocked"
    assert result.ledger[0]["findings"]
    blocked = json.loads((tmp_path / "package.blocked.json").read_text())
    assert blocked["plan"] == plan
    assert not (tmp_path / "package").exists()


def test_undecodable_correction_records_its_failure_and_the_parse_error(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([b"{}", b"\xff"])

    result = run_refund(tmp_path, transport, ONE_PLAN_CORRECTION)

    assert result.status == "unresolved"
    assert [request["stage"] for request in transport.requests] == ["call1", "correction"]
    assert result.findings[-1].code == "correction_failed"
    assert result.findings[-1].path == "call1"
    correction = result.ledger[1]
    assert "invalid start byte" in correction["parse_error"]
    assert correction["checks_not_run"] == ["plan_validation"]
    assert [item["code"] for item in correction["findings"]] == ["correction_failed"]
    attempt = load_failure_evidence(result.failure_evidence_path)["attempts"][1]
    assert attempt["failure"]["code"] == "correction_failed"
    assert attempt["checks_not_run"] == ["plan_validation"]


def test_secret_bearing_correction_fails_without_a_parse_error(tmp_path: Path) -> None:
    plan = copy.deepcopy(_plan())
    plan["api_key"] = "redacted"
    transport = ScriptedAuthoringTransport([b"{}", json.dumps(plan)])

    result = run_refund(tmp_path, transport, ONE_PLAN_CORRECTION)

    assert result.status == "unresolved"
    assert result.findings[-1].code == "correction_failed"
    assert "secret-bearing" in result.findings[-1].detail
    correction = result.ledger[1]
    assert [item["code"] for item in correction["findings"]] == ["correction_failed"]
    assert "parse_error" not in correction
    assert "checks_not_run" not in correction


def test_oversized_correction_prompt_stops_before_the_correction_dispatch(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([json.dumps({"junk": "x" * 1_100_000})])

    result = run_refund(tmp_path, transport, ONE_PLAN_CORRECTION)

    assert result.status == "prompt_overflow"
    assert _codes(result) == ["prompt_overflow"]
    assert result.findings[0].path == "correction"
    assert [request["stage"] for request in transport.requests] == ["call1"]
    assert len(result.ledger) == 1
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["status"] == "prompt_overflow"
    assert len(evidence["attempts"]) == 1


def test_correction_overflow_evidence_names_the_overflow_as_its_terminal_finding(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([json.dumps({"junk": "x" * 1_100_000})])

    result = run_refund(tmp_path, transport, ONE_PLAN_CORRECTION)

    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["status"] == "prompt_overflow"
    assert [finding["code"] for finding in evidence["findings"]] == ["prompt_overflow"]
    assert evidence["findings"][0]["path"] == "correction"
    assert evidence["terminal"] == {
        "stage": "plan",
        "attempt_index": 0,
        "reason": "prompt_overflow",
    }
    failed_attempt_codes = {finding["code"] for finding in evidence["attempts"][0]["findings"]}
    assert "plan_validation" in failed_attempt_codes


def test_artifact_correction_overflow_evidence_names_the_overflow_as_its_terminal_finding(
    tmp_path: Path,
) -> None:
    policy = AuthoringPolicy(
        plan_max_corrections=0,
        artifact_max_corrections=1,
        review_plan=False,
        review_artifact=False,
    )
    transport = ScriptedAuthoringTransport(
        [json.dumps(_plan()), _framed({"junk": "x" * 1_100_000})]
    )

    result = run_refund(tmp_path, transport, policy)

    assert result.status == "prompt_overflow"
    assert [request["stage"] for request in transport.requests] == ["call1", "call2"]
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert [finding["code"] for finding in evidence["findings"]] == ["prompt_overflow"]
    assert evidence["terminal"] == {
        "stage": "artifact",
        "attempt_index": 1,
        "reason": "prompt_overflow",
    }


def _plan_needing_a_prerequisite_consumer() -> dict:
    plan = copy.deepcopy(_plan())
    plan["runtime_bindings"][0]["consumers"] = ["setup.arguments.owned_order"]
    return plan


def _consumer_rewrite() -> dict:
    return {
        "transformation": "binding_consumer_added",
        "binding": "owned_order",
        "original_consumers": ["setup.arguments.owned_order"],
        "canonical_consumers": ["setup.arguments.owned_order", "prerequisites.owned_order"],
        "prerequisite_index": 0,
        "prerequisite_name": "owned_order",
        "consumer": "prerequisites.owned_order",
    }


def test_plan_validation_rewrites_are_recorded_on_the_dispatch(tmp_path: Path) -> None:
    plan = _plan_needing_a_prerequisite_consumer()

    result = run_refund(tmp_path, ScriptedAuthoringTransport([json.dumps(plan), _framed()]))

    assert result.status == "accepted"
    assert result.transformations == [_consumer_rewrite()]
    assert result.ledger[0]["transformations"] == [_consumer_rewrite()]
    assert "transformations" not in result.ledger[1]
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    assert evidence["transformations"] == [_consumer_rewrite()]
    assert evidence["attempts"][0]["transformations"] == [_consumer_rewrite()]


def test_correction_validation_rewrites_are_recorded_on_the_correction(tmp_path: Path) -> None:
    plan = _plan_needing_a_prerequisite_consumer()
    transport = ScriptedAuthoringTransport([b"{}", json.dumps(plan), _framed()])

    result = run_refund(tmp_path, transport, ONE_PLAN_CORRECTION)

    assert result.status == "accepted"
    assert [record["stage"] for record in result.ledger] == ["call1", "correction", "call2"]
    assert "transformations" not in result.ledger[0]
    assert result.ledger[1]["transformations"] == [_consumer_rewrite()]
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    assert evidence["attempts"][1]["transformations"] == [_consumer_rewrite()]
    assert evidence["transformations"] == [_consumer_rewrite()]


def test_package_assembly_rejection_fails_the_run_at_the_artifact_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject(**_: object) -> None:
        raise ArtifactValidationError("judge specification must be an object", "judge.json")

    monkeypatch.setattr(orchestrator, "_package_from_responses", reject)

    result = run_refund(tmp_path, ScriptedAuthoringTransport([json.dumps(_plan()), _framed()]))

    assert result.status == "failed"
    assert [(f.code, f.path, f.stage) for f in result.findings] == [
        ("assembly_validation", "judge.json", "artifact")
    ]
    assert result.findings[0].detail == "judge specification must be an object"
    assert result.package is None
    assert not (tmp_path / "package").exists()


def test_transport_failure_keeps_the_controls_the_transport_reports(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([RuntimeError("provider down")])
    transport.last_controls = {"max_retries": 0, "temperature": 0}

    result = run_refund(tmp_path, transport)

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

    result = run_refund(tmp_path, transport, PLAN_REVIEW)

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

    result = run_refund(tmp_path, transport, PLAN_REVIEW)

    assert result.status == "prompt_overflow"
    assert result.review_status["plan"] == "prompt_overflow"
    assert len(transport.requests) == 1


def test_call1_context_preflight_failure_ends_the_run_failed_before_any_dispatch(
    tmp_path: Path,
) -> None:
    class _Call1PreflightTransport(ScriptedAuthoringTransport):
        def preflight_context_budget(self, packet):
            raise PromptPreflightError("scripted preflight failure")

    transport = _Call1PreflightTransport([json.dumps(_plan())])

    result = run_refund(tmp_path, transport, ONE_PLAN_CORRECTION)

    assert result.status == "failed"
    assert [(f.code, f.path, f.stage) for f in result.findings] == [
        ("prompt_preflight", "call1", "plan")
    ]
    assert transport.requests == []
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["status"] == "failed"
    assert evidence["attempts"] == []
    assert evidence["terminal"]["reason"] == "prompt_preflight"
    assert [finding["code"] for finding in evidence["findings"]] == ["prompt_preflight"]


def test_review_context_preflight_failure_keeps_the_review_unavailable_stop(
    tmp_path: Path,
) -> None:
    class _ReviewPreflightTransport(ScriptedAuthoringTransport):
        def preflight_context_budget(self, packet):
            if packet.stage == "plan_review":
                raise PromptPreflightError("scripted preflight failure")

    result = run_refund(tmp_path, _ReviewPreflightTransport([json.dumps(_plan())]), PLAN_REVIEW)

    assert result.status == "review_unavailable"
    assert _codes(result) == ["transport_failure"]


def test_review_response_capture_is_kept_on_the_ledger_and_attempt(tmp_path: Path) -> None:
    capture = {"finish_reason": "stop"}
    transport = ScriptedAuthoringTransport(
        [
            json.dumps(_plan()),
            TransportResponse(raw=review_response(), response_capture=capture),
            _framed(),
        ]
    )

    result = run_refund(tmp_path, transport, PLAN_REVIEW)

    assert result.status == "accepted"
    assert result.ledger[1]["stage"] == "plan_review"
    assert result.ledger[1]["response_capture"] == capture


def test_fenced_review_response_records_its_transformation(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport(
        [json.dumps(_plan()), b"```json\n" + review_response() + b"\n```", _framed()]
    )

    result = run_refund(tmp_path, transport, PLAN_REVIEW)

    assert result.status == "accepted"
    assert result.ledger[1]["transformation"] == "outer_fence_removed"
    assert "outer_fence_removed" in result.transformations
