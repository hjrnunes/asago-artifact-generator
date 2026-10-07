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
from tests import test_semantic_judge_spec_wording as judged
from tests import test_versioned_prompt_roles as unjudged


def _guarantees(checks: tuple[Any, ...]) -> list[str]:
    return [check.guarantee for check in checks]


def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    return build_plan_reviewer_context(
        unjudged._view(), plan, unjudged._inventory(), unjudged._runtime_contract()
    )["mechanical_check_summary"]


def test_plan_summary_passes_and_lists_every_check_for_a_plan_the_checks_accept() -> None:
    summary = _plan_summary(unjudged._plan())

    assert summary["status"] == "passed"
    assert summary["checks"] == _guarantees(PLAN_MECHANICAL_CHECKS)


def test_plan_summary_does_not_pass_a_plan_the_checks_reject() -> None:
    plan = unjudged._plan()
    plan["unexpected_root_field"] = "x"

    summary = _plan_summary(plan)

    assert summary["status"] == "failed"
    assert summary["checks"] == []


def test_artifact_summary_omits_the_judge_fact_check_when_the_artifact_has_no_judge() -> None:
    context = build_artifact_reviewer_context(
        unjudged._view(),
        unjudged._plan(),
        unjudged._metadata(),
        unjudged._inventory(),
        unjudged._runtime_contract(),
    )

    summary = context["mechanical_check_summary"]
    assert summary["status"] == "passed"
    assert summary["checks"] == _guarantees(ARTIFACT_MECHANICAL_CHECKS[:2])


def test_artifact_summary_lists_the_judge_fact_check_when_the_artifact_has_a_judge() -> None:
    context = build_artifact_reviewer_context(
        judged._view(),
        judged._plan(),
        judged._metadata("Does the reply assert the claim?"),
        judged._inventory(),
        judged._runtime_contract(),
    )

    summary = context["mechanical_check_summary"]
    assert summary["status"] == "passed"
    assert summary["checks"] == _guarantees(ARTIFACT_MECHANICAL_CHECKS)


def test_artifact_summary_does_not_pass_an_artifact_the_checks_reject() -> None:
    metadata = deepcopy(unjudged._metadata())
    metadata["semantic_judge_spec"] = {"question": "Not a question."}

    context = build_artifact_reviewer_context(
        unjudged._view(),
        unjudged._plan(),
        metadata,
        unjudged._inventory(),
        unjudged._runtime_contract(),
    )

    assert context["mechanical_check_summary"]["status"] == "failed"
    assert context["mechanical_check_summary"]["checks"] == []
