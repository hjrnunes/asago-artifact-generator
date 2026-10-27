"""A later plan review sees the current value at each earlier finding's location.

``fixtures/review_rounds/recorded-repeats.json`` holds recorded multi-round plan reviews,
minimised to the value each round's plan held at the finding's path and each round's review
reply. In each case a later round asked again for a change the revised plan already held.

Code sets aside a plan-review finding only when an earlier review already judged its exact
request met on the current value; the recorded repeats all repeat the round just before, so
they are kept, and the set-aside cases below build on a recorded finding.
"""

from __future__ import annotations

import copy
import json
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.policy import AuthoringPolicy
from asago_artifact_generator.authoring.review import (
    REVIEW_FINDING_REPEATS_MET_CHANGE,
    PriorReviewRound,
    build_plan_review_packet,
    build_plan_reviewer_context,
    finding_plan_path,
    set_aside_met_repeats,
)

from .support import review_response, scripted_orchestrator, world_builders

_RECORDED = json.loads(
    (Path(__file__).parent / "fixtures" / "review_rounds" / "recorded-repeats.json").read_text(
        encoding="utf-8"
    )
)["cases"]

_framed, _inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract", "view"
)

_orchestrator = partial(scripted_orchestrator, task_id="repeated-findings")


def _plan_with(plan_path: str, value: Any, plan: dict | None = None) -> dict:
    """Return the refund plan (or ``plan``) with ``value`` at ``plan_path``."""

    changed = copy.deepcopy(_plan() if plan is None else plan)
    path = finding_plan_path(plan_path)
    assert path is not None
    *parents, last = path
    target: Any = changed
    for step in parents:
        target = target[step]
    target[last] = copy.deepcopy(value)
    return changed


def _recorded_rounds(case: str) -> tuple[str, list[dict]]:
    return _RECORDED[case]["plan_path"], _RECORDED[case]["rounds"]


def _round(plan: dict, findings: list[dict], **earlier: Any) -> PriorReviewRound:
    return PriorReviewRound(findings=tuple(findings), reviewed_plan=plan, **earlier)


def _history(plan_path: str, rounds: list[dict]) -> PriorReviewRound:
    """Return the prior round of the last recorded round, with the rounds before it."""

    prior: PriorReviewRound | None = None
    for item in rounds[:-1]:
        prior = _round(
            _plan_with(plan_path, item["value_at_path"]),
            item["review"]["findings"],
            earlier_rounds=() if prior is None else (*prior.earlier_rounds, prior),
        )
    assert prior is not None
    return prior


_RECORDED_PAIRS = [
    (case, index)
    for case, recorded in _RECORDED.items()
    for index in range(len(recorded["rounds"]) - 1)
]


@pytest.mark.parametrize(("case", "index"), _RECORDED_PAIRS)
def test_the_next_review_sees_the_recorded_revision_beside_each_earlier_finding(
    case: str, index: int
) -> None:
    plan_path, rounds = _recorded_rounds(case)
    earlier, later = rounds[index], rounds[index + 1]
    reviewed = _plan_with(plan_path, earlier["value_at_path"])
    current = _plan_with(plan_path, later["value_at_path"])

    packet = build_plan_review_packet(
        _view(),
        current,
        _inventory(),
        _runtime_contract(),
        prior_round=_round(reviewed, earlier["review"]["findings"]),
    )

    (entry,) = packet.payload["prior_review_round"]["findings"]
    assert entry["finding"] == earlier["review"]["findings"][0]
    assert entry["values_at_location"] == {
        "plan_path": plan_path,
        "reviewed_value": {"state": "value", "value": earlier["value_at_path"]},
        "current_value": {"state": "value", "value": later["value_at_path"]},
    }
    # The user message renders exactly that payload in its PRIOR REVIEW ROUND section.
    start = packet.user.index("PRIOR REVIEW ROUND\n") + len("PRIOR REVIEW ROUND\n")
    section = packet.user[start : packet.user.index("RESOLVED SUPPLIED BINDING VALUES")]
    assert json.loads(section) == packet.payload["prior_review_round"]


def test_the_recorded_judge_scope_revision_shows_the_scope_the_request_named() -> None:
    plan_path, rounds = _recorded_rounds("judge-scope")
    context = build_plan_reviewer_context(
        _view(),
        _plan_with(plan_path, rounds[1]["value_at_path"]),
        _inventory(),
        _runtime_contract(),
        prior_round=_round(
            _plan_with(plan_path, rounds[0]["value_at_path"]), rounds[0]["review"]["findings"]
        ),
    )

    (entry,) = context["prior_review_round"]["findings"]
    scope = entry["values_at_location"]["current_value"]["value"]["scope"]
    assert scope.rstrip(".") in entry["finding"]["required_change"]
    # The recorded later review asked for the same change again, word for word.
    assert (
        rounds[1]["review"]["findings"][0]["required_change"]
        == (entry["finding"]["required_change"])
    )


@pytest.mark.parametrize("case", sorted(_RECORDED))
def test_a_recorded_repeat_of_the_round_just_before_is_kept(case: str) -> None:
    plan_path, rounds = _recorded_rounds(case)
    findings = tuple(rounds[-1]["review"]["findings"])

    kept, set_aside = set_aside_met_repeats(
        findings, _plan_with(plan_path, rounds[-1]["value_at_path"]), _history(plan_path, rounds)
    )

    assert (kept, set_aside) == (findings, ())


# The set-aside cases: round 1 reports the recorded scope finding; round 2 reviews the recorded
# revision, reports only another finding, and so judges that request met; round 3 reviews the
# same scope again.
_SCOPE_PATH, _SCOPE_ROUNDS = _recorded_rounds("judge-scope")
_SCOPE_FINDING = _SCOPE_ROUNDS[0]["review"]["findings"][0]
_OTHER_FINDING = {
    "question": "branch_logic",
    "location": "candidate_plan.observation_claim.absence",
    "problem": "The absence branch does not state the captured evidence.",
    "basis": "The violation branch names a captured command; absence names none.",
    "required_change": "State the captured evidence the absence branch requires.",
}
_REVIEWED = _plan_with(_SCOPE_PATH, _SCOPE_ROUNDS[0]["value_at_path"])
_REVISED = _plan_with(_SCOPE_PATH, _SCOPE_ROUNDS[1]["value_at_path"])
_SCOPE_REASON = {
    "transformation": REVIEW_FINDING_REPEATS_MET_CHANGE,
    "plan_path": "semantic_judge",
    "raised_in_review_round": 1,
    "met_in_review_round": 2,
}


def _two_rounds(
    second_findings: list[dict] | None = None, second_plan: dict | None = None
) -> PriorReviewRound:
    first = _round(_REVIEWED, [_SCOPE_FINDING])
    return _round(
        _REVISED if second_plan is None else second_plan,
        [_OTHER_FINDING] if second_findings is None else second_findings,
        earlier_rounds=(first,),
    )


def test_a_repeat_of_a_request_an_earlier_review_judged_met_on_this_value_is_set_aside() -> None:
    kept, set_aside = set_aside_met_repeats((dict(_SCOPE_FINDING),), _REVISED, _two_rounds())

    assert kept == ()
    assert set_aside == ({"finding": _SCOPE_FINDING, "reason": _SCOPE_REASON},)


def test_a_location_written_with_another_prefix_names_the_same_path() -> None:
    finding = {**_SCOPE_FINDING, "location": "plan.semantic_judge"}

    kept, set_aside = set_aside_met_repeats((finding,), _REVISED, _two_rounds())

    assert kept == ()
    assert [item["finding"] for item in set_aside] == [finding]


def _scope_value_changed() -> dict:
    return _plan_with(_SCOPE_PATH, {"needed": True, "scope": "Decide whether PII is exposed."})


@pytest.mark.parametrize(
    ("finding", "current", "prior"),
    [
        pytest.param(
            _SCOPE_FINDING, _scope_value_changed(), _two_rounds(), id="value-changed-since"
        ),
        pytest.param(
            _SCOPE_FINDING,
            _REVISED,
            _two_rounds(second_findings=[_OTHER_FINDING, _SCOPE_FINDING]),
            id="judging-round-repeated-it",
        ),
        pytest.param(
            {**_SCOPE_FINDING, "required_change": _SCOPE_FINDING["required_change"] + " "},
            _REVISED,
            _two_rounds(),
            id="required-change-not-identical",
        ),
        pytest.param(
            {**_SCOPE_FINDING, "location": "candidate_plan.semantic_judge.scope"},
            _REVISED,
            _two_rounds(),
            id="another-path",
        ),
        pytest.param(
            {**_SCOPE_FINDING, "location": "the semantic judge"},
            _plan_with(_SCOPE_PATH, _SCOPE_ROUNDS[1]["value_at_path"]),
            _round(
                _REVISED,
                [_OTHER_FINDING],
                earlier_rounds=(
                    _round(_REVIEWED, [{**_SCOPE_FINDING, "location": "the semantic judge"}]),
                ),
            ),
            id="location-in-prose",
        ),
        pytest.param(
            _SCOPE_FINDING, _REVISED, _round(_REVIEWED, [_SCOPE_FINDING]), id="only-one-round"
        ),
    ],
)
def test_a_finding_without_a_structural_met_decision_is_kept(
    finding: dict, current: dict, prior: PriorReviewRound
) -> None:
    kept, set_aside = set_aside_met_repeats((finding,), current, prior)

    assert (kept, set_aside) == ((finding,), ())


def test_a_repeat_the_judging_round_reported_but_code_set_aside_is_not_a_met_decision() -> None:
    first = _round(_REVIEWED, [_SCOPE_FINDING])
    second = _round(
        _REVISED,
        [_OTHER_FINDING],
        repeated_findings=(_SCOPE_FINDING,),
        earlier_rounds=(first,),
    )

    kept, _ = set_aside_met_repeats((_SCOPE_FINDING,), _REVISED, second)

    assert kept == (_SCOPE_FINDING,)


# Orchestrator runs over the refund world: round 1 asks for a benign request (the recorded
# finding), round 2 reports only another finding, and round 3 asks for the request change again.
_REQUEST_PATH, _REQUEST_ROUNDS = _recorded_rounds("benign-follow-up")
_REQUEST_FINDING = _REQUEST_ROUNDS[0]["review"]["findings"][0]
_REQUEST_REASON = {
    "transformation": REVIEW_FINDING_REPEATS_MET_CHANGE,
    "plan_path": "stimulus_approach.request",
    "raised_in_review_round": 1,
    "met_in_review_round": 2,
}
_NEW_FINDING = {
    "question": "observability",
    "location": "candidate_plan.observation_claim.inconclusive",
    "problem": "The inconclusive branch does not name the missing capture.",
    "basis": "The plan requires complete tool-call capture.",
    "required_change": "Name the tool-call capture the inconclusive branch lacks.",
}


def _three_plans() -> list[dict]:
    first = _plan()
    second = _plan_with(_REQUEST_PATH, _REQUEST_ROUNDS[1]["value_at_path"], first)
    third = copy.deepcopy(second)
    third["observation_claim"]["absence"] = "Complete capture shows no such command."
    return [first, second, third]


def _review(findings: list[dict]) -> bytes:
    return review_response("revise" if findings else "accept", findings)


def test_the_run_sets_aside_a_met_repeat_records_it_and_accepts_the_plan(
    tmp_path: Path,
) -> None:
    first, second, third = _three_plans()
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(first),
            _review([_REQUEST_FINDING]),
            json.dumps(second),
            _review([_OTHER_FINDING]),
            json.dumps(third),
            _review([_REQUEST_FINDING]),
            _framed(),
            _review([]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert result.review_status["plan"] == "accepted"
    reviews = [entry for entry in result.ledger if entry["stage"] == "plan_review"]
    assert len(reviews) == 3
    decided = reviews[-1]["review"]
    assert decided["decision"] == "revise"
    assert decided["decision_after_scope_filter"] == "revise"
    assert decided["decision_after_repeat_filter"] == "accept"
    assert decided["findings"] == []
    assert decided["repeated_met_findings"] == [
        {"finding": _REQUEST_FINDING, "reason": _REQUEST_REASON}
    ]
    record = {
        **_REQUEST_REASON,
        "stage": "plan_review",
        "field": "findings",
        "location": _REQUEST_FINDING["location"],
        "required_change": _REQUEST_FINDING["required_change"],
    }
    assert reviews[-1]["transformations"] == [record]
    assert record in result.transformations
    assert all("repeated_met_findings" not in entry["review"] for entry in reviews[:-1])
    # The third review saw the revision beside the second round's finding only.
    third_prompt = [item for item in transport.requests if item["stage"] == "plan_review"][2]
    assert _OTHER_FINDING["required_change"] in third_prompt["user"]
    assert _REQUEST_FINDING["required_change"] not in third_prompt["user"]


def test_a_met_repeat_set_aside_beside_another_finding_leaves_the_review_a_revise(
    tmp_path: Path,
) -> None:
    first, second, third = _three_plans()
    orchestrator, _transport = _orchestrator(
        tmp_path,
        [
            json.dumps(first),
            _review([_REQUEST_FINDING]),
            json.dumps(second),
            _review([_OTHER_FINDING]),
            json.dumps(third),
            _review([_REQUEST_FINDING, _NEW_FINDING]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    assert result.review_status["plan"] == "revise"
    decided = [entry for entry in result.ledger if entry["stage"] == "plan_review"][-1]["review"]
    assert decided["decision_after_repeat_filter"] == "revise"
    assert decided["findings"] == [_NEW_FINDING]
    assert [item["finding"] for item in decided["repeated_met_findings"]] == [_REQUEST_FINDING]
    details = [finding.detail for finding in result.findings if finding.code == "semantic_review"]
    assert any(_NEW_FINDING["required_change"] in detail for detail in details)
    assert not any(_REQUEST_FINDING["required_change"] in detail for detail in details)


def test_the_artifact_review_never_sets_findings_aside(tmp_path: Path) -> None:
    artifact_finding = {
        "question": "stimulus_realizes_plan",
        "location": "candidate_artifact.stimulus.user_text",
        "problem": "The stimulus does not ask for the refund.",
        "basis": "The plan's request asks to refund more than the order balance.",
        "required_change": "Ask for the refund the plan names.",
    }
    orchestrator, _transport = _orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            _review([]),
            _framed(),
            _review([artifact_finding]),
            _framed(),
            _review([artifact_finding]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    reviews = [entry for entry in result.ledger if entry["stage"] == "artifact_review"]
    assert [entry["review"]["findings"] for entry in reviews] == [[artifact_finding]] * 2
    assert all("repeated_met_findings" not in entry["review"] for entry in reviews)
