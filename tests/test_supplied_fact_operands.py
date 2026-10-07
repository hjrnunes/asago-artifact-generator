"""A supplied fact operand holds before the run; reviewers must not demand a lookup.

The wording the prompts carry is listed in ``tests/phrases/call1.yaml`` and
``plan_review.yaml``; these tests pin the review question and the field meanings it comes from.
"""

from __future__ import annotations

from asago_artifact_generator.authoring.contracts import PLAN_FIELD_MEANINGS
from asago_artifact_generator.authoring.review import _PLAN_REVIEW_QUESTIONS

_RULE = "established by the supplied inventory before the run"


def test_observability_question_accepts_supplied_fact_operands() -> None:
    question = next(item for item in _PLAN_REVIEW_QUESTIONS if item["id"] == "observability")

    assert _RULE in question["question"]
    assert "earlier captured call" in question["question"]


def test_field_meanings_distinguish_pre_run_facts_from_captured_evidence() -> None:
    assert _RULE in PLAN_FIELD_MEANINGS
