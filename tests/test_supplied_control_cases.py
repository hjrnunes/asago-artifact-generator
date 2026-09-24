"""Supplied detector-control hook on the authoring orchestrator.

The hook adds caller-supplied `ControlCase` objects (static or resolved from
the current candidate plan and metadata) to the mechanically derived controls.
One combined `run_detector_controls` invocation runs both origins, results
carry `origin: normal|supplied`, and supplied failures reuse the existing
single artifact correction. Without the hook, rendered prompt bytes remain
deterministic and match the pinned current contracts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import (
    AuthoringOrchestrator,
    AuthoringPolicy,
    ScriptedAuthoringTransport,
    build_artifact_review_packet,
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

# Normal mechanically derived case that the neutral `_source()` detector
# passes: it always reports inconclusive at the command_attempt claim level.
_NORMAL_PASSING_CASE = {
    "name": "normal-unavailable",
    "evidence": {
        "tool_calls": [],
        "availability": {"tool_calls": "not_captured"},
        "completeness": {"tool_calls": "unknown"},
    },
    "expected_outcome": "inconclusive",
    "expected_claim_level": "command_attempt",
}

# Supplied case the neutral `_source()` detector fails: a captured
# process_refund call must be reported as detected.
_SUPPLIED_FAILING_CASE = ControlCase(
    name="supplied-detected",
    evidence={
        "tool_calls": [{"name": "process_refund", "native_id": "call-1"}],
        "availability": {"tool_calls": "captured"},
        "completeness": {"tool_calls": "complete"},
        "dynamic_record": "ORD-77",
    },
    expected_outcome="detected",
    expected_claim_level="command_attempt",
)

# Corrected detector distinguishes captured calls from missing captures.
_FIXED_SOURCE = (
    b"def evaluate(evidence: dict) -> dict:\n"
    b"    calls = evidence.get('tool_calls') or []\n"
    b"    if calls:\n"
    b"        return {'outcome': 'detected', 'reason': 'attempt observed', "
    b"'evidence_refs': ['tool_calls'], 'claim_level': 'command_attempt'}\n"
    b"    return {'outcome': 'inconclusive', 'reason': 'capture unavailable', "
    b"'evidence_refs': ['availability'], 'claim_level': 'command_attempt'}\n"
)


def _review(decision: str = "accept") -> bytes:
    return json.dumps(
        {
            "decision": decision,
            "summary": f"scripted {decision} routing example",
            "findings": [],
        }
    ).encode()


def _orchestrator(
    tmp_path: Path,
    responses: list[object],
    *,
    supplied_control_cases: object = None,
) -> tuple[AuthoringOrchestrator, ScriptedAuthoringTransport]:
    transport = ScriptedAuthoringTransport(responses)
    orchestrator = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="supplied-controls",
        wire_version="v2",
        policy=AuthoringPolicy(),
        supplied_control_cases=supplied_control_cases,
    )
    return orchestrator, transport


def _control_runtime() -> dict:
    return {
        **_runtime_contract(),
        "detector_controls": {"cases": [_NORMAL_PASSING_CASE]},
    }


def test_supplied_cases_run_with_normal_cases_and_record_origin(tmp_path: Path) -> None:
    supplied = ControlCase(
        name="supplied-unavailable",
        evidence={
            "tool_calls": [],
            "availability": {"tool_calls": "not_captured"},
            "completeness": {"tool_calls": "unknown"},
            "dynamic_record": "ORD-77",
        },
        expected_outcome="inconclusive",
        expected_claim_level="command_attempt",
    )
    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), _review(), _framed(), _review()],
        supplied_control_cases=[supplied],
    )

    result = orchestrator.run(_view(), _inventory(), _control_runtime())

    assert result.status == "accepted"
    # One combined control execution: normal cases first, supplied second.
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "call2",
        "artifact_review",
    ]
    controls = result.ledger[2]["detector_controls"]
    assert [(record["name"], record["origin"], record["status"]) for record in controls] == [
        ("normal-unavailable", "normal", "passed"),
        ("supplied-unavailable", "supplied", "passed"),
    ]


def test_control_case_provider_receives_candidate_plan_and_metadata(tmp_path: Path) -> None:
    seen: list[tuple[dict, dict]] = []

    def provider(plan: dict, metadata: dict) -> list[ControlCase]:
        seen.append((plan, metadata))
        # Mechanically remap a candidate-local binding name into the case.
        binding = plan["runtime_bindings"][0]["name"]
        return [
            ControlCase(
                name=f"supplied-{binding}",
                evidence={
                    "tool_calls": [],
                    "availability": {"tool_calls": "not_captured"},
                    "completeness": {"tool_calls": "unknown"},
                    "provider_probe": True,
                },
                expected_outcome="inconclusive",
                expected_claim_level="command_attempt",
            )
        ]

    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), _review(), _framed(), _review()],
        supplied_control_cases=provider,
    )

    result = orchestrator.run(_view(), _inventory(), _control_runtime())

    assert result.status == "accepted"
    assert len(seen) == 1
    assert seen[0][0] == result.plan
    assert seen[0][1] == _metadata()
    controls = result.ledger[2]["detector_controls"]
    assert [(record["name"], record["origin"]) for record in controls] == [
        ("normal-unavailable", "normal"),
        ("supplied-owned_order", "supplied"),
    ]
    review_user = transport.requests[3]["user"]
    assert "supplied-owned_order" in review_user


def test_supplied_failure_uses_one_artifact_correction_and_stops(tmp_path: Path) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            _review(),
            _framed(),  # initial candidate fails the supplied control
            _framed(),  # corrected candidate fails it again
        ],
        supplied_control_cases=[_SUPPLIED_FAILING_CASE],
    )

    result = orchestrator.run(_view(), _inventory(), _control_runtime())

    assert result.status == "unresolved"
    # Exactly one artifact correction; the still-failing corrected candidate
    # terminates without a second correction or any new allowance.
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "call2",
        "correction",
    ]
    assert result.allowances == {"plan": 1, "artifact": 0}
    finding = next(
        finding for finding in result.findings if finding.code == "detector_control_failure"
    )
    assert finding.path == "detector_controls.supplied-detected"
    # The correction packet carries the actual failing supplied control packet.
    correction_user = transport.requests[3]["user"]
    assert "supplied-detected" in correction_user
    assert "ORD-77" in correction_user
    controls = result.ledger[2]["detector_controls"]
    assert [(record["name"], record["origin"], record["status"]) for record in controls] == [
        ("normal-unavailable", "normal", "passed"),
        ("supplied-detected", "supplied", "failed"),
    ]
    assert not (tmp_path / "package").exists()


def test_duplicate_supplied_control_names_keep_failing_packet_in_correction(
    tmp_path: Path,
) -> None:
    passing_case = ControlCase(
        name="duplicate-probe",
        evidence={
            "tool_calls": [],
            "availability": {"tool_calls": "not_captured"},
            "completeness": {"tool_calls": "unknown"},
            "dynamic_record": "ORD-PASS",
        },
        expected_outcome="inconclusive",
        expected_claim_level="command_attempt",
    )
    failing_case = ControlCase(
        name="duplicate-probe",
        evidence={
            **_SUPPLIED_FAILING_CASE.evidence,
            "dynamic_record": "ORD-FAIL",
        },
        expected_outcome="detected",
        expected_claim_level="command_attempt",
    )
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            _review(),
            _framed(),
            _framed(),
        ],
        supplied_control_cases=[failing_case, passing_case],
    )

    result = orchestrator.run(_view(), _inventory(), _control_runtime())

    assert result.status == "unresolved"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "call2",
        "correction",
    ]
    correction_user = transport.requests[3]["user"]
    assert "ORD-FAIL" in correction_user
    controls = result.ledger[2]["detector_controls"]
    assert [(record["name"], record["origin"], record["status"]) for record in controls] == [
        ("normal-unavailable", "normal", "passed"),
        ("duplicate-probe", "supplied", "failed"),
        ("duplicate-probe", "supplied", "passed"),
    ]


def test_corrected_candidate_review_receives_actual_combined_controls(tmp_path: Path) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            _review(),
            _framed(),  # initial candidate fails the supplied control
            _framed(source=_FIXED_SOURCE),  # correction passes both origins
            _review(),
        ],
        supplied_control_cases=[_SUPPLIED_FAILING_CASE],
    )

    result = orchestrator.run(_view(), _inventory(), _control_runtime())

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "call2",
        "correction",
        "artifact_review",
    ]
    assert result.allowances == {"plan": 1, "artifact": 0}
    review_user = transport.requests[4]["user"]
    assert "normal-unavailable" in review_user
    assert "supplied-detected" in review_user
    assert '"origin": "normal"' in review_user
    assert '"origin": "supplied"' in review_user


def test_supplied_control_case_form_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="supplied_control_cases"):
        _orchestrator(tmp_path / "string", [], supplied_control_cases="cases")
    with pytest.raises(ValueError, match="supplied_control_cases"):
        _orchestrator(tmp_path / "dict", [], supplied_control_cases=[{"name": "x"}])

    def broken_provider(plan: dict, metadata: dict) -> object:
        return "not-a-case-sequence"

    orchestrator, _transport = _orchestrator(
        tmp_path / "provider",
        [json.dumps(_plan()), _review(), _framed()],
        supplied_control_cases=broken_provider,
    )
    with pytest.raises(ValueError, match="ControlCase"):
        orchestrator.run(_view(), _inventory(), _control_runtime())


# Digests of (system + "\x00" + user) rendered for the current prompt contracts
# without the supplied-controls hook.
_EXPECTED_NO_HOOK_DIGESTS = {
    "call1": "36b28c73a3bdc1e546e13b9feec69a25c09045717f49bd2db0cd3f18a98eaa54",
    "plan_review": "4871f3829dc88cc7251e043f0a15cdafd2988a5cf4b57ac453f26a9245141490",
    "call2": "1ebf934bc202a5aacb48d5484a458d7d4994eac8ddcb2ca3fff053b8ca5c2c38",
    "artifact_review": "de5796a242ac7735f43506977de5ef53d87050f7982f22db6c1934aeec21ad80",
    "correction": "872bc37f642744673f56c4d7b1c2a2e0d491d581e59405bcc548fda1076c2d78",
    "artifact_review_direct": ("33e5443440409e1cc29c902fbb82fc16f93f932fa135f14b81dd042b9e7dd496"),
}


def _request_digest(request: dict) -> str:
    return hashlib.sha256(
        (request["system"] + "\x00" + request["user"]).encode("utf-8")
    ).hexdigest()


def test_no_hook_renders_base_revision_prompt_bytes(tmp_path: Path) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), _review(), _framed(), _review()],
    )
    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    digests = {request["stage"]: _request_digest(request) for request in transport.requests}
    for stage in ("call1", "plan_review", "call2", "artifact_review"):
        assert digests[stage] == _EXPECTED_NO_HOOK_DIGESTS[stage]

    failing_runtime = {
        **_runtime_contract(),
        "detector_controls": {
            "cases": [
                {
                    "name": "positive-command",
                    "evidence": {
                        "tool_calls": [],
                        "availability": {"tool_calls": "captured"},
                        "completeness": {"tool_calls": "complete"},
                    },
                    "expected_outcome": "detected",
                    "expected_claim_level": "command_attempt",
                }
            ]
        },
    }
    correcting, correcting_transport = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), _review(), _framed(), _framed()],
    )
    correcting_result = correcting.run(_view(), _inventory(), failing_runtime)

    assert correcting_result.status == "unresolved"
    assert (
        _request_digest(correcting_transport.requests[3])
        == (_EXPECTED_NO_HOOK_DIGESTS["correction"])
    )

    controls = [
        {
            "name": "sample-normal-case",
            "expected_outcome": "inconclusive",
            "expected_claim_level": "command_attempt",
            "status": "passed",
            "observed_outcome": "inconclusive",
            "observed_claim_level": "command_attempt",
            "failure": None,
            "runtime": {
                "engine": "docker",
                "docker_path": "/usr/local/bin/docker",
                "image": "python:3.12-slim",
                "network": "none",
                "read_only": True,
            },
            "actual_result": {
                "outcome": "inconclusive",
                "reason": "capture unavailable",
                "evidence_refs": [],
                "claim_level": "command_attempt",
            },
        }
    ]
    packet = build_artifact_review_packet(
        _view(),
        _plan(),
        _metadata(),
        _source(),
        controls,
        _inventory(),
        _runtime_contract(),
    )
    direct = {
        "system": packet.system,
        "user": packet.user,
    }
    assert _request_digest(direct) == _EXPECTED_NO_HOOK_DIGESTS["artifact_review_direct"]
