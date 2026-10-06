from __future__ import annotations

import pytest

from asago_artifact_generator.authoring import orchestrator
from asago_artifact_generator.authoring.checks import (
    collect_artifact_findings_v2,
    collect_plan_findings,
    collect_plan_findings_v2,
)
from asago_artifact_generator.authoring.core import (
    FINDING_STAGE_KEYS,
    Finding,
    FindingTarget,
    PromptOverflowError,
    PromptPreflightError,
    _prompt_preflight_finding,
)
from asago_artifact_generator.authoring.review import _review_finding_to_finding


@pytest.mark.parametrize(
    ("path", "parts", "qualifier"),
    [
        ("runtime_bindings[3].selector", ("runtime_bindings", 3, "selector"), None),
        (
            "prerequisites[0].evidence_refs[12]",
            ("prerequisites", 0, "evidence_refs", 12),
            None,
        ),
        ("stimulus.user_text:item_id", ("stimulus", "user_text"), "item_id"),
        ("stimulus_approach.request:a:b", ("stimulus_approach", "request"), "a:b"),
        (
            "detector_controls.judge-content-supported",
            ("detector_controls", "judge-content-supported"),
            None,
        ),
        ("call1", ("call1",), None),
    ],
)
def test_finding_target_parses_a_path_and_renders_it_back(
    path: str, parts: tuple[str | int, ...], qualifier: str | None
) -> None:
    target = FindingTarget.parse(path)

    assert target == FindingTarget(parts, qualifier)
    assert target.render() == path
    assert Finding("code", "detail", path).target == target


@pytest.mark.parametrize(
    "path", ["", "[0]", "a..b", "a.", ".a", "a[01]", "a[x]", "a]", "a:", ":a", "a[0]b"]
)
def test_finding_target_rejects_a_path_it_cannot_render_back(path: str) -> None:
    assert FindingTarget.parse(path) is None
    assert Finding("code", "detail", path).target is None


def test_finding_stage_stays_out_of_equality_repr_and_serialization() -> None:
    plain = Finding("code", "detail", "plan", {"k": 1})
    staged = Finding("code", "detail", "plan", {"k": 1}, stage="plan")

    assert staged == plain
    assert hash(staged) == hash(plain)
    assert repr(staged) == repr(plain)
    assert staged.to_dict() == plain.to_dict()
    assert staged.stage == "plan"
    assert plain.stage is None


def test_finding_stage_keys_match_the_orchestrator_stage_table() -> None:
    assert dict(FINDING_STAGE_KEYS) == orchestrator._FINDING_PATH_STAGES


def test_check_collectors_mark_their_findings_with_the_stage() -> None:
    plan_findings = collect_plan_findings({}, {}, {})
    v2_findings = collect_plan_findings_v2({}, {}, {})
    artifact_findings = collect_artifact_findings_v2({}, {}, {}, {})

    assert plan_findings
    assert v2_findings
    assert artifact_findings
    assert {finding.stage for finding in plan_findings} == {"plan"}
    assert {finding.stage for finding in v2_findings} == {"plan"}
    assert {finding.stage for finding in artifact_findings} == {"artifact"}
    assert collect_plan_findings("plan", {}, {})[0].stage == "plan"


@pytest.mark.parametrize("stage", ["plan", "artifact"])
def test_review_findings_carry_their_stage(stage: str) -> None:
    finding = _review_finding_to_finding({"location": "x", "required_change": "y"}, stage)

    assert finding.path == stage
    assert finding.stage == stage


@pytest.mark.parametrize(
    ("packet_stage", "stage"),
    [("call1", "plan"), ("plan_review", "plan"), ("call2", "artifact"), ("correction", None)],
)
def test_prompt_preflight_findings_carry_the_logical_stage(
    packet_stage: str, stage: str | None
) -> None:
    for exc in (PromptPreflightError("too big"), PromptOverflowError("too big")):
        finding = _prompt_preflight_finding(exc, packet_stage)

        assert finding.path == packet_stage
        assert finding.stage == stage
