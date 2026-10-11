"""The one dispatch boundary: every physical request reserves, records, and sends once."""

from __future__ import annotations

from typing import Any

import pytest

from asago_artifact_generator.authoring import transport as transport_module
from asago_artifact_generator.authoring.core import BudgetExceeded
from asago_artifact_generator.authoring.policy import AuthoringBudget
from asago_artifact_generator.authoring.transport import (
    FIRST_ATTEMPT,
    SERVICE_TIER_FALLBACK_REASON,
    TRANSPORT_RETRY,
    AttemptRefused,
    RequestBoundary,
)

_RATE_LIMIT = {"type": "RateLimitError", "status_code": 429}
_BAD_GATEWAY = {"type": "InternalServerError", "status_code": 502}
_REASONS = (FIRST_ATTEMPT, SERVICE_TIER_FALLBACK_REASON, TRANSPORT_RETRY)


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport_module, "_sleep", lambda _: None)


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
