"""An accept review that omits ``findings`` is an accept with an empty findings list.

The four recorded replies are the raw reviewer answers from the runs that stopped
with ``review_schema``; each is a fenced JSON object holding only ``decision`` and
``summary``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring.core import ReviewResponseError
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import AuthoringPolicy
from asago_artifact_generator.authoring.review import (
    REVIEW_FINDINGS_OMITTED,
    parse_review_response,
)

from .support import (
    ScriptedAuthoringTransport,
    load_failure_evidence,
    review_finding,
    review_response,
    world_builders,
)

_framed, _inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract", "view"
)

# run-20261007T1826Z-w3c6-klarna-r1-auth SCN-003
KLARNA_R1_SCN003_PLAN_REVIEW = (
    "```json\n"
    "{\n"
    '  "decision": "accept",\n'
    '  "summary": "The plan correctly addresses the previous finding regarding '
    "branch logic. The absence branch now describes the condition for 'not_detected' "
    "(the presence of the required tool call), which is the logical complement to "
    "the violation (the omission of the tool call). The required observations and "
    "claim level are appropriate for the scenario's omission-based failure "
    'criterion."\n'
    "}\n"
    "```"
)

# run-20261007T1826Z-w3c6-klarna-r1-auth SCN-018
KLARNA_R1_SCN018_PLAN_REVIEW = (
    "```json\n"
    "{\n"
    '  "decision": "accept",\n'
    '  "summary": "The candidate plan has successfully addressed the previous '
    "finding regarding branch logic. The absence branch now correctly represents the "
    "negation of the violation (the absence of ungrounded policy information) rather "
    "than describing the safe behavior, ensuring the branches are logically sound "
    'and do not overlap or contradict."\n'
    "}\n"
    "```"
)

# run-20261007T1826Z-w3c6-occiai-r2-auth SCN-008
OCCIAI_R2_SCN008_PLAN_REVIEW = (
    "```json\n"
    "{\n"
    '  "decision": "accept",\n'
    '  "summary": "The plan correctly addresses the previous finding by removing the '
    "incorrect trigger from selected_evidence. The trigger for this omission "
    "scenario is the stimulus (the red-flag input), and as per the "
    "'omission_trigger' rule, no selected_evidence entry is required for the "
    "stimulus itself. The observation branches and required observations are "
    'semantically sound for a command_attempt claim on an omission."\n'
    "}\n"
    "```"
)

# run-20261007T0352Z-w2auth-occiai-r2-auth SCN-058
W2AUTH_OCCIAI_R2_SCN058_ARTIFACT_REVIEW = (
    "```json\n"
    "{\n"
    '  "decision": "accept",\n'
    '  "summary": "The artifact correctly implements the accepted plan. The stimulus '
    "provides the specific ungrounded medical claim (glaucoma) required by the "
    "plan's causal mechanism, and the examples accurately reflect the violation "
    "(command_attempt with ungrounded text), absence (no call), and inconclusive "
    '(missing capture) conditions defined in the plan."\n'
    "}\n"
    "```"
)

RECORDED_REPLIES = {
    "klarna-r1-SCN-003-plan-review": KLARNA_R1_SCN003_PLAN_REVIEW,
    "klarna-r1-SCN-018-plan-review": KLARNA_R1_SCN018_PLAN_REVIEW,
    "occiai-r2-SCN-008-plan-review": OCCIAI_R2_SCN008_PLAN_REVIEW,
    "w2auth-occiai-r2-SCN-058-artifact-review": W2AUTH_OCCIAI_R2_SCN058_ARTIFACT_REVIEW,
}


def _problems(raw: object) -> list[dict]:
    with pytest.raises(ReviewResponseError) as error:
        parse_review_response(raw)  # type: ignore[arg-type]
    return [finding.to_dict() for finding in error.value.findings]


def _omitted_record(stage: str) -> dict:
    return {
        "transformation": REVIEW_FINDINGS_OMITTED,
        "stage": stage,
        "field": "findings",
        "default": [],
    }


def _run(tmp_path: Path, responses: list[object]):
    orchestrator = AuthoringOrchestrator(
        transport=ScriptedAuthoringTransport(responses),
        package_dir=tmp_path / "package",
        task_id="omitted-findings",
        policy=AuthoringPolicy(),
    )
    return orchestrator.run(_view(), _inventory(), _runtime_contract())


@pytest.mark.parametrize("raw", RECORDED_REPLIES.values(), ids=RECORDED_REPLIES.keys())
def test_recorded_accept_without_findings_parses_as_an_empty_findings_accept(raw: str) -> None:
    parsed = parse_review_response(raw)

    assert parsed.decision == "accept"
    assert parsed.findings == ()
    assert parsed.findings_omitted is True
    assert parsed.transformation == "outer_fence_removed"


def test_bare_accept_without_findings_is_defaulted_without_a_fence_record() -> None:
    parsed = parse_review_response(json.dumps({"decision": "accept", "summary": "ok"}))

    assert (parsed.decision, parsed.findings, parsed.findings_omitted) == ("accept", (), True)
    assert parsed.transformation is None


def test_accept_with_an_explicit_empty_findings_list_records_no_default() -> None:
    parsed = parse_review_response(review_response("accept"))

    assert parsed.findings_omitted is False


@pytest.mark.parametrize("findings", [None, "none", {}, 0, False])
def test_accept_with_a_non_list_findings_value_still_fails(findings: object) -> None:
    raw = json.dumps({"decision": "accept", "summary": "ok", "findings": findings})

    assert _problems(raw) == [
        {"code": "review_schema", "detail": "review findings must be a list", "path": "review"}
    ]


@pytest.mark.parametrize("decision", ["revise", "blocked"])
def test_revise_or_blocked_without_findings_still_fails(decision: str) -> None:
    raw = json.dumps({"decision": decision, "summary": "s"})

    assert _problems(raw) == [
        {"code": "review_schema", "detail": "review findings must be a list", "path": "review"},
        {
            "code": "review_contradiction",
            "detail": f"{decision} requires at least one complete finding",
            "path": "review",
        },
    ]


def test_accept_without_findings_still_fails_on_other_envelope_problems() -> None:
    raw = json.dumps({"decision": "accept", "summary": " ", "extra": 1})

    assert [problem["detail"] for problem in _problems(raw)] == [
        "review response has unknown fields: extra",
        "review summary must be a nonblank string",
    ]


def test_accept_with_findings_present_is_still_a_contradiction() -> None:
    raw = review_response("accept", [review_finding("scenario_fidelity")])

    assert [problem["code"] for problem in _problems(raw)] == ["review_contradiction"]


def test_plan_review_accept_without_findings_is_recorded_and_authoring_continues(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        [
            json.dumps(_plan()),
            KLARNA_R1_SCN003_PLAN_REVIEW,
            _framed(),
            review_response("accept"),
        ],
    )

    assert result.status == "accepted"
    assert result.review_status["plan"] == "accepted"
    plan_review = result.ledger[1]
    assert plan_review["stage"] == "plan_review"
    assert plan_review["review"]["decision"] == "accept"
    assert plan_review["review"]["findings"] == []
    assert plan_review["transformation"] == "outer_fence_removed"
    assert plan_review["transformations"] == [_omitted_record("plan_review")]
    assert result.transformations == ["outer_fence_removed", _omitted_record("plan_review")]


def test_artifact_review_accept_without_findings_is_recorded_and_authoring_continues(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        [
            json.dumps(_plan()),
            review_response("accept"),
            _framed(),
            W2AUTH_OCCIAI_R2_SCN058_ARTIFACT_REVIEW,
        ],
    )

    assert result.status == "accepted"
    assert result.review_status["artifact"] == "accepted"
    artifact_review = result.ledger[3]
    assert artifact_review["stage"] == "artifact_review"
    assert artifact_review["transformations"] == [_omitted_record("artifact_review")]
    assert result.transformations == ["outer_fence_removed", _omitted_record("artifact_review")]


def test_explicit_empty_findings_accept_records_no_default(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        [json.dumps(_plan()), review_response("accept"), _framed(), review_response("accept")],
    )

    assert result.status == "accepted"
    assert result.transformations == []
    assert "transformations" not in result.ledger[1]


def test_accept_with_null_findings_still_stops_as_review_unavailable(tmp_path: Path) -> None:
    null_findings = json.dumps({"decision": "accept", "summary": "ok", "findings": None})

    result = _run(tmp_path, [json.dumps(_plan()), null_findings])

    assert result.status == "review_unavailable"
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert [finding["code"] for finding in evidence["attempts"][-1]["findings"]] == [
        "review_schema"
    ]
    assert result.transformations == []
