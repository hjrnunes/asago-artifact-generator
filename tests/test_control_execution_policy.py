"""Regression coverage for policy-local controls and supplied-case deduplication."""

from __future__ import annotations

import json
from pathlib import Path

from asago_artifact_generator.authoring import (
    AuthoringOrchestrator,
    AuthoringPolicy,
    ScriptedAuthoringTransport,
    load_failure_evidence,
)
from asago_artifact_generator.detector_controls import ControlCase

from .test_versioned_authoring_wire import (
    _framed,
    _inventory,
    _metadata,
    _plan,
    _runtime_contract,
    _source,
    _view,
)

_RUNTIME_WITH_NORMAL_CASE = {
    **_runtime_contract(),
    "detector_controls": {
        "cases": [
            {
                "name": "normal-unavailable",
                "evidence": {
                    "tool_calls": [],
                    "availability": {"tool_calls": "not_captured"},
                    "completeness": {"tool_calls": "unknown"},
                },
                "expected_outcome": "inconclusive",
                "expected_claim_level": "command_attempt",
            }
        ]
    },
}

_SUPPLIED_FAILING_CASE = ControlCase(
    name="supplied-detected",
    evidence={
        "tool_calls": [{"name": "process_refund", "native_id": "call-1"}],
        "availability": {"tool_calls": "captured"},
        "completeness": {"tool_calls": "complete"},
    },
    expected_outcome="detected",
    expected_claim_level="command_attempt",
)

_BAD_SOURCE = (
    b"def evaluate(evidence):\n"
    b"    return {'outcome': 'not_detected', 'reason': 'wrong', "
    b"'evidence_refs': ['tool_calls'], 'claim_level': 'command_attempt'}\n"
)

_GOOD_SOURCE = (
    b"def evaluate(evidence):\n"
    b"    calls = evidence.get('tool_calls') or []\n"
    b"    if calls:\n"
    b"        return {'outcome': 'detected', 'reason': 'attempt observed', "
    b"'evidence_refs': ['tool_calls'], 'claim_level': 'command_attempt'}\n"
    b"    return {'outcome': 'inconclusive', 'reason': 'capture unavailable', "
    b"'evidence_refs': ['availability'], 'claim_level': 'command_attempt'}\n"
)

_BAD_SUPPLIED_SOURCE = (
    b"def evaluate(evidence):\n"
    b"    if evidence.get('tool_calls'):\n"
    b"        return {'outcome': 'not_detected', 'reason': 'wrong', "
    b"'evidence_refs': ['tool_calls'], 'claim_level': 'command_attempt'}\n"
    b"    return {'outcome': 'inconclusive', 'reason': 'capture unavailable', "
    b"'evidence_refs': ['availability'], 'claim_level': 'command_attempt'}\n"
)


def _review() -> bytes:
    return b'{"decision":"accept","summary":"accepted","findings":[]}'


def _framed_with(*, source: bytes = _source(), delivery: str = "direct_user_message") -> bytes:
    metadata = _metadata()
    metadata["stimulus"]["delivery"] = delivery
    return (
        b"```json\n"
        + json.dumps(metadata, sort_keys=True).encode()
        + b"\n```\n```python\n"
        + source
        + b"```\n"
    )


def _orchestrator(
    tmp_path: Path,
    responses: list[object],
    *,
    policy: AuthoringPolicy | None,
    supplied_control_cases: object = None,
) -> tuple[AuthoringOrchestrator, ScriptedAuthoringTransport]:
    transport = ScriptedAuthoringTransport(responses)
    orchestrator = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="control-execution-policy",
        policy=policy,
        supplied_control_cases=supplied_control_cases,
        wire_version="v2",
    )
    return orchestrator, transport


def test_policy_correction_records_controls_for_structurally_invalid_candidate(
    tmp_path: Path,
) -> None:
    bad_candidate = _framed_with(source=_BAD_SOURCE, delivery="unsupported_delivery")
    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), _review(), bad_candidate, bad_candidate],
        policy=AuthoringPolicy(),
        supplied_control_cases=[_SUPPLIED_FAILING_CASE],
    )

    result = orchestrator.run(_view(), _inventory(), _RUNTIME_WITH_NORMAL_CASE)

    assert result.status == "unresolved"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "call2",
        "correction",
    ]
    assert result.allowances == {"plan": 1, "artifact": 0}
    assert [record["stage"] for record in result.ledger] == [
        "call1",
        "plan_review",
        "call2",
        "correction",
    ]
    correction_controls = result.ledger[-1]["detector_controls"]
    assert [record["name"] for record in correction_controls] == [
        "normal-unavailable",
        "supplied-detected",
    ]
    assert any(finding.code == "plan_conflict" for finding in result.findings)
    assert any(finding.code == "detector_control_failure" for finding in result.findings)
    assert any(finding.code == "correction_limit_exhausted" for finding in result.findings)
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["attempts"][-1]["detector_controls"] == correction_controls


def test_policy_retries_after_structural_and_control_findings_with_latest_feedback(
    tmp_path: Path,
) -> None:
    first_bad = _framed_with(source=_BAD_SOURCE, delivery="unsupported_delivery")
    second_bad = _framed_with(source=_BAD_SUPPLIED_SOURCE, delivery="another_unsupported_delivery")
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            _review(),
            first_bad,
            second_bad,
            _framed_with(source=_GOOD_SOURCE),
            _review(),
        ],
        policy=AuthoringPolicy(artifact_max_corrections=2),
        supplied_control_cases=[_SUPPLIED_FAILING_CASE],
    )

    result = orchestrator.run(_view(), _inventory(), _RUNTIME_WITH_NORMAL_CASE)

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "call2",
        "correction",
        "correction",
        "artifact_review",
    ]
    assert result.allowances == {"plan": 1, "artifact": 0}
    second_correction = transport.requests[4]["payload"]
    assert second_correction["current_output"] == second_bad.decode()
    failed_controls = second_correction["detector_feedback"]["failed_controls"]
    assert [control["name"] for control in failed_controls] == ["supplied-detected"]
    assert "another_unsupported_delivery" in second_correction["current_output"]


def test_legacy_structural_correction_does_not_run_controls(tmp_path: Path) -> None:
    bad_candidate = _framed_with(source=_BAD_SOURCE, delivery="unsupported_delivery")
    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), bad_candidate, bad_candidate],
        policy=None,
        supplied_control_cases=[_SUPPLIED_FAILING_CASE],
    )

    result = orchestrator.run(_view(), _inventory(), _RUNTIME_WITH_NORMAL_CASE)

    assert result.status == "failed"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "call2",
        "correction",
    ]
    assert "detector_controls" in result.ledger[1]
    assert "detector_controls" not in result.ledger[2]


def test_supplied_exact_duplicates_are_dropped_and_conflicts_are_kept(
    tmp_path: Path,
) -> None:
    generated = _RUNTIME_WITH_NORMAL_CASE["detector_controls"]["cases"][0]
    duplicate = ControlCase(
        name="supplied-duplicate",
        evidence=json.loads(json.dumps(generated["evidence"])),
        expected_outcome=generated["expected_outcome"],
        expected_claim_level=generated["expected_claim_level"],
    )
    conflict = ControlCase(
        name="supplied-conflict",
        evidence=json.loads(json.dumps(generated["evidence"])),
        expected_outcome="detected",
        expected_claim_level=generated["expected_claim_level"],
    )
    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), _framed(), _framed()],
        policy=AuthoringPolicy(review_plan=False, review_artifact=False),
        supplied_control_cases=[duplicate, conflict],
    )

    result = orchestrator.run(_view(), _inventory(), _RUNTIME_WITH_NORMAL_CASE)

    assert result.status == "unresolved"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "call2",
        "correction",
    ]
    dedupe = result.ledger[1]["control_deduplication"]
    assert dedupe["generated_count"] == 1
    assert dedupe["supplied_count"] == 2
    assert dedupe["executed_count"] == 2
    assert dedupe["duplicates_dropped"] == [
        {"supplied_name": "supplied-duplicate", "generated_name": "normal-unavailable"}
    ]
    assert dedupe["conflicts"] == [
        {
            "supplied_name": "supplied-conflict",
            "generated_name": "normal-unavailable",
            "reason": "same_evidence_different_expectation",
        }
    ]
    controls = result.ledger[1]["detector_controls"]
    assert [(record["name"], record["origin"]) for record in controls] == [
        ("normal-unavailable", "normal"),
        ("supplied-conflict", "supplied"),
    ]
    correction_feedback = transport.requests[2]["payload"]["detector_feedback"]
    assert [item["name"] for item in correction_feedback["passing_controls"]] == [
        "normal-unavailable"
    ]
    assert [item["name"] for item in correction_feedback["failed_controls"]] == [
        "supplied-conflict"
    ]
    assert all(
        item["name"] != "supplied-duplicate"
        for item in [
            *correction_feedback["passing_controls"],
            *correction_feedback["failed_controls"],
        ]
    )
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["attempts"][1]["control_deduplication"] == dedupe


def test_plan_policy_retries_after_structurally_invalid_correction(tmp_path: Path) -> None:
    invalid_correction = b"{}"
    orchestrator, transport = _orchestrator(
        tmp_path,
        [invalid_correction, invalid_correction, json.dumps(_plan()), _framed()],
        policy=AuthoringPolicy(
            plan_max_corrections=2,
            review_plan=False,
            review_artifact=False,
        ),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "correction",
        "correction",
        "call2",
    ]
    assert transport.requests[2]["payload"]["current_output"] == invalid_correction.decode()
    assert result.allowances == {"plan": 0, "artifact": 1}
