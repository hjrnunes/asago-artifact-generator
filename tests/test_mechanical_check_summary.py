from __future__ import annotations

from copy import deepcopy
from typing import Any

from asago_artifact_generator.authoring.checks import (
    ARTIFACT_MECHANICAL_CHECKS,
    PLAN_MECHANICAL_CHECKS,
)
from asago_artifact_generator.authoring.review import (
    build_artifact_reviewer_context,
    build_plan_reviewer_context,
)
from tests.support import world_builders

from .reply_support import (
    reply_inventory,
    reply_metadata,
    reply_plan,
    reply_runtime_contract,
    reply_view,
)

_view, _inventory, _runtime_contract, _plan, _metadata = world_builders(
    "ehr", "view", "inventory", "runtime_contract", "plan", "metadata"
)


def _guarantees(checks: tuple[Any, ...]) -> list[str]:
    return [check.guarantee for check in checks]


def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    return build_plan_reviewer_context(_view(), plan, _inventory(), _runtime_contract())[
        "mechanical_check_summary"
    ]


def test_plan_summary_passes_and_lists_every_check_for_a_plan_the_checks_accept() -> None:
    summary = _plan_summary(_plan())

    assert summary["status"] == "passed"
    assert summary["checks"] == _guarantees(PLAN_MECHANICAL_CHECKS)


def test_plan_summary_does_not_pass_a_plan_the_checks_reject() -> None:
    plan = _plan()
    plan["unexpected_root_field"] = "x"

    summary = _plan_summary(plan)

    assert summary["status"] == "failed"
    assert summary["checks"] == []


def _condition_consistent_metadata() -> dict[str, Any]:
    """Return an artifact whose captures agree with the handoff's tool-call condition."""

    metadata = _metadata()
    for label, patient in (("unsafe", "P-1"), ("safe", "P-2")):
        call = {"operation": "summarize_for_ehr", "arguments": {"patient_id": patient}}
        metadata["examples"][label]["capture"] = {"tool_calls": [call], "complete": True}
    return metadata


def test_artifact_summary_omits_the_judge_fact_check_when_the_artifact_has_no_judge() -> None:
    context = build_artifact_reviewer_context(
        _view(),
        _plan(),
        _condition_consistent_metadata(),
        _inventory(),
        _runtime_contract(),
    )

    summary = context["mechanical_check_summary"]
    assert summary["status"] == "passed"
    assert summary["checks"] == _guarantees(ARTIFACT_MECHANICAL_CHECKS[:2])


def test_artifact_summary_lists_the_judge_fact_check_when_the_artifact_has_a_judge() -> None:
    context = build_artifact_reviewer_context(
        reply_view(),
        reply_plan(),
        reply_metadata("Does the reply assert the claim?"),
        reply_inventory(),
        reply_runtime_contract(),
    )

    summary = context["mechanical_check_summary"]
    assert summary["status"] == "passed"
    assert summary["checks"] == _guarantees(ARTIFACT_MECHANICAL_CHECKS)


def test_artifact_summary_does_not_pass_an_artifact_the_checks_reject() -> None:
    metadata = deepcopy(_metadata())
    metadata["semantic_judge_spec"] = {"question": "Not a question."}

    context = build_artifact_reviewer_context(
        _view(),
        _plan(),
        metadata,
        _inventory(),
        _runtime_contract(),
    )

    assert context["mechanical_check_summary"]["status"] == "failed"
    assert context["mechanical_check_summary"]["checks"] == []
