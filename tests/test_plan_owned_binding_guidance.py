"""Artifact-stage text states that runtime bindings are plan-owned, for every condition."""

from __future__ import annotations

import tests.test_owner_scope_block as osb
from asago_artifact_generator.authoring import (
    _CURRENT_ARTIFACT_CORRECTION_GUIDANCE,
    _CURRENT_PLAN_AUTHOR_GUIDANCE,
    evidence_packet_contract,
)
from asago_artifact_generator.detector_controls import (
    DETECTOR_FEEDBACK_CORRECTION_GUIDANCE,
    LEGACY_DETECTOR_FEEDBACK_CORRECTION_GUIDANCE,
)
from asago_artifact_generator.detector_runtime import validate_detector_evidence_access

_RULE = "artifact authoring cannot add"
# Advice that only a plan author can follow.
_PLAN_ONLY_ADVICE = (
    "If adding a detector-only binding",
    "When adding a detector-only binding",
    "A detector-only binding should also list",
    "declare a runtime binding named",
    "If this is a detector-only binding",
    "For a newly added detector-only binding",
)
_BINDINGS = [
    {
        "name": "caller_id",
        "source_kind": "supplied_input",
        "source_ref": "facts:state:caller_id",
        "selector": "value",
        "consumers": ["detector.caller_id"],
    }
]


def _plan_only_advice(text: str) -> list[str]:
    return [phrase for phrase in _PLAN_ONLY_ADVICE if phrase in text]


def test_artifact_guidance_constants_state_the_plan_owned_rule() -> None:
    binding_rule = evidence_packet_contract()["detector_access"]["binding_rule"]

    for text in (
        _CURRENT_ARTIFACT_CORRECTION_GUIDANCE,
        DETECTOR_FEEDBACK_CORRECTION_GUIDANCE,
        binding_rule,
    ):
        assert _RULE in text
        assert _plan_only_advice(text) == []


def test_artifact_stage_prompts_state_the_rule_and_the_plan_author_keeps_its_own() -> None:
    packets = osb._render_all_stage_packets(osb._view())

    for stage in ("call2", "artifact_review", "artifact_correction"):
        text = packets[stage].system + packets[stage].user
        # The reviewer reads the accepted plan as fixed and never gets binding advice.
        if stage != "artifact_review":
            assert _RULE in text, stage
        assert _plan_only_advice(text) == [], stage
    assert "list its detector.<binding_name> consumer" in _CURRENT_PLAN_AUTHOR_GUIDANCE
    assert "list its detector.<binding_name> consumer" in packets["call1"].user


def test_undeclared_access_findings_state_the_rule_without_a_condition() -> None:
    binding = b"def evaluate(evidence):\n    return evidence['bindings']['record']\n"
    root = b"def evaluate(evidence):\n    return evidence['state']\n"

    for source, name in ((binding, "'record'"), (root, "'state'")):
        [finding] = validate_detector_evidence_access(
            source, bindings=_BINDINGS, judge_enabled=False
        )
        assert finding["code"] == "undeclared_evidence_access"
        assert name in finding["detail"]
        assert _RULE in finding["detail"]
        assert "caller_id" in finding["detail"]
        assert _plan_only_advice(finding["detail"]) == []


def test_legacy_guidance_is_unchanged() -> None:
    assert "detector_access" not in evidence_packet_contract(legacy=True)
    assert _RULE not in LEGACY_DETECTOR_FEEDBACK_CORRECTION_GUIDANCE
