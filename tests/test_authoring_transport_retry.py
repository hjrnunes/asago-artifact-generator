"""One recorded retry after a transport error in the authoring transport."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest

from asago_artifact_generator.authoring import transport as transport_module
from asago_artifact_generator.authoring.core import PromptPacket
from asago_artifact_generator.authoring.policy import AuthoringBudget
from asago_artifact_generator.authoring.transport import PrivateModelAuthoringTransport

from .support import load_failure_evidence, stage_local_orchestrator
from .test_authoring_orchestration import _view
from .test_versioned_authoring_wire import _framed
from .test_versioned_authoring_wire import _inventory as _inventory_v2
from .test_versioned_authoring_wire import _plan as _plan_v2
from .test_versioned_authoring_wire import _runtime_contract as _runtime_contract_v2

_REQUEST = httpx2.Request("POST", "https://private.invalid/v1")


def _packet(stage: str = "call1") -> PromptPacket:
    return PromptPacket(stage=stage, version="test", system="system", user="user", payload={})


def _response(content: str = "{}") -> object:
    return SimpleNamespace(
        model="returned-model",
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=None,
    )


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(message="connection reset", request=_REQUEST)


def _status_error(status: int) -> openai.APIStatusError:
    response = httpx2.Response(status, request=_REQUEST)
    if status == 429:
        return openai.RateLimitError("rate limited", response=response, body=None)
    if status >= 500:
        return openai.InternalServerError("upstream failed", response=response, body=None)
    return openai.BadRequestError("bad request", response=response, body=None)


class _FakeClient:
    """Raise or return each scripted outcome in turn, keeping every request."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, object]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **request: object) -> object:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _transport(client: _FakeClient, **options: Any) -> PrivateModelAuthoringTransport:
    return PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="author-model",
        client=client,
        **options,
    )


@pytest.mark.parametrize(
    ("failure", "recorded"),
    [
        (_connection_error(), {"type": "APIConnectionError", "status_code": None}),
        (_status_error(502), {"type": "InternalServerError", "status_code": 502}),
        (_status_error(500), {"type": "InternalServerError", "status_code": 500}),
    ],
    ids=["connection", "502", "500"],
)
def test_a_transport_error_is_retried_once_and_recorded(
    failure: BaseException, recorded: dict[str, object]
) -> None:
    client = _FakeClient([failure, _response('{"ok": true}')])
    transport = _transport(client)

    response = transport.complete(_packet())

    assert response.raw == b'{"ok": true}'
    assert len(client.requests) == 2
    assert client.requests[0] == client.requests[1]
    assert transport.last_retries == [{"attempt": 2, "retry_of": recorded}]


def test_a_call_without_a_failure_records_no_retry() -> None:
    transport = _transport(_FakeClient([_response()]))

    transport.complete(_packet())

    assert transport.last_retries == []


def test_the_retry_record_does_not_outlive_its_call() -> None:
    client = _FakeClient([_connection_error(), _response(), _response()])
    transport = _transport(client)

    transport.complete(_packet())
    transport.complete(_packet())

    assert transport.last_retries == []


@pytest.mark.parametrize(
    "failure",
    [_connection_error(), _status_error(503)],
    ids=["connection", "503"],
)
def test_two_transport_errors_surface_after_exactly_two_requests(
    failure: BaseException,
) -> None:
    client = _FakeClient([failure, failure])
    transport = _transport(client)

    with pytest.raises(type(failure)):
        transport.complete(_packet())

    assert len(client.requests) == 2
    assert len(transport.last_retries) == 1


@pytest.mark.parametrize(
    "failure",
    [
        openai.APITimeoutError(request=_REQUEST),
        _status_error(400),
        _status_error(429),
        _status_error(404),
    ],
    ids=["timeout", "400", "429", "404"],
)
def test_other_provider_errors_are_never_retried(failure: BaseException) -> None:
    client = _FakeClient([failure])
    transport = _transport(client)

    with pytest.raises(type(failure)):
        transport.complete(_packet())

    assert len(client.requests) == 1
    assert transport.last_retries == []


def test_the_service_tier_fallback_keeps_its_own_rule_and_a_5xx_on_it_retries() -> None:
    client = _FakeClient([_status_error(429), _status_error(502), _response()])
    transport = _transport(client, service_tier="flex", service_tier_fallback="default")

    response = transport.complete(_packet())

    assert [request.get("service_tier") for request in client.requests] == [
        "flex",
        "default",
        "default",
    ]
    assert response.controls["service_tier_fallback_used"] is True
    assert transport.last_retries == [
        {"attempt": 2, "retry_of": {"type": "InternalServerError", "status_code": 502}}
    ]


def test_a_validation_failure_is_not_a_transport_error() -> None:
    client = _FakeClient([_response("not json at all")])
    transport = _transport(client)

    transport.complete(_packet())

    assert len(client.requests) == 1
    assert transport.last_retries == []


def test_the_retry_waits_the_fixed_delay_once(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(transport_module, "_sleep", slept.append)
    monkeypatch.setattr(transport_module, "RETRY_DELAY_SECONDS", 0.75)

    _transport(_FakeClient([_connection_error(), _response()])).complete(_packet())

    assert slept == [0.75]


def test_the_default_delay_is_short() -> None:
    assert 0 <= transport_module.DEFAULT_RETRY_DELAY_SECONDS <= 2


def test_sdk_retries_stay_off_and_the_controls_say_so() -> None:
    client = _FakeClient([_connection_error(), _response()])
    transport = _transport(client)

    response = transport.complete(_packet())

    assert transport.max_retries == 0
    assert response.controls["max_retries"] == 0


def test_a_refusing_gate_stops_the_retry_and_surfaces_the_first_error() -> None:
    client = _FakeClient([_connection_error(), _response()])
    transport = _transport(client)
    transport.retry_gate = lambda: False

    with pytest.raises(openai.APIConnectionError):
        transport.complete(_packet())

    assert len(client.requests) == 1
    assert transport.last_retries == []


def test_an_allowing_gate_is_asked_once_per_retry() -> None:
    asked: list[bool] = []
    client = _FakeClient([_connection_error(), _response()])
    transport = _transport(client)
    transport.retry_gate = lambda: asked.append(True) or True

    transport.complete(_packet())

    assert asked == [True]


# --- orchestrator: budget, ledger, evidence, package --------------------------------


def _plan_then_artifact(*first: object) -> _FakeClient:
    return _FakeClient([*first, _response(json.dumps(_plan_v2())), _response(_framed().decode())])


def _run(tmp_path: Path, client: _FakeClient, **kwargs: Any) -> Any:
    return stage_local_orchestrator(
        transport=_transport(client),
        package_dir=tmp_path / "package",
        task_id="retry-task",
        **kwargs,
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())


def test_a_retried_dispatch_still_produces_the_accepted_package(tmp_path: Path) -> None:
    client = _plan_then_artifact(_connection_error())

    result = _run(tmp_path, client)

    assert result.status == "accepted"
    assert len(client.requests) == 3
    assert [record["stage"] for record in result.ledger] == ["call1", "call2"]


def test_the_budget_counts_both_attempts(tmp_path: Path) -> None:
    budget = AuthoringBudget(aggregate_limit=10, task_limit=10, author_limit=10, review_limit=10)
    client = _plan_then_artifact(_status_error(502))

    result = _run(tmp_path, client, budget=budget)

    assert result.status == "accepted"
    assert budget.total_dispatched == 3
    assert budget.dispatched_by_task["retry-task"] == 3
    assert budget.dispatched_by_task_role["retry-task"]["author"] == 3


def test_the_ledger_and_the_evidence_record_the_retry_on_its_dispatch(
    tmp_path: Path,
) -> None:
    client = _plan_then_artifact(_status_error(502))

    result = _run(tmp_path, client)

    expected = [{"attempt": 2, "retry_of": {"type": "InternalServerError", "status_code": 502}}]
    first, second = result.ledger
    assert first["transport_retries"] == expected
    assert "transport_retries" not in second
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    assert evidence["attempts"][0]["transport_retries"] == expected
    assert "transport_retries" not in evidence["attempts"][1]
    assert first["controls"]["max_retries"] == 0


def test_the_package_ledger_carries_the_retry_and_keeps_max_retries_zero(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, _plan_then_artifact(_connection_error()))

    assert result.package is not None
    ledger = json.loads(result.package.members["authoring/ledger.json"])
    assert ledger[0]["transport_retries"][0]["retry_of"]["type"] == "APIConnectionError"
    assert result.package.manifest.authoring["max_retries"] == 0
    assert result.package.manifest.authoring["budget"]["task_spent"] == 3


def test_a_ledger_without_a_retry_has_no_retry_key(tmp_path: Path) -> None:
    result = _run(tmp_path, _plan_then_artifact())

    assert all("transport_retries" not in record for record in result.ledger)


def test_two_transport_errors_fail_the_stage_after_two_requests(tmp_path: Path) -> None:
    failure = _connection_error()
    budget = AuthoringBudget(aggregate_limit=10, task_limit=10, author_limit=10, review_limit=10)
    client = _FakeClient([failure, failure])

    result = _run(tmp_path, client, budget=budget)

    assert result.status == "transport_failure"
    assert len(client.requests) == 2
    assert budget.total_dispatched == 2
    assert [finding.code for finding in result.findings] == ["transport_failure"]
    assert result.ledger[0]["transport_retries"][0]["retry_of"]["type"] == "APIConnectionError"


def test_a_budget_with_no_request_left_refuses_the_retry(tmp_path: Path) -> None:
    budget = AuthoringBudget(aggregate_limit=1, task_limit=10, author_limit=10, review_limit=10)
    client = _FakeClient([_connection_error(), _response()])

    result = _run(tmp_path, client, budget=budget)

    assert result.status == "transport_failure"
    assert len(client.requests) == 1
    assert budget.total_dispatched == 1
    assert [finding.code for finding in result.findings] == ["transport_failure"]
    assert "transport_retries" not in result.ledger[0]


def test_a_non_retryable_failure_costs_one_request(tmp_path: Path) -> None:
    budget = AuthoringBudget(aggregate_limit=10, task_limit=10, author_limit=10, review_limit=10)
    client = _FakeClient([_status_error(400)])

    result = _run(tmp_path, client, budget=budget)

    assert result.status == "transport_failure"
    assert len(client.requests) == 1
    assert budget.total_dispatched == 1
