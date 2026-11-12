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
    RequestOutcome,
    RequestSession,
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
_framed, _authoring_inventory, _authoring_plan, _authoring_runtime_contract = world_builders(
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


def test_a_session_reserves_every_physical_request_before_it_sends() -> None:
    sender = _Sender()
    order: list[str] = []
    session = RequestSession(lambda reason: order.append(f"reserve {reason}") or True)
    boundary = session.new_boundary(lambda **request: order.append("send") or sender(**request))

    boundary.dispatch({"model": "m"}, FIRST_ATTEMPT)
    boundary.dispatch({"model": "m"}, SERVICE_TIER_FALLBACK_REASON, _RATE_LIMIT)

    assert order == [
        f"reserve {FIRST_ATTEMPT}",
        "send",
        f"reserve {SERVICE_TIER_FALLBACK_REASON}",
        "send",
    ]
    assert session.outcome() == RequestOutcome(
        attempts=2,
        retries=({"attempt": 2, "retry_of": _RATE_LIMIT, "reason": SERVICE_TIER_FALLBACK_REASON},),
    )


def test_a_refused_session_sends_nothing_and_reports_no_attempt() -> None:
    sender = _Sender()
    session = RequestSession(lambda _reason: False)
    boundary = session.new_boundary(sender)

    with pytest.raises(AttemptRefused) as refused:
        boundary.dispatch({"model": "m"}, FIRST_ATTEMPT)

    assert refused.value.reason == FIRST_ATTEMPT
    assert sender.requests == []
    assert session.outcome() == RequestOutcome()


def test_a_session_without_a_reserve_hook_allows_every_request() -> None:
    session = RequestSession()
    boundary = session.new_boundary(_Sender())

    boundary.dispatch({"model": "m"}, FIRST_ATTEMPT)
    boundary.dispatch({"model": "m"}, TRANSPORT_RETRY, _BAD_GATEWAY)

    assert session.outcome().attempts == 2
    assert session.outcome().retries == ({"attempt": 2, "retry_of": _BAD_GATEWAY},)


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


def test_the_transport_asks_its_session_before_every_physical_request() -> None:
    asked: list[str] = []
    session = RequestSession(lambda reason: asked.append(reason) or True)
    transport = _tiered(_rate_limit_then_bad_gateway_then(chat_completion()))

    transport.complete(prompt_packet(), session)

    assert asked == [FIRST_ATTEMPT, SERVICE_TIER_FALLBACK_REASON, TRANSPORT_RETRY]
    assert session.outcome() == RequestOutcome(
        attempts=3,
        retries=(
            {"attempt": 2, "retry_of": _RATE_LIMIT, "reason": SERVICE_TIER_FALLBACK_REASON},
            {"attempt": 3, "retry_of": _BAD_GATEWAY},
        ),
    )


def test_a_refused_first_attempt_sends_nothing() -> None:
    client = scripted_client([chat_completion()])
    transport = private_transport(client=client)
    session = RequestSession(lambda _reason: False)

    with pytest.raises(AttemptRefused):
        transport.complete(prompt_packet(), session)

    assert client.requests == []
    assert session.outcome() == RequestOutcome()


def test_a_fallback_followed_by_a_retryable_error_cannot_pass_the_cap() -> None:
    budget = _budget(2)
    client = _rate_limit_then_bad_gateway_then(chat_completion())
    transport = _tiered(client)
    session = RequestSession(_budget_gate(budget))

    with pytest.raises(openai.InternalServerError):
        transport.complete(prompt_packet(), session)

    assert len(client.requests) == 2
    assert budget.total_dispatched == 2
    assert session.outcome().retries == (
        {"attempt": 2, "retry_of": _RATE_LIMIT, "reason": SERVICE_TIER_FALLBACK_REASON},
    )


def test_a_refused_fallback_keeps_the_controls_of_the_request_that_was_sent() -> None:
    transport = _tiered(scripted_client([status_error(429)]))
    session = RequestSession(lambda reason: reason == FIRST_ATTEMPT)

    with pytest.raises(openai.RateLimitError):
        transport.complete(prompt_packet(), session)

    assert transport.last_controls is not None
    assert transport.last_controls["service_tier"] == "flex"
    assert transport.last_controls["service_tier_fallback_used"] is False
    assert session.outcome() == RequestOutcome(attempts=1)


# --- the orchestrator opens one request session per dispatch -----------------------


class _SessionRecordingTransport(PrivateModelAuthoringTransport):
    """Record the session each dispatch hands to the transport."""

    sessions: list[RequestSession]

    def complete(self, packet: Any, session: RequestSession | None = None) -> Any:
        assert session is not None, "the orchestrator opens a session before the dispatch"
        self.sessions.append(session)
        return super().complete(packet, session)


def _session_transport(client: Any) -> _SessionRecordingTransport:
    transport = _SessionRecordingTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
        service_tier="flex",
        service_tier_fallback="default",
        client=client,
    )
    transport.sessions = []
    return transport


def _orchestrate(tmp_path: Path, transport: Any, budget: AuthoringBudget) -> Any:
    return stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="task",
        budget=budget,
    ).run(_view(), _authoring_inventory(), _authoring_runtime_contract())


def test_the_orchestrator_opens_one_session_per_dispatch(tmp_path: Path) -> None:
    budget = _budget(10)
    transport = _session_transport(
        _rate_limit_then_bad_gateway_then(
            chat_completion(json.dumps(_authoring_plan())), chat_completion(_framed().decode())
        )
    )

    result = _orchestrate(tmp_path, transport, budget)

    assert result.status == "accepted"
    assert [session.outcome() for session in transport.sessions] == [
        RequestOutcome(
            attempts=3,
            retries=(
                {"attempt": 2, "retry_of": _RATE_LIMIT, "reason": SERVICE_TIER_FALLBACK_REASON},
                {"attempt": 3, "retry_of": _BAD_GATEWAY},
            ),
        ),
        RequestOutcome(attempts=1),
    ]
    assert budget.total_dispatched == 4
    assert [record["dispatch_index"] for record in result.ledger] == [1, 2]
    assert [retry["attempt"] for retry in result.ledger[0]["transport_retries"]] == [2, 3]


class _ReserveRecordingBudget(AuthoringBudget):
    """Record each reservation at the moment the budget makes it."""

    def __init__(self, events: list[str], **options: Any) -> None:
        super().__init__(**options)
        self.events = events

    def reserve(self, task_id: str, *, role: str = "author") -> int:
        self.events.append("reserve")
        return super().reserve(task_id, role=role)


def test_the_orchestrator_reserves_just_before_every_physical_request(tmp_path: Path) -> None:
    events: list[str] = []
    budget = _ReserveRecordingBudget(
        events, aggregate_limit=10, task_limit=10, author_limit=10, review_limit=10
    )
    client = _rate_limit_then_bad_gateway_then(
        chat_completion(json.dumps(_authoring_plan())), chat_completion(_framed().decode())
    )
    send = client.chat.completions.create

    def recorded_send(**request: Any) -> Any:
        events.append("send")
        return send(**request)

    client.chat.completions.create = recorded_send

    result = _orchestrate(tmp_path, _session_transport(client), budget)

    assert result.status == "accepted"
    assert events == ["reserve", "send"] * 4


def test_a_first_attempt_over_the_cap_opens_no_record_and_leaves_the_last_one_alone(
    tmp_path: Path,
) -> None:
    # The plan arrives on the fallback tier, so its controls differ from those
    # of the artifact request the budget refuses before it is sent.
    budget = _budget(2)
    client = scripted_client(
        [status_error(429), chat_completion(json.dumps(_authoring_plan())), chat_completion()]
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
