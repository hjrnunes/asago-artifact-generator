"""A review response that fails its checks asks the review again within the stage allowance."""

from __future__ import annotations

import base64
import json
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.core import PromptPacket
from asago_artifact_generator.authoring.journal import RunFindingRecorded
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import AuthoringBudget, AuthoringPolicy
from asago_artifact_generator.failure_evidence import failure_evidence_path

from .prompt_support import REJECTED_ARTIFACT_REVIEW, REJECTED_PLAN_REVIEW, rejected_review
from .support import (
    ScriptedAuthoringTransport,
    chat_completion,
    load_failure_evidence,
    private_transport,
    review_response,
    scripted_client,
    scripted_orchestrator,
    world_builders,
)

_framed, _inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract", "view"
)

_orchestrator = partial(scripted_orchestrator, task_id="review-retry")

_MISSING_REQUIRED_CHANGE = (
    "review.findings[0] must have exactly question, location, problem, basis, and "
    "required_change (missing=['required_change'], unknown=[])"
)


def _run(tmp_path: Path, responses: list[Any], **policy: Any) -> tuple[Any, Any]:
    orchestrator, transport = _orchestrator(tmp_path, responses, policy=AuthoringPolicy(**policy))
    return orchestrator.run(_view(), _inventory(), _runtime_contract()), transport


def _raw(attempt: dict[str, Any]) -> bytes:
    return base64.b64decode(attempt["raw_response"]["base64"])


def test_a_plan_review_missing_a_required_part_is_asked_again_and_proceeds(
    tmp_path: Path,
) -> None:
    result, transport = _run(
        tmp_path,
        [
            json.dumps(_plan()),
            REJECTED_PLAN_REVIEW,
            review_response(),
            _framed(),
            review_response(),
        ],
    )

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "plan_review",
        "call2",
        "artifact_review",
    ]
    assert result.review_status == {"plan": "accepted", "artifact": "accepted"}
    assert result.review_retry_allowances == {"plan": 0, "artifact": 1}
    rejected, retry = result.ledger[1], result.ledger[2]
    assert rejected["review_error"] == [
        {"code": "review_schema", "detail": _MISSING_REQUIRED_CHANGE, "path": "review.findings[0]"}
    ]
    assert rejected["failure"]["code"] == "review_unavailable"
    assert result.raw_responses[rejected["raw_response_key"]] == REJECTED_PLAN_REVIEW
    assert "allowance" not in rejected
    assert retry["allowance"] == "review_retry"
    assert retry["retries_dispatch"] == rejected["dispatch_index"]
    assert retry["review"]["status"] == "accepted"


def test_the_retry_prompt_carries_the_rejected_response_and_its_findings_verbatim(
    tmp_path: Path,
) -> None:
    _, transport = _run(
        tmp_path,
        [
            json.dumps(_plan()),
            REJECTED_PLAN_REVIEW,
            review_response(),
            _framed(),
            review_response(),
        ],
    )

    first, retry = transport.requests[1], transport.requests[2]
    assert "REJECTED REVIEW RESPONSE" not in first["user"]
    assert "rejected_review_response" not in first["payload"]
    assert retry["system"] == first["system"]
    assert retry["user"].startswith(first["user"].rstrip("\n"))
    assert f"REJECTED REVIEW RESPONSE\n{REJECTED_PLAN_REVIEW.decode()}\n" in retry["user"]
    rejected = retry["payload"]["rejected_review_response"]
    assert rejected["response"] == REJECTED_PLAN_REVIEW.decode()
    assert rejected["response_encoding"] == "utf-8-exact"
    assert rejected["findings"] == [
        finding.to_dict() for finding in rejected_review(REJECTED_PLAN_REVIEW).findings
    ]
    assert _MISSING_REQUIRED_CHANGE in retry["user"]


def test_a_review_that_is_not_utf8_is_shown_with_replacements_and_kept_exactly(
    tmp_path: Path,
) -> None:
    not_utf8 = b'{"decision": "accept", "summary": "ok\xff", "findings": []}'
    result, transport = _run(
        tmp_path,
        [json.dumps(_plan()), not_utf8, review_response(), _framed(), review_response()],
    )

    assert result.status == "accepted"
    rejected = transport.requests[2]["payload"]["rejected_review_response"]
    assert rejected["response"] == not_utf8.decode("utf-8", errors="replace")
    assert "\ufffd" in rejected["response"]
    assert rejected["response_encoding"] == "utf-8-replacement-inexact"
    assert result.raw_responses[result.ledger[1]["raw_response_key"]] == not_utf8


def test_an_artifact_review_that_is_not_json_is_asked_again_and_proceeds(tmp_path: Path) -> None:
    result, transport = _run(
        tmp_path,
        [
            json.dumps(_plan()),
            review_response(),
            _framed(),
            REJECTED_ARTIFACT_REVIEW,
            review_response(),
        ],
    )

    assert result.status == "accepted"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "call2",
        "artifact_review",
        "artifact_review",
    ]
    assert result.review_retry_allowances == {"plan": 1, "artifact": 0}
    assert "REJECTED REVIEW RESPONSE" in transport.requests[4]["user"]
    assert result.ledger[4]["retries_dispatch"] == 4


@pytest.mark.parametrize("retries", [0, 1, 2])
def test_repeated_malformed_reviews_stop_at_the_bound_with_the_reason(
    tmp_path: Path, retries: int
) -> None:
    result, transport = _run(
        tmp_path,
        [json.dumps(_plan()), *[REJECTED_PLAN_REVIEW] * (retries + 1)],
        plan_max_review_retries=retries,
    )

    assert result.status == "review_unavailable"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        *["plan_review"] * (retries + 1),
    ]
    assert result.review_status["plan"] == "unavailable"
    assert result.review_retry_allowances == {"plan": 0, "artifact": 1}
    assert [finding.code for finding in result.findings] == [
        "review_unavailable",
        "review_retry_limit_exhausted",
    ]
    evidence = load_failure_evidence(result.failure_evidence_path)
    reviews = evidence["attempts"][1:]
    assert [_raw(attempt) for attempt in reviews] == [REJECTED_PLAN_REVIEW] * (retries + 1)
    for attempt in reviews[:-1]:
        assert attempt["findings"] == [
            {
                "code": "review_schema",
                "detail": _MISSING_REQUIRED_CHANGE,
                "path": "review.findings[0]",
                "stage": "plan",
            }
        ]
        assert attempt["failure"]["detail"] == _MISSING_REQUIRED_CHANGE
    assert [finding["code"] for finding in reviews[-1]["findings"]] == [
        "review_schema",
        "review_retry_limit_exhausted",
    ]
    assert reviews[-1]["findings"][-1]["detail"] == "plan review retry allowance is exhausted"
    assert [attempt.get("retries_dispatch") for attempt in reviews] == [
        None,
        *range(2, retries + 2),
    ]
    assert evidence["review_retry_allowances"] == {"plan": 0, "artifact": 1}
    assert evidence["policy"]["plan_max_review_retries"] == retries
    assert evidence["terminal"] == {
        "stage": "plan",
        "attempt_index": retries + 1,
        "reason": "review_retry_limit_exhausted",
    }


def test_an_unreviewed_stage_has_no_review_retries() -> None:
    policy = AuthoringPolicy(review_plan=False)

    assert policy.plan_review_retries == 0
    assert policy.artifact_review_retries == 1


@pytest.mark.parametrize("field_name", ["plan_max_review_retries", "artifact_max_review_retries"])
@pytest.mark.parametrize("value", [True, -1, 1.5, "1"])
def test_review_retry_allowances_validate_strictly(field_name: str, value: Any) -> None:
    with pytest.raises(ValueError, match=field_name):
        AuthoringPolicy(**{field_name: value})


def _private_run(
    tmp_path: Path, contents: list[bytes], budget: AuthoringBudget
) -> tuple[Any, Any]:
    client = scripted_client([chat_completion(content.decode()) for content in contents])
    orchestrator = AuthoringOrchestrator(
        transport=private_transport(client=client),
        package_dir=tmp_path / "package",
        task_id="review-retry",
        budget=budget,
        policy=AuthoringPolicy(review_artifact=False),
    )
    return orchestrator.run(_view(), _inventory(), _runtime_contract()), client


def test_the_retry_reserves_budget_through_the_request_boundary(tmp_path: Path) -> None:
    budget = AuthoringBudget(aggregate_limit=10, task_limit=10, author_limit=10, review_limit=10)

    result, client = _private_run(
        tmp_path,
        [json.dumps(_plan()).encode(), REJECTED_PLAN_REVIEW, review_response(), _framed()],
        budget,
    )

    assert result.status == "accepted"
    assert len(client.requests) == 4
    assert budget.dispatched_by_task_role["review-retry"] == {"author": 2, "reviewer": 2}
    assert [record["stage"] for record in result.ledger] == [
        "call1",
        "plan_review",
        "plan_review",
        "call2",
    ]
    assert result.ledger[2]["allowance"] == "review_retry"
    assert result.budget["review_spent"] == 2


def test_an_exhausted_review_budget_stops_the_retry_before_it_is_sent(tmp_path: Path) -> None:
    budget = AuthoringBudget(aggregate_limit=10, task_limit=10, author_limit=10, review_limit=1)

    result, client = _private_run(
        tmp_path,
        [json.dumps(_plan()).encode(), REJECTED_PLAN_REVIEW, review_response()],
        budget,
    )

    assert result.status == "budget_exhausted"
    assert len(client.requests) == 2
    assert [record["stage"] for record in result.ledger] == ["call1", "plan_review"]
    assert result.ledger[1]["review_error"][0]["detail"] == _MISSING_REQUIRED_CHANGE
    assert result.budget["review_spent"] == 1
    assert result.findings[-1].code == "budget_exhausted"
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert _raw(evidence["attempts"][1]) == REJECTED_PLAN_REVIEW
    assert evidence["attempts"][1]["failure"]["code"] == "review_unavailable"


def test_a_retry_the_budget_refuses_leaves_the_retry_allowance_unspent(tmp_path: Path) -> None:
    budget = AuthoringBudget(aggregate_limit=10, task_limit=10, author_limit=10, review_limit=1)

    result, client = _private_run(
        tmp_path,
        [json.dumps(_plan()).encode(), REJECTED_PLAN_REVIEW, review_response()],
        budget,
    )

    assert result.status == "budget_exhausted"
    assert len(client.requests) == 2
    assert result.review_retry_allowances == {"plan": 1, "artifact": 0}
    assert all("allowance" not in record for record in result.ledger)
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["review_retry_allowances"] == {"plan": 1, "artifact": 0}
    assert [attempt["stage"] for attempt in evidence["attempts"]] == ["call1", "plan_review"]
    assert all("retries_dispatch" not in attempt for attempt in evidence["attempts"])


def test_a_retry_sent_and_failed_in_transport_spends_the_retry_allowance(
    tmp_path: Path,
) -> None:
    result, transport = _run(
        tmp_path,
        [json.dumps(_plan()), REJECTED_PLAN_REVIEW, RuntimeError("connection reset")],
    )

    assert result.status == "review_unavailable"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "plan_review",
        "plan_review",
    ]
    assert result.review_retry_allowances == {"plan": 0, "artifact": 1}
    assert result.ledger[2]["allowance"] == "review_retry"
    assert result.ledger[2]["retries_dispatch"] == 2


class _EvidenceAtRequest(ScriptedAuthoringTransport):
    """Scripted transport that reads the failure-evidence sidecar as each request is sent."""

    def __init__(self, responses: list[Any], evidence_path: Path) -> None:
        super().__init__(responses)
        self.evidence_path = evidence_path
        self.evidence: list[dict[str, Any] | None] = []

    def complete(self, packet: PromptPacket) -> Any:
        self.evidence.append(
            load_failure_evidence(self.evidence_path) if self.evidence_path.exists() else None
        )
        return super().complete(packet)


def test_a_rejected_review_the_retry_recovers_from_leaves_no_run_finding(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    transport = _EvidenceAtRequest(
        [
            json.dumps(_plan()),
            REJECTED_PLAN_REVIEW,
            review_response(),
            _framed(),
            review_response(),
        ],
        failure_evidence_path(package_dir),
    )
    orchestrator = AuthoringOrchestrator(
        transport=transport,
        package_dir=package_dir,
        task_id="review-retry",
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    during_retry = transport.evidence[2]
    assert during_retry is not None
    assert "review_unavailable" not in [finding["code"] for finding in during_retry["findings"]]
    rejected = during_retry["attempts"][1]
    assert rejected["failure"]["code"] == "review_unavailable"
    assert [finding["code"] for finding in rejected["findings"]] == ["review_schema"]
    assert not any(
        isinstance(event, RunFindingRecorded) and event.finding.code == "review_unavailable"
        for event in orchestrator._journal.events
    )
    assert "review_unavailable" not in [finding.code for finding in result.findings]


def test_an_exhausted_review_retry_still_records_the_run_finding(tmp_path: Path) -> None:
    orchestrator, _ = _orchestrator(
        tmp_path,
        [json.dumps(_plan()), REJECTED_PLAN_REVIEW, REJECTED_PLAN_REVIEW],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "review_unavailable"
    run_findings = [
        event.finding.code
        for event in orchestrator._journal.events
        if isinstance(event, RunFindingRecorded)
    ]
    assert run_findings == ["review_unavailable"]
