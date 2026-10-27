"""Recorded plan-review replies that used to end an item get a bounded review retry.

``tests/fixtures/review_replies/`` holds plan_review replies a reviewer model
returned in recorded authoring runs, cut to the bytes these tests need:

- ``plan-review-missing-required-change.txt``: a complete reply whose only
  finding has no ``required_change`` (finish reason ``stop``).
- ``plan-review-token-limit-open-fence.txt``: a reply that ran until the
  16384-token completion limit inside its ```json fence; its first finding up
  to the field the model started writing next, then the last lines it wrote.
- ``plan-review-token-limit-after-fence.txt``: a reply that closed its fence
  and then kept writing until the same limit; the fenced object and the first
  lines after it.

Each test sends the replies through the real request boundary with a scripted
client; nothing contacts a model endpoint.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import AuthoringBudget, AuthoringPolicy
from asago_artifact_generator.authoring.templates import prose

from .support import (
    FIXTURES,
    chat_completion,
    load_failure_evidence,
    private_transport,
    review_response,
    scripted_client,
    world_builders,
)

_framed, _inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract", "view"
)

_REPLIES = FIXTURES / "review_replies"
MISSING_REQUIRED_CHANGE = (_REPLIES / "plan-review-missing-required-change.txt").read_bytes()
OPEN_FENCE = (_REPLIES / "plan-review-token-limit-open-fence.txt").read_bytes()
AFTER_FENCE = (_REPLIES / "plan-review-token-limit-after-fence.txt").read_bytes()

_LIMIT = 16384
_TRUNCATED = {
    "code": "review_truncated",
    "detail": (
        f"review response stopped at the completion token limit of {_LIMIT} tokens before it "
        'was complete (finish_reason "length"); a review response must be complete within that '
        "limit"
    ),
    "path": "review",
}
_UNCLOSED = {
    "code": "truncated_fence",
    "detail": "review response lowercase ```json fence is not closed",
    "path": "review",
}
_AFTER_FENCE = {
    "code": "trailing_content",
    "detail": "review response contains content outside its JSON fence",
    "path": "review",
}
_MISSING = {
    "code": "review_schema",
    "detail": (
        "review.findings[0] must have exactly question, location, problem, basis, and "
        "required_change (missing=['required_change'], unknown=[])"
    ),
    "path": "review.findings[0]",
}


def _stopped(content: bytes, **fields: Any) -> Any:
    return chat_completion(content.decode(), finish_reason="stop", **fields)


def _cut_off(content: bytes, **fields: Any) -> Any:
    usage = {"prompt_tokens": 19806, "completion_tokens": _LIMIT, "total_tokens": 19806 + _LIMIT}
    return chat_completion(content.decode(), finish_reason="length", usage=usage, **fields)


def _run(
    tmp_path: Path, completions: list[Any], **policy: Any
) -> tuple[Any, Any, AuthoringBudget]:
    """Run the plan stage alone through the request boundary over ``completions``."""

    client = scripted_client(completions)
    budget = AuthoringBudget(aggregate_limit=10, task_limit=10, author_limit=10, review_limit=10)
    orchestrator = AuthoringOrchestrator(
        transport=private_transport(
            client=client, max_completion_tokens=_LIMIT, context_window_tokens=65536
        ),
        package_dir=tmp_path / "package",
        task_id="recorded-review",
        budget=budget,
        policy=AuthoringPolicy(review_artifact=False, **policy),
    )
    return orchestrator.run(_view(), _inventory(), _runtime_contract()), client, budget


def _plan_reply() -> Any:
    return _stopped(json.dumps(_plan()).encode())


def _retry_instruction() -> str:
    return prose("review/rejected_response_instruction")


def _user(request: dict[str, Any]) -> str:
    return next(message["content"] for message in request["messages"] if message["role"] == "user")


def _raw(attempt: dict[str, Any]) -> bytes:
    return base64.b64decode(attempt["raw_response"]["base64"])


def test_a_recorded_finding_without_required_change_is_asked_again_and_proceeds(
    tmp_path: Path,
) -> None:
    result, client, budget = _run(
        tmp_path,
        [
            _plan_reply(),
            _stopped(MISSING_REQUIRED_CHANGE),
            _stopped(review_response()),
            _stopped(_framed()),
        ],
    )

    assert result.status == "accepted"
    assert [record["stage"] for record in result.ledger] == [
        "call1",
        "plan_review",
        "plan_review",
        "call2",
    ]
    rejected, retry = result.ledger[1], result.ledger[2]
    assert rejected["review_error"] == [_MISSING]
    assert retry["allowance"] == "review_retry"
    assert retry["retries_dispatch"] == rejected["dispatch_index"]
    assert result.review_retry_allowances == {"plan": 0, "artifact": 0}
    assert budget.dispatched_by_task_role["recorded-review"] == {"author": 2, "reviewer": 2}
    retry_user = _user(client.requests[2])
    assert f"REJECTED REVIEW RESPONSE\n{MISSING_REQUIRED_CHANGE.decode()}\n" in retry_user
    assert _MISSING["detail"] in retry_user
    assert "completion token limit" not in retry_user


@pytest.mark.parametrize(
    ("reply", "check_finding"),
    [(OPEN_FENCE, _UNCLOSED), (AFTER_FENCE, _AFTER_FENCE)],
    ids=["open_fence", "after_fence"],
)
def test_a_recorded_reply_cut_off_at_the_token_limit_is_asked_again_and_proceeds(
    tmp_path: Path, reply: bytes, check_finding: dict[str, str]
) -> None:
    result, client, budget = _run(
        tmp_path,
        [_plan_reply(), _cut_off(reply), _stopped(review_response()), _stopped(_framed())],
    )

    assert result.status == "accepted"
    assert result.review_status["plan"] == "accepted"
    rejected, retry = result.ledger[1], result.ledger[2]
    assert rejected["review_error"] == [_TRUNCATED, check_finding]
    assert rejected["failure"]["code"] == "review_unavailable"
    assert retry["allowance"] == "review_retry"
    assert retry["retries_dispatch"] == rejected["dispatch_index"]
    # The cut-off reply is a rejected response, never a transport retry.
    assert all("transport_retries" not in record for record in result.ledger)
    assert len(client.requests) == 4
    assert budget.dispatched_by_task_role["recorded-review"] == {"author": 2, "reviewer": 2}
    # The reply, its finish reason, and its usage stay separate records.
    assert result.raw_responses[rejected["raw_response_key"]] == reply
    assert rejected["response_capture"]["finish_reason"] == {"state": "value", "value": "length"}
    assert rejected["response_capture"]["final_answer"]["content"] == reply.decode()
    assert rejected["usage"]["completion_tokens"] == _LIMIT


def test_the_retry_after_a_cut_off_reply_shows_the_reply_and_the_limit_it_must_fit(
    tmp_path: Path,
) -> None:
    _, client, _ = _run(
        tmp_path,
        [_plan_reply(), _cut_off(OPEN_FENCE), _stopped(review_response()), _stopped(_framed())],
    )

    first, retry = client.requests[1], client.requests[2]
    first_user, retry_user = _user(first), _user(retry)
    assert retry_user.startswith(first_user.rstrip("\n"))
    assert f"REJECTED REVIEW RESPONSE\n{OPEN_FENCE.decode()}\n" in retry_user
    assert json.dumps(_TRUNCATED["detail"]) in retry_user
    assert json.dumps(_UNCLOSED["detail"]) in retry_user
    # The retry adds the same sections as any review retry: no new prompt text.
    assert retry_user.endswith(f"REVIEW RESPONSE RETRY INSTRUCTION\n{_retry_instruction()}\n")
    # The retry keeps the request's limits; it asks for less, not for more room.
    assert retry["max_completion_tokens"] == first["max_completion_tokens"] == _LIMIT


def test_a_reply_cut_off_at_the_limit_is_rejected_even_when_its_text_parses(
    tmp_path: Path,
) -> None:
    result, _, _ = _run(
        tmp_path,
        [
            _plan_reply(),
            _cut_off(review_response()),
            _stopped(review_response()),
            _stopped(_framed()),
        ],
    )

    assert result.status == "accepted"
    assert result.ledger[1]["review_error"] == [_TRUNCATED]
    assert result.ledger[2]["allowance"] == "review_retry"


def test_reasoning_beside_a_cut_off_reply_is_never_read_as_the_review(tmp_path: Path) -> None:
    result, client, _ = _run(
        tmp_path,
        [
            _plan_reply(),
            _cut_off(OPEN_FENCE, reasoning_content=review_response().decode()),
            _stopped(review_response()),
            _stopped(_framed()),
        ],
    )

    assert result.ledger[1]["review_error"] == [_TRUNCATED, _UNCLOSED]
    payload_response = _user(client.requests[2]).split("REJECTED REVIEW RESPONSE\n", 1)[1]
    assert payload_response.startswith(OPEN_FENCE.decode())


@pytest.mark.parametrize(
    ("first", "first_findings"),
    [
        (_stopped(MISSING_REQUIRED_CHANGE), [_MISSING]),
        (_cut_off(OPEN_FENCE), [_TRUNCATED, _UNCLOSED]),
    ],
    ids=["missing_required_change", "cut_off"],
)
def test_the_item_ends_only_once_the_review_retry_allowance_is_spent(
    tmp_path: Path, first: Any, first_findings: list[dict[str, str]]
) -> None:
    result, client, budget = _run(tmp_path, [_plan_reply(), first, _cut_off(AFTER_FENCE)])

    assert result.status == "review_unavailable"
    assert len(client.requests) == 3
    assert budget.dispatched_by_task_role["recorded-review"] == {"author": 1, "reviewer": 2}
    assert result.review_retry_allowances == {"plan": 0, "artifact": 0}
    assert [finding.code for finding in result.findings] == [
        "review_unavailable",
        "review_retry_limit_exhausted",
    ]
    assert result.ledger[1]["review_error"] == first_findings
    assert result.ledger[2]["review_error"] == [_TRUNCATED, _AFTER_FENCE]
    assert result.ledger[2]["allowance"] == "review_retry"
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert [finding["code"] for finding in evidence["attempts"][2]["findings"]] == [
        "review_truncated",
        "trailing_content",
        "review_retry_limit_exhausted",
    ]
    assert evidence["terminal"] == {
        "stage": "plan",
        "attempt_index": 2,
        "reason": "review_retry_limit_exhausted",
    }


def test_without_a_review_retry_allowance_a_cut_off_reply_ends_the_item(tmp_path: Path) -> None:
    result, client, _ = _run(
        tmp_path, [_plan_reply(), _cut_off(OPEN_FENCE)], plan_max_review_retries=0
    )

    assert result.status == "review_unavailable"
    assert len(client.requests) == 2
    assert result.ledger[1]["review_error"] == [_TRUNCATED, _UNCLOSED]
    assert result.findings[-1].code == "review_retry_limit_exhausted"
