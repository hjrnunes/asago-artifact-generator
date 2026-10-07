"""Artifact-stage text states that runtime bindings are plan-owned, for every condition."""

from __future__ import annotations

from asago_artifact_generator.authoring.correction import _CURRENT_ARTIFACT_CORRECTION_GUIDANCE
from asago_artifact_generator.authoring.prompt_context import _CURRENT_PLAN_AUTHOR_GUIDANCE

from .prompt_support import render_stage_packets

_RULE = "artifact authoring cannot add"
# Advice that only a plan author can follow.
_PLAN_ONLY_ADVICE = (
    "If adding a detector-only binding",
    "When adding a detector-only binding",
    "A detector-only binding should also list",
    "declare a runtime binding named",
    "If this is a detector-only binding",
    "For a newly added detector-only binding",
    "If adding a judge-only binding",
    "When adding a judge-only binding",
    "A judge-only binding should also list",
    "If this is a judge-only binding",
    "For a newly added judge-only binding",
)


def _plan_only_advice(text: str) -> list[str]:
    return [phrase for phrase in _PLAN_ONLY_ADVICE if phrase in text]


def test_artifact_guidance_constants_state_the_plan_owned_rule() -> None:
    assert _RULE in _CURRENT_ARTIFACT_CORRECTION_GUIDANCE
    assert _plan_only_advice(_CURRENT_ARTIFACT_CORRECTION_GUIDANCE) == []


def test_artifact_stage_prompts_state_the_rule_and_the_plan_author_keeps_its_own() -> None:
    packets = render_stage_packets()

    for stage in ("call2", "artifact_review", "artifact_correction"):
        text = packets[stage].system + packets[stage].user
        # The reviewer reads the accepted plan as fixed and never gets binding advice.
        if stage != "artifact_review":
            assert _RULE in text, stage
        assert _plan_only_advice(text) == [], stage
    assert "List judge.<binding_name> only for a value" in _CURRENT_PLAN_AUTHOR_GUIDANCE
    assert "List judge.<binding_name> only for a value" in packets["call1"].user
