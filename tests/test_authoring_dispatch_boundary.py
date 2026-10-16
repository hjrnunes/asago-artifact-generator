"""The one dispatch boundary: every physical request reserves, records, and sends once."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openai
import pytest

from asago_artifact_generator.authoring import transport as transport_module
from asago_artifact_generator.authoring.core import BudgetExceeded
from asago_artifact_generator.authoring.policy import AuthoringBudget
from asago_artifact_generator.authoring.transport import (
    FIRST_ATTEMPT,
    SERVICE_TIER_FALLBACK_REASON,
    TRANSPORT_RETRY,
    AttemptRefused,
    PrivateModelAuthoringTransport,
    RequestBoundary,
)

from .support import (
    chat_completion,
    load_failure_evidence,
    private_transport,
    prompt_packet,
    scripted_client,
    stage_local_orchestrator,
    status_error,
    world_builders,
)

(_view,) = world_builders("refund-minimal", "view")
_framed, _inventory_v2, _plan_v2, _runtime_contract_v2 = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract"
)

_RATE_LIMIT = {"type": "RateLimitError", "status_code": 429}
_BAD_GATEWAY = {"type": "InternalServerError", "status_code": 502}
_REASONS = (FIRST_ATTEMPT, SERVICE_TIER_FALLBACK_REASON, TRANSPORT_RETRY)


class _Sender:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, **request: Any) -> str:
        self.requests.append(request)
        return f"response {len(self.requests)}"


def _budget_gate(budget: AuthoringBudget, asked: list[str] | None = None):
    def gate(reason: str) -> bool:
        if asked is not None:
            asked.append(reason)
        try:
            budget.reserve("task", role="author")
        except BudgetExceeded:
            return False
        return True

    return gate


def _budget(limit: int, *, spent: int = 0) -> AuthoringBudget:
    budget = AuthoringBudget(
        aggregate_limit=limit, task_limit=limit, author_limit=limit, review_limit=limit
    )
    for _ in range(spent):
        budget.reserve("task", role="author")
    return budget


def test_one_dispatch_reserves_then_sends_exactly_one_request() -> None:
    sender = _Sender()
    order: list[str] = []
    boundary = RequestBoundary(
        lambda **request: order.append("send") or sender(**request),
        gate=lambda reason: order.append(f"reserve {reason}") or True,
    )

    response = boundary.dispatch({"model": "m"}, FIRST_ATTEMPT)

    assert response == "response 1"
    assert sender.requests == [{"model": "m"}]
    assert order == [f"reserve {FIRST_ATTEMPT}", "send"]


@pytest.mark.parametrize("reason", _REASONS)
def test_with_every_request_reserved_the_next_attempt_of_any_reason_is_refused(
    reason: str,
) -> None:
    sender = _Sender()
    budget = _budget(2, spent=2)
    boundary = RequestBoundary(sender, gate=_budget_gate(budget))

    with pytest.raises(AttemptRefused) as refused:
        boundary.dispatch(
            {"model": "m"}, reason, None if reason == FIRST_ATTEMPT else _BAD_GATEWAY
        )

    assert refused.value.reason == reason
    assert sender.requests == []
    assert boundary.attempts == []
    assert budget.total_dispatched == 2


def test_the_cap_counts_physical_attempts_of_every_reason_together() -> None:
    sender = _Sender()
    budget = _budget(2)
    boundary = RequestBoundary(sender, gate=_budget_gate(budget))

    boundary.dispatch({"model": "m"}, FIRST_ATTEMPT)
    boundary.dispatch({"model": "m"}, SERVICE_TIER_FALLBACK_REASON, _RATE_LIMIT)
    with pytest.raises(AttemptRefused):
        boundary.dispatch({"model": "m"}, TRANSPORT_RETRY, _BAD_GATEWAY)

    assert len(sender.requests) == 2
    assert budget.total_dispatched == 2


def test_each_reason_is_recorded_once_per_attempt() -> None:
    asked: list[str] = []
    boundary = RequestBoundary(_Sender(), gate=_budget_gate(_budget(10), asked))

    boundary.dispatch({"model": "m"}, FIRST_ATTEMPT)
    boundary.dispatch({"model": "m"}, SERVICE_TIER_FALLBACK_REASON, _RATE_LIMIT)
    boundary.dispatch({"model": "m"}, TRANSPORT_RETRY, _BAD_GATEWAY)

    assert asked == list(_REASONS)
    assert boundary.attempts == [
        {"attempt": 1, "reason": FIRST_ATTEMPT, "retry_of": None},
        {"attempt": 2, "reason": SERVICE_TIER_FALLBACK_REASON, "retry_of": _RATE_LIMIT},
        {"attempt": 3, "reason": TRANSPORT_RETRY, "retry_of": _BAD_GATEWAY},
    ]


def test_the_ledger_lists_only_the_requests_after_the_first_in_their_recorded_shape() -> None:
    boundary = RequestBoundary(_Sender())

    boundary.dispatch({"model": "m"}, FIRST_ATTEMPT)
    assert boundary.extra_requests() == []
    boundary.dispatch({"model": "m"}, SERVICE_TIER_FALLBACK_REASON, _RATE_LIMIT)
    boundary.dispatch({"model": "m"}, TRANSPORT_RETRY, _BAD_GATEWAY)

    assert boundary.extra_requests() == [
        {"attempt": 2, "retry_of": _RATE_LIMIT, "reason": SERVICE_TIER_FALLBACK_REASON},
        {"attempt": 3, "retry_of": _BAD_GATEWAY},
    ]


def test_an_attempt_whose_send_raises_is_still_recorded_and_reserved() -> None:
    budget = _budget(10)

    def failing(**request: Any) -> Any:
        raise ConnectionError("reset")

    boundary = RequestBoundary(failing, gate=_budget_gate(budget))

    with pytest.raises(ConnectionError):
        boundary.dispatch({"model": "m"}, FIRST_ATTEMPT)

    assert [attempt["reason"] for attempt in boundary.attempts] == [FIRST_ATTEMPT]
    assert budget.total_dispatched == 1


def test_without_a_gate_every_attempt_is_allowed() -> None:
    sender = _Sender()
    boundary = RequestBoundary(sender)

    for reason in _REASONS:
        boundary.dispatch({"model": "m"}, reason)

    assert len(sender.requests) == 3


def test_only_a_transport_retry_waits_the_fixed_delay_after_its_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        transport_module, "_sleep", lambda seconds: events.append(f"sleep {seconds}")
    )
    monkeypatch.setattr(transport_module, "RETRY_DELAY_SECONDS", 0.5)
    boundary = RequestBoundary(
        lambda **request: events.append("send"),
        gate=lambda reason: events.append(f"reserve {reason}") or True,
    )

    for reason in _REASONS:
        boundary.dispatch({"model": "m"}, reason)

    assert events == [
        f"reserve {FIRST_ATTEMPT}",
        "send",
        f"reserve {SERVICE_TIER_FALLBACK_REASON}",
        "send",
        f"reserve {TRANSPORT_RETRY}",
        "sleep 0.5",
        "send",
    ]


# --- the transport sends only through the boundary ---------------------------------


def _tiered(client: Any, **options: Any) -> PrivateModelAuthoringTransport:
    return private_transport(
        client=client, service_tier="flex", service_tier_fallback="default", **options
    )


def _rate_limit_then_bad_gateway_then(*rest: Any) -> Any:
    return scripted_client([status_error(429), status_error(502), *rest])


def test_every_physical_request_of_the_transport_passes_through_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    dispatch = RequestBoundary.dispatch

    def spy(self: RequestBoundary, request: dict[str, Any], reason: str, *rest: Any) -> Any:
        seen.append(reason)
        return dispatch(self, request, reason, *rest)

    monkeypatch.setattr(RequestBoundary, "dispatch", spy)
    client = _rate_limit_then_bad_gateway_then(chat_completion())

    _tiered(client).complete(prompt_packet())

    assert seen == [FIRST_ATTEMPT, SERVICE_TIER_FALLBACK_REASON, TRANSPORT_RETRY]
    assert len(client.requests) == 3


def test_the_transport_asks_its_attempt_gate_for_every_physical_request() -> None:
    asked: list[str] = []
    transport = _tiered(_rate_limit_then_bad_gateway_then(chat_completion()))
    transport.attempt_gate = _budget_gate(_budget(10), asked)

    transport.complete(prompt_packet())

    assert asked == [FIRST_ATTEMPT, SERVICE_TIER_FALLBACK_REASON, TRANSPORT_RETRY]


def test_a_refused_first_attempt_sends_nothing() -> None:
    client = scripted_client([chat_completion()])
    transport = private_transport(client=client)
    transport.attempt_gate = lambda reason: False

    with pytest.raises(AttemptRefused):
        transport.complete(prompt_packet())

    assert client.requests == []
    assert transport.last_retries == []


def test_a_fallback_followed_by_a_retryable_error_cannot_pass_the_cap() -> None:
    budget = _budget(2)
    client = _rate_limit_then_bad_gateway_then(chat_completion())
    transport = _tiered(client)
    transport.attempt_gate = _budget_gate(budget)

    with pytest.raises(openai.InternalServerError):
        transport.complete(prompt_packet())

    assert len(client.requests) == 2
    assert budget.total_dispatched == 2
    assert transport.last_retries == [
        {"attempt": 2, "retry_of": _RATE_LIMIT, "reason": SERVICE_TIER_FALLBACK_REASON}
    ]


def test_a_refused_fallback_keeps_the_controls_of_the_request_that_was_sent() -> None:
    transport = _tiered(scripted_client([status_error(429)]))
    transport.attempt_gate = lambda reason: reason == FIRST_ATTEMPT

    with pytest.raises(openai.RateLimitError):
        transport.complete(prompt_packet())

    assert transport.last_controls is not None
    assert transport.last_controls["service_tier"] == "flex"
    assert transport.last_controls["service_tier_fallback_used"] is False


# --- the orchestrator reserves every physical request at the boundary --------------


class _GateSpyTransport(PrivateModelAuthoringTransport):
    """Record each reason the orchestrator's gate is asked about."""

    asked: list[str]

    def complete(self, packet: Any) -> Any:
        gate = self.attempt_gate
        assert gate is not None, "the orchestrator installs the gate before the request"
        self.attempt_gate = lambda reason: self.asked.append(reason) or gate(reason)
        try:
            return super().complete(packet)
        finally:
            self.attempt_gate = gate


def _spy_transport(client: Any) -> _GateSpyTransport:
    transport = _GateSpyTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
        service_tier="flex",
        service_tier_fallback="default",
        client=client,
    )
    transport.asked = []
    return transport


def _orchestrate(tmp_path: Path, transport: Any, budget: AuthoringBudget) -> Any:
    return stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="task",
        budget=budget,
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())


def test_the_orchestrator_reserves_every_physical_request_at_the_boundary(
    tmp_path: Path,
) -> None:
    budget = _budget(10)
    transport = _spy_transport(
        _rate_limit_then_bad_gateway_then(
            chat_completion(json.dumps(_plan_v2())), chat_completion(_framed().decode())
        )
    )

    result = _orchestrate(tmp_path, transport, budget)

    assert result.status == "accepted"
    assert transport.asked == [
        FIRST_ATTEMPT,
        SERVICE_TIER_FALLBACK_REASON,
        TRANSPORT_RETRY,
        FIRST_ATTEMPT,
    ]
    assert budget.total_dispatched == 4
    assert [record["dispatch_index"] for record in result.ledger] == [1, 2]
    assert [retry["attempt"] for retry in result.ledger[0]["transport_retries"]] == [2, 3]
    assert transport.attempt_gate is None


def test_a_first_attempt_over_the_cap_opens_no_record_and_leaves_the_last_one_alone(
    tmp_path: Path,
) -> None:
    # The plan arrives on the fallback tier, so its controls differ from those
    # of the artifact request the budget refuses before it is sent.
    budget = _budget(2)
    client = scripted_client(
        [status_error(429), chat_completion(json.dumps(_plan_v2())), chat_completion()]
    )
    transport = _tiered(client)

    result = _orchestrate(tmp_path, transport, budget)

    assert result.status == "budget_exhausted"
    assert len(client.requests) == 2
    [record] = result.ledger
    assert record["stage"] == "call1"
    assert record["controls"]["service_tier"] == "default"
    assert record["controls"]["service_tier_fallback_used"] is True
    evidence = load_failure_evidence(tmp_path / "package.failure-evidence.json")
    [attempt] = evidence["attempts"]
    assert attempt["raw_response"]["availability"] == "available"
    assert attempt["controls"]["value"]["service_tier_fallback_used"] is True
    assert "failure" not in attempt
    assert [finding["code"] for finding in evidence["findings"]] == ["budget_exhausted"]
