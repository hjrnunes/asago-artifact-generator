from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import openai
import pytest
from scripts.fresh_trial.receipts import _case_receipt
from scripts.fresh_trial.run_fresh_authoring_trial import (
    PersistedAuthoringBudget,
    run_authoring_batch,
)

from asago_artifact_generator.authoring import (
    AuthoringBudget,
    AuthoringOrchestrator,
    AuthoringPolicy,
    Finding,
    PrivateModelAuthoringTransport,
    PromptPacket,
    ScriptedAuthoringTransport,
    TransportResponse,
)
from asago_artifact_generator.failure_evidence import load_failure_evidence

from .test_fresh_authoring_trial import _case_inputs, _successful_responses
from .test_versioned_authoring_wire import (
    _framed,
    _inventory,
    _plan,
    _runtime_contract,
    _view,
)


def _no_review_policy() -> AuthoringPolicy:
    return AuthoringPolicy(
        plan_max_corrections=0,
        artifact_max_corrections=0,
        review_plan=False,
        review_artifact=False,
    )


def _run_direct(
    tmp_path: Path,
    transport: object,
    responses: list[object],
    *,
    policy: AuthoringPolicy | None = None,
):
    if isinstance(transport, ScriptedAuthoringTransport):
        transport.responses = list(responses)
    return AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="provenance",
        wire_version="v2",
        policy=policy or _no_review_policy(),
        budget=AuthoringBudget(
            aggregate_limit=20,
            task_limit=20,
            author_limit=10,
            review_limit=10,
        ),
    ).run(_view(), _inventory(), _runtime_contract())


def test_dispatch_and_failure_attempts_carry_model_identity_for_all_stages(
    tmp_path: Path,
) -> None:
    invalid_plan = _plan()
    invalid_plan["unexpected"] = "correction required"
    provider_model = "provider-model"
    transport = ScriptedAuthoringTransport(
        [
            TransportResponse(
                raw=json.dumps(invalid_plan).encode(),
                provider_model=provider_model,
            ),
            TransportResponse(raw=json.dumps(_plan()).encode(), provider_model=provider_model),
            TransportResponse(
                raw=b'{"decision":"accept","summary":"ok","findings":[]}',
                provider_model=provider_model,
            ),
            TransportResponse(raw=_framed(), provider_model=provider_model),
            TransportResponse(
                raw=b'{"decision":"accept","summary":"ok","findings":[]}',
                provider_model=provider_model,
            ),
        ]
    )
    transport.profile_name = "private-profile"
    transport.model = "requested-model"

    result = _run_direct(
        tmp_path,
        transport,
        transport.responses,
        policy=AuthoringPolicy(),
    )

    assert result.status == "accepted"
    assert [record["stage"] for record in result.ledger] == [
        "call1",
        "correction",
        "plan_review",
        "call2",
        "artifact_review",
    ]
    expected = {
        "profile_alias": "private-profile",
        "requested_model": "requested-model",
        "returned_model": {
            "availability": "available",
            "value": provider_model,
        },
    }
    assert [record["model_identity"] for record in result.ledger] == [expected] * 5
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    assert [attempt["model_identity"] for attempt in evidence["attempts"]] == [expected] * 5


def test_dispatch_without_transport_metadata_records_unavailable_identity(tmp_path: Path) -> None:
    result = _run_direct(
        tmp_path,
        ScriptedAuthoringTransport([json.dumps(_plan()).encode(), _framed()]),
        [json.dumps(_plan()).encode(), _framed()],
    )

    assert result.status == "accepted"
    assert all(
        record["model_identity"]
        == {
            "profile_alias": None,
            "requested_model": None,
            "returned_model": {
                "availability": "unavailable",
                "reason": "provider_did_not_report_model",
            },
        }
        for record in result.ledger
    )


def test_raising_transport_keeps_requested_identity_and_not_returned_model(
    tmp_path: Path,
) -> None:
    transport = ScriptedAuthoringTransport([RuntimeError("provider unavailable")])
    transport.profile_name = "private-profile"
    transport.model = "requested-model"

    result = _run_direct(
        tmp_path,
        transport,
        transport.responses,
    )

    assert result.status == "transport_failure"
    expected = {
        "profile_alias": "private-profile",
        "requested_model": "requested-model",
        "returned_model": {
            "availability": "unavailable",
            "reason": "not_returned",
        },
    }
    assert result.ledger[0]["model_identity"] == expected
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    assert evidence["attempts"][0]["model_identity"] == expected


def test_private_transport_captures_provider_model_without_persisting_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_key = "test-api-key-must-not-persist"
    base_url = "https://private.example.invalid/v1"

    class FakeCompletions:
        def create(self, **_: object) -> object:
            return SimpleNamespace(
                model="provider-model",
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="not-json"),
                        finish_reason="stop",
                    )
                ],
                usage=None,
            )

    class FakeClient:
        def __init__(self, **_: object) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    transport = PrivateModelAuthoringTransport(
        base_url=base_url,
        api_key=api_key,
        model="requested-model",
        profile_name="private-profile",
    )
    packet = PromptPacket("call1", "test", "system", "user", {})

    response = transport.complete(packet)

    assert response.provider_model == "provider-model"

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="private-credentials",
        wire_version="v2",
        policy=_no_review_policy(),
        budget=AuthoringBudget(aggregate_limit=2, task_limit=2),
    ).run(_view(), _inventory(), _runtime_contract())
    serialized_ledger = json.dumps(result.ledger)
    serialized_evidence = (tmp_path / "package.failure-evidence.json").read_text()
    assert result.status == "unresolved"
    assert api_key not in serialized_ledger
    assert base_url not in serialized_ledger
    assert api_key not in serialized_evidence
    assert base_url not in serialized_evidence


def test_private_transport_uses_none_when_provider_omits_model(monkeypatch) -> None:
    class FakeCompletions:
        def create(self, **_: object) -> object:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
                usage=None,
            )

    class FakeClient:
        def __init__(self, **_: object) -> None:
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.example.invalid/v1",
        api_key="test-api-key",
        model="requested-model",
    )

    response = transport.complete(PromptPacket("call1", "test", "system", "user", {}))
    assert response.provider_model is None


def test_package_raw_response_paths_match_members(tmp_path: Path) -> None:
    raw_responses = [json.dumps(_plan()).encode(), _framed()]
    transport = ScriptedAuthoringTransport(raw_responses)
    result = _run_direct(tmp_path, transport, raw_responses)

    assert result.status == "accepted"
    assert result.package is not None
    for record in result.ledger:
        member_name = record["raw_response"]
        assert member_name in result.package.members
        assert (
            result.package.members[member_name] == result.raw_responses[record["raw_response_key"]]
        )


def test_failed_trial_writes_available_raw_responses_and_matching_hashes(tmp_path: Path) -> None:
    run_dir = tmp_path / "fresh-consumer-five-case-20260923T000099Z"
    run_dir.mkdir()
    budget = PersistedAuthoringBudget.load(run_dir / "authoring" / "budget-ledger.json")
    invalid_responses = [b"not-json", b"still-not-json"]
    transport = ScriptedAuthoringTransport(invalid_responses + _successful_responses(4))

    status = run_authoring_batch(
        run_dir,
        cases=_case_inputs(),
        budget=budget,
        transport_factory=lambda: transport,
        raw_evidence_root=tmp_path / "raw",
    )

    assert status["cases"]["G07"]["status"] == "unresolved"
    case_raw_dir = tmp_path / "raw" / "G07"
    ledger = json.loads((case_raw_dir / "authoring" / "ledger.json").read_text())["ledger"]
    evidence = load_failure_evidence(case_raw_dir / "package.failure-evidence.json")
    attempts = {attempt["dispatch_index"]: attempt for attempt in evidence["attempts"]}
    assert ledger
    for record in ledger:
        assert record["raw_response"].startswith("authoring/")
        raw_path = case_raw_dir / record["raw_response"]
        raw_bytes = raw_path.read_bytes()
        attempt = attempts[record["dispatch_index"]]
        assert attempt["raw_response"]["sha256"] == sha256(raw_bytes).hexdigest()


def test_live_transport_construction_receipt_distinguishes_factory_and_caller_errors(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "fresh-consumer-five-case-20260923T000100Z"
    run_dir.mkdir()
    budget = PersistedAuthoringBudget.load(run_dir / "authoring" / "budget-ledger.json")

    def failing_factory() -> object:
        raise RuntimeError("factory failed")

    status = run_authoring_batch(
        run_dir,
        cases=_case_inputs(),
        budget=budget,
        transport_factory=failing_factory,
        raw_evidence_root=tmp_path / "raw-factory",
    )
    assert status["transport_constructed"] is False
    assert status["cases"]["G07"]["status"] == "transport_construction_failed"

    caller_run_dir = tmp_path / "fresh-consumer-five-case-20260923T000101Z"
    caller_run_dir.mkdir()
    caller_budget = PersistedAuthoringBudget.load(
        caller_run_dir / "authoring" / "budget-ledger.json"
    )

    class RaisingOrchestrator:
        def __init__(self, **_: object) -> None:
            pass

        def run(self, *_: object) -> object:
            raise RuntimeError("caller failed")

    status = run_authoring_batch(
        caller_run_dir,
        cases=_case_inputs(),
        budget=caller_budget,
        transport_factory=lambda: ScriptedAuthoringTransport([]),
        orchestrator_factory=RaisingOrchestrator,
        raw_evidence_root=tmp_path / "raw-caller",
    )
    assert status["transport_constructed"] is True
    assert status["cases"]["G07"]["status"] == "caller_error"


def test_case_receipt_uses_stage_terminal_and_counts_model_identities(tmp_path: Path) -> None:
    identity_a = {
        "profile_alias": "profile-a",
        "requested_model": "requested-a",
        "returned_model": {"availability": "available", "value": "returned-a"},
    }
    identity_b = {
        "profile_alias": "profile-b",
        "requested_model": "requested-b",
        "returned_model": {"availability": "unavailable", "reason": "not_returned"},
    }
    result = SimpleNamespace(
        status="failed",
        ledger=[
            {"stage": "call1", "model_identity": identity_a},
            {"stage": "plan_review", "model_identity": identity_a},
            {"stage": "call2", "model_identity": identity_b},
        ],
        findings=[
            Finding(
                "validation_failure",
                "invalid reference",
                "semantic_judge_spec.fact_refs[0]",
            )
        ],
        prompts={},
        package=None,
        package_path=None,
        failure_evidence_path=None,
    )
    receipt = _case_receipt(
        "G07",
        result,
        budget=PersistedAuthoringBudget.load(tmp_path / "budget.json"),
        package_dir=tmp_path / "package",
        raw_evidence_dir=tmp_path / "raw",
        timings_path=tmp_path / "timings.jsonl",
        unresolved_controls=[],
    )

    assert receipt["terminal_stage"] == "call2"
    assert receipt["reason"] == "validation_failure"
    assert receipt["reason_path"] == "semantic_judge_spec.fact_refs[0]"
    assert receipt["model_identities"] == [
        {
            "profile_alias": "profile-a",
            "requested_model": "requested-a",
            "returned_model": "returned-a",
            "dispatches": 2,
        },
        {
            "profile_alias": "profile-b",
            "requested_model": "requested-b",
            "returned_model": None,
            "dispatches": 1,
        },
    ]
