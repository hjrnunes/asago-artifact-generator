"""Offline tests for the target-free two-call authoring seam."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring.core import (
    CALL1_PROMPT_VERSION_V21,
    PromptOverflowError,
    TransportResponse,
)
from asago_artifact_generator.authoring.policy import AuthoringBudget, AuthoringResult
from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
)

from .support import (
    ScriptedAuthoringTransport,
    load_failure_evidence,
    stage_local_orchestrator,
    world_builders,
)

_framed, _metadata, _inventory_v2, _plan_v2, _runtime_contract_v2 = world_builders(
    "refund", "framed", "metadata", "inventory", "plan", "runtime_contract"
)

(_view,) = world_builders("refund-minimal", "view")


def test_stage_correction_contains_complete_contract_and_all_findings(
    tmp_path: Path,
) -> None:
    malformed = {
        "interpretation": "wrong",
        "selected_evidence": ["wrong"],
        "assumptions": "wrong",
        "setup_recipe": "wrong",
        "runtime_bindings": {"wrong": True},
        "prerequisites": ["wrong"],
        "stimulus_approach": "wrong",
        "observation_claim": "wrong",
        "required_observations": "wrong",
        "semantic_judge": "wrong",
        "unresolved_requirements": "wrong",
    }
    response = json.dumps(malformed)
    transport = ScriptedAuthoringTransport([response, response])

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="complete-correction",
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "unresolved"
    assert len(result.ledger) == 2
    correction = transport.requests[1]
    original = transport.requests[0]
    payload = correction["payload"]
    assert payload["current_output"] == response
    assert payload["current_output_encoding"] == "utf-8-exact"
    assert payload["response_contract"] == original["payload"]["response_contract"]
    assert not {"original_request", "failed_response", "failed_response_encoding"} & set(payload)
    assert "failed_response_bytes_hex" not in payload
    assert "failed_response_bytes_base64" not in payload
    assert "failed_stage_contract" not in payload
    assert len(payload["findings"]) >= 11
    assert len(result.ledger[1]["findings"]) >= 11
    assert result.raw_responses["call1"] == response.encode()
    assert result.decoded_responses["call1"] == malformed


def test_two_calls_build_an_immutable_package_without_detector_code(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan_v2()), _framed()])
    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="refund-task",
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert isinstance(result, AuthoringResult)
    assert result.status == "accepted"
    assert result.package_path == tmp_path / "package"
    assert result.package is not None
    assert "detector.py" not in result.package.members
    assert "tool_call_condition.json" in result.package.members
    assert json.loads(result.package.members["explanation.json"])["text"]
    assert json.loads(result.package.members["examples.json"])["unsafe"]["label"] == (
        "author-proposed"
    )
    assert result.package.members["authoring/01-call1.raw"] == json.dumps(_plan_v2()).encode()
    assert [record["stage"] for record in result.ledger] == ["call1", "call2"]
    assert result.package.manifest.authoring["usage"][0]["availability"] == "unavailable"
    assert result.raw_responses["call1"] == json.dumps(_plan_v2()).encode()
    assert result.raw_responses["call2"] == _framed()
    assert result.prompts["call1"].version == CALL1_PROMPT_VERSION_V21
    assert transport.max_retries == 0


def test_package_write_failure_fails_with_the_write_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(destination: Path, package: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr("asago_artifact_generator.authoring.orchestrator.write_package", disk_full)
    transport = ScriptedAuthoringTransport([json.dumps(_plan_v2()), _framed()])

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="refund-task",
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "failed"
    assert result.package_path is None
    assert [(finding.code, finding.detail) for finding in result.findings] == [
        ("package_write_failed", "disk full")
    ]
    assert not (tmp_path / "package").exists()


def test_structural_unknown_reference_blocks_call2_without_prose_classification(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    plan["selected_evidence"] = [{"ref": "missing:record", "role": "record", "source": "facts"}]
    transport = ScriptedAuthoringTransport([json.dumps(plan), json.dumps(plan)])

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="bad-reference",
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "unresolved"
    assert result.package is None
    assert [request["stage"] for request in transport.requests] == ["call1", "correction"]
    assert any(finding.code == "unknown_reference" for finding in result.findings)


def test_essential_missing_information_is_retained_as_blocked_plan(tmp_path: Path) -> None:
    plan = _plan_v2()
    plan["unresolved_requirements"] = [
        {"name": "fresh_order", "essential": True, "reason": "not supplied"}
    ]
    transport = ScriptedAuthoringTransport([json.dumps(plan)])

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="blocked",
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "blocked"
    assert result.decoded_responses["call1"] == plan
    assert len(transport.requests) == 1
    assert not (tmp_path / "package").exists()


@pytest.mark.parametrize("failed_stage", ["call1", "call2"])
def test_stage_correction_contains_exact_failure_and_never_fourth_request(
    tmp_path: Path, failed_stage: str
) -> None:
    if failed_stage == "call1":
        invalid_plan = _plan_v2()
        invalid_plan["selected_evidence"] = [{"ref": "missing", "role": "x"}]
        responses: list[object] = [
            json.dumps(invalid_plan).encode(),
            json.dumps(_plan_v2()).encode(),
            _framed(),
        ]
    else:
        responses = [
            json.dumps(_plan_v2()).encode(),
            _framed(_metadata() | {"setup_recipe": []}),
            _framed(),
        ]
    transport = ScriptedAuthoringTransport(responses)

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / failed_stage,
        task_id=failed_stage,
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "accepted"
    assert len(transport.requests) == 3
    correction = transport.requests[1 if failed_stage == "call1" else 2]
    assert correction["stage"] == "correction"
    assert correction["payload"]["failed_stage"] == failed_stage
    failed_index = 0 if failed_stage == "call1" else 1
    assert correction["payload"]["current_output"] == responses[failed_index].decode()
    assert (
        correction["payload"]["response_contract"]
        == transport.requests[failed_index]["payload"]["response_contract"]
    )
    assert not {"original_request", "failed_response", "failed_response_encoding"} & set(
        correction["payload"]
    )
    assert correction["payload"]["findings"]


def test_failed_correction_stops_with_two_dispatches(tmp_path: Path) -> None:
    first = _plan_v2()
    first["selected_evidence"] = [{"ref": "missing", "role": "x"}]
    second = _plan_v2()
    second["selected_evidence"] = [{"ref": "still-missing", "role": "x", "source": "facts"}]
    transport = ScriptedAuthoringTransport([json.dumps(first), json.dumps(second)])

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="exhausted",
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "unresolved"
    assert len(transport.requests) == 2
    assert any("still-missing" in finding.detail for finding in result.findings)


def test_transport_failure_is_recorded_before_dispatch_and_contains_no_endpoint(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([RuntimeError("offline")])
    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="transport",
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "transport_failure"
    assert result.ledger[0]["dispatch_index"] == 1
    assert result.ledger[0]["error"] == "offline"
    assert "endpoint" not in json.dumps(result.ledger).lower()


@pytest.mark.parametrize(
    ("responses", "expected_stages"),
    [
        ([TimeoutError("Request timed out.")], ["call1"]),
        ([json.dumps(_plan_v2()), TimeoutError("Request timed out.")], ["call1", "call2"]),
    ],
    ids=["call1", "call2"],
)
def test_transport_failure_is_not_eligible_for_normal_correction(
    tmp_path: Path,
    responses: list[object],
    expected_stages: list[str],
) -> None:
    transport = ScriptedAuthoringTransport(responses)

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="transport-gate",
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "transport_failure"
    assert [request["stage"] for request in transport.requests] == expected_stages
    assert len(result.ledger) == len(expected_stages)
    assert result.ledger[-1]["stage"] != "correction"
    assert [finding.code for finding in result.findings] == ["transport_failure"]


@pytest.mark.parametrize(
    ("responses", "expected_stages"),
    [
        ([b"", json.dumps(_plan_v2()), _framed()], ["call1", "correction", "call2"]),
        (
            [json.dumps(_plan_v2()), b"```json\n{broken}\n```\n", _framed()],
            ["call1", "call2", "correction"],
        ),
    ],
    ids=["empty-call1-response", "malformed-call2-response"],
)
def test_response_bearing_failure_remains_eligible_for_correction(
    tmp_path: Path,
    responses: list[object],
    expected_stages: list[str],
) -> None:
    transport = ScriptedAuthoringTransport(responses)

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="response-bearing-correction",
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == expected_stages
    assert result.package is not None


def test_call2_rejects_fabricated_assistant_or_tool_history(tmp_path: Path) -> None:
    bad = _metadata()
    bad["stimulus"] = {
        "user_text": "Do it.",
        "delivery": "direct_user_message",
        "history": [{"role": "assistant", "content": "Sure"}],
        "slots": [],
    }
    transport = ScriptedAuthoringTransport([json.dumps(_plan_v2()), _framed(bad), _framed(bad)])
    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="history",
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "unresolved"
    assert any(finding.code == "non_user_history" for finding in result.findings)


def test_blocked_plan_is_retained_outside_the_immutable_package(tmp_path: Path) -> None:
    plan = _plan_v2()
    plan["unresolved_requirements"] = [
        {"name": "fresh_order", "essential": True, "reason": "not supplied"}
    ]
    result = stage_local_orchestrator(
        transport=ScriptedAuthoringTransport([json.dumps(plan)]),
        package_dir=tmp_path / "package",
        task_id="blocked-persisted",
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "blocked"
    retained = tmp_path / "package.blocked.json"
    assert retained.is_file()
    saved = json.loads(retained.read_text())
    assert saved["status"] == "blocked"
    assert saved["plan"] == plan
    assert not (tmp_path / "package" / "manifest.json").exists()


def test_setup_argument_schema_mismatch_is_structural_and_uses_stage_correction(
    tmp_path: Path,
) -> None:
    plan = _plan_v2()
    plan["setup_recipe"] = [
        {
            "operation": "process_refund",
            "arguments": {"order_id": "ord-1", "amount": "not-a-number"},
        }
    ]
    result = stage_local_orchestrator(
        transport=ScriptedAuthoringTransport([json.dumps(plan), json.dumps(plan)]),
        package_dir=tmp_path / "package",
        task_id="schema-mismatch",
    ).run(
        _view(),
        _inventory_v2(),
        {**_runtime_contract_v2(), "setup_permissions": ["process_refund"]},
    )

    assert result.status == "unresolved"
    assert len(result.ledger) == 2
    assert any(finding.code == "schema_type_mismatch" for finding in result.findings)


def test_budget_reserves_before_transport_failure_without_correction(
    tmp_path: Path,
) -> None:
    budget = AuthoringBudget(aggregate_limit=3, task_limit=3)
    transport = ScriptedAuthoringTransport(
        [RuntimeError("first"), RuntimeError("second"), RuntimeError("third")]
    )
    first = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "one",
        task_id="one",
        budget=budget,
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())
    second = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "two",
        task_id="two",
        budget=budget,
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert first.status == "transport_failure"
    assert second.status == "transport_failure"
    assert budget.total_dispatched == 2
    assert len(transport.requests) == 2
    assert all(request["stage"] == "call1" for request in transport.requests)
    assert any(finding.detail == "second" for finding in second.findings)


def test_response_capture_is_persisted_separately_from_final_answer(
    tmp_path: Path,
) -> None:
    capture = {
        "schema_version": "authoring-response-capture-v1",
        "final_answer": {"state": "empty", "content": ""},
        "reasoning": {"state": "text", "content": "must not be parsed"},
        "finish_reason": {"state": "value", "value": "length"},
    }
    result = stage_local_orchestrator(
        transport=ScriptedAuthoringTransport(
            [TransportResponse(raw=b"", response_capture=capture)]
        ),
        package_dir=tmp_path / "package",
        task_id="capture-separation",
        budget=AuthoringBudget(aggregate_limit=1, task_limit=1),
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "budget_exhausted"
    saved = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    attempt = saved["attempts"][0]
    assert attempt["response_capture"] == capture
    assert result.ledger[0]["response_capture"] == capture
    assert attempt["raw_response"]["byte_length"] == 0
    assert "reasoning" not in attempt.get("decoded_output", {})


def test_prompt_overflow_is_reported_without_silent_truncation() -> None:
    with pytest.raises(PromptOverflowError, match="explicitly scoped"):
        build_call1_packet_v2(
            _view(), _inventory_v2(), _runtime_contract_v2(), max_prompt_bytes=10
        )
