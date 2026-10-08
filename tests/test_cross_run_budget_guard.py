from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring.core import MAX_AUTHORING_REQUESTS
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import (
    AuthoringBudget,
    AuthoringPolicy,
)

from .support import (
    ScriptedAuthoringTransport,
    world_builders,
)

_inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "inventory", "plan", "runtime_contract", "view"
)


def test_author_limit_stops_before_the_fourth_author_request(tmp_path: Path) -> None:

    transport = ScriptedAuthoringTransport(
        [
            b"{}",  # in-run author 1: mechanical plan failure
            json.dumps(_plan()),  # in-run author 2: plan correction
            json.dumps({"decision": "accept", "summary": "routing", "findings": []}),
            b"not a two-block artifact",  # in-run author 3: artifact failure
        ]
    )

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "author-limit",
        task_id="author-limit",
        budget=AuthoringBudget(author_limit=3),
        policy=AuthoringPolicy(),
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "budget_exhausted"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "correction",
        "plan_review",
        "call2",
    ]
    assert [record["role"] for record in result.ledger] == [
        "author",
        "author",
        "reviewer",
        "author",
    ]
    assert result.findings[-1].code == "budget_exhausted"
    assert "author/correction" in result.findings[-1].detail


@pytest.mark.parametrize(
    ("orchestrator_options", "dispatched", "detail_fragment"),
    [
        pytest.param(
            {"budget": AuthoringBudget(author_limit=0)},
            [],
            "author/correction",
            id="author-limit-zero",
        ),
        pytest.param(
            {"budget": AuthoringBudget(review_limit=0)},
            ["call1"],
            "review",
            id="review-limit-zero",
        ),
        pytest.param(
            {
                "budget": AuthoringBudget(
                    aggregate_limit=MAX_AUTHORING_REQUESTS,
                    total_dispatched=MAX_AUTHORING_REQUESTS,
                )
            },
            [],
            "aggregate",
            id="aggregate-exhausted",
        ),
    ],
)
def test_exhausted_budget_stops_before_the_next_dispatch(
    tmp_path: Path,
    orchestrator_options: dict[str, object],
    dispatched: list[str],
    detail_fragment: str,
) -> None:
    transport = ScriptedAuthoringTransport(
        [
            json.dumps(_plan()),
            json.dumps({"decision": "accept", "summary": "unused", "findings": []}),
        ]
    )

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="budget-exhausted",
        policy=AuthoringPolicy(),
        **orchestrator_options,
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "budget_exhausted"
    assert [request["stage"] for request in transport.requests] == dispatched
    assert [record["stage"] for record in result.ledger] == dispatched
    assert [finding.code for finding in result.findings] == ["budget_exhausted"]
    assert detail_fragment in result.findings[0].detail


def test_budget_rejects_malformed_dispatch_counters() -> None:
    cases = [
        ({"dispatched_by_task": []}, "dispatched_by_task must be a mapping"),
        ({"dispatched_by_task_role": []}, "dispatched_by_task_role must be a mapping"),
        ({"dispatched_by_task": {" ": 1}}, "dispatched_by_task keys must be nonblank strings"),
        ({"dispatched_by_task": {3: 1}}, "dispatched_by_task keys must be nonblank strings"),
        (
            {"dispatched_by_task_role": {"": {"author": 1}}},
            "dispatched_by_task_role keys must be nonblank strings",
        ),
        (
            {"dispatched_by_task_role": {"task": 1}},
            "dispatched_by_task_role['task'] must be a mapping",
        ),
        (
            {"dispatched_by_task_role": {"task": {"judge": 1}}},
            "dispatched_by_task_role['task'] has unsupported role 'judge'",
        ),
    ]

    for fields, message in cases:
        with pytest.raises(ValueError) as error:
            AuthoringBudget(**fields)
        assert str(error.value) == message


def test_budget_accepts_well_formed_dispatch_counters() -> None:
    budget = AuthoringBudget(
        total_dispatched=3,
        dispatched_by_task={"task": 3},
        dispatched_by_task_role={"task": {"author": 2, "reviewer": 1}},
    )

    assert budget.dispatched_by_task_role == {"task": {"author": 2, "reviewer": 1}}


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"review_plan": 1}, "review_plan must be a boolean"),
        ({"review_artifact": "yes"}, "review_artifact must be a boolean"),
        ({"review_model_profile": " "}, "review_model_profile must be a nonblank string"),
        ({"review_model_profile": 3}, "review_model_profile must be a nonblank string"),
        ({"plan_max_corrections": True}, "plan_max_corrections must be a nonnegative integer"),
        ({"artifact_max_corrections": -1}, "artifact_max_corrections must be a nonnegative"),
    ],
)
def test_policy_rejects_malformed_fields(fields: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        AuthoringPolicy(**fields)


def test_policy_defaults_unset_corrections_to_one() -> None:
    policy = AuthoringPolicy(artifact_max_corrections=0)

    assert (policy.plan_max_corrections, policy.artifact_max_corrections) == (1, 0)
