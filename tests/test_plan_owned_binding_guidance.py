"""Artifact-stage text states that runtime bindings are plan-owned, for every condition."""

from __future__ import annotations

from asago_artifact_generator.authoring.correction import _CURRENT_ARTIFACT_CORRECTION_GUIDANCE

from .prompt_support import render_stage_packets

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


def test_artifact_correction_guidance_gives_no_plan_only_binding_advice() -> None:
    assert _plan_only_advice(_CURRENT_ARTIFACT_CORRECTION_GUIDANCE) == []


def test_artifact_stage_prompts_give_no_plan_only_binding_advice() -> None:
    packets = render_stage_packets()

    for stage in ("call2", "artifact_review", "artifact_correction"):
        text = packets[stage].system + packets[stage].user
        # The reviewer reads the accepted plan as fixed and never gets binding advice.
        assert _plan_only_advice(text) == [], stage
