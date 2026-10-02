from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import openai
import pytest

from asago_artifact_generator.authoring import (
    AuthoringBudget,
    AuthoringOrchestrator,
    AuthoringPolicy,
    PrivateModelAuthoringTransport,
    PromptPacket,
    TransportResponse,
)
from asago_artifact_generator.failure_evidence import load_failure_evidence

from .support import ScriptedAuthoringTransport
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
