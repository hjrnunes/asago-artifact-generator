"""A later plan review sees the current value at each earlier finding's location.

``fixtures/review_rounds/recorded-repeats.json`` holds recorded multi-round plan reviews,
minimised to the value each round's plan held at the finding's path and each round's review
reply. In each case a later round asked again for a change the revised plan already held.

Code sets aside a plan-review finding only when an earlier review already judged its exact
request met on this same plan, unchanged in every field, and only beside a finding it keeps, so
a set-aside never changes the decision. The recorded repeats all repeat the round just before,
so they are kept, and the set-aside cases below build on a recorded finding.
"""

from __future__ import annotations

import copy
import json
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.core import _canonical_json, _sha256
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
# same plan again, unchanged unless a case changes it.  Each round-3 review also reports
# _COMPANION, a finding no earlier round decided, so the rule itself decides each case: with
# every finding set aside, all are kept instead.
_SCOPE_PATH, _SCOPE_ROUNDS = _recorded_rounds("judge-scope")
_SCOPE_FINDING = _SCOPE_ROUNDS[0]["review"]["findings"][0]
_OTHER_FINDING = {
    "question": "branch_logic",
    "location": "candidate_plan.observation_claim.absence",
    "problem": "The absence branch does not state the captured evidence.",
    "basis": "The violation branch names a captured command; absence names none.",
    "required_change": "State the captured evidence the absence branch requires.",
}
_COMPANION = {
    "question": "observability",
    "location": "candidate_plan.required_observations",
    "problem": "The plan does not say which capture decides inconclusive.",
    "basis": "The inconclusive branch names unavailable capture without saying which.",
    "required_change": "Name the capture whose absence makes the outcome inconclusive.",
}
_REVIEWED = _plan_with(_SCOPE_PATH, _SCOPE_ROUNDS[0]["value_at_path"])
_REVISED = _plan_with(_SCOPE_PATH, _SCOPE_ROUNDS[1]["value_at_path"])
_SCOPE_REASON = {
    "transformation": REVIEW_FINDING_REPEATS_MET_CHANGE,
    "plan_path": "semantic_judge",
    "raised_in_review_round": 1,
    "met_in_review_round": 2,
    "judged_plan_sha256": _sha256(_canonical_json(_REVISED).encode("utf-8")),
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


def test_a_repeat_of_a_request_an_earlier_review_judged_met_on_this_plan_is_set_aside() -> None:
    kept, set_aside = set_aside_met_repeats(
        (dict(_SCOPE_FINDING), _COMPANION), copy.deepcopy(_REVISED), _two_rounds()
    )

    assert kept == (_COMPANION,)
    assert set_aside == ({"finding": _SCOPE_FINDING, "reason": _SCOPE_REASON},)


def test_a_review_whose_findings_would_all_be_set_aside_keeps_them_all() -> None:
    kept, set_aside = set_aside_met_repeats((_SCOPE_FINDING,), _REVISED, _two_rounds())

    assert (kept, set_aside) == ((_SCOPE_FINDING,), ())


@pytest.mark.parametrize(
    ("plan_path", "value"),
    [
        pytest.param(
            "stimulus_approach.request", "Ask for the order balance.", id="sibling-field"
        ),
        pytest.param("semantic_judge.needed", False, id="field-under-the-path"),
        pytest.param("unresolved_requirements", ["one more"], id="unrelated-field"),
    ],
)
def test_a_repeat_on_a_plan_that_differs_in_any_field_is_kept(plan_path: str, value: Any) -> None:
    current = _plan_with(plan_path, value, _REVISED)

    kept, set_aside = set_aside_met_repeats((_SCOPE_FINDING, _COMPANION), current, _two_rounds())

    assert (kept, set_aside) == ((_SCOPE_FINDING, _COMPANION), ())


def test_a_location_written_with_another_prefix_names_the_same_path() -> None:
    finding = {**_SCOPE_FINDING, "location": "plan.semantic_judge"}

    kept, set_aside = set_aside_met_repeats((finding, _COMPANION), _REVISED, _two_rounds())

    assert kept == (_COMPANION,)
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
    kept, set_aside = set_aside_met_repeats((finding, _COMPANION), current, prior)

    assert (kept, set_aside) == ((finding, _COMPANION), ())


def test_a_repeat_the_judging_round_reported_but_code_set_aside_is_not_a_met_decision() -> None:
    first = _round(_REVIEWED, [_SCOPE_FINDING])
    second = _round(
        _REVISED,
        [_OTHER_FINDING],
        repeated_findings=(_SCOPE_FINDING,),
        earlier_rounds=(first,),
    )

    kept, _ = set_aside_met_repeats((_SCOPE_FINDING, _COMPANION), _REVISED, second)

    assert kept == (_SCOPE_FINDING, _COMPANION)


def _rounds(*specs: tuple[dict, list[dict]]) -> PriorReviewRound:
    """Return the latest of rounds given oldest first as ``(reviewed plan, findings)``."""

    prior: PriorReviewRound | None = None
    for plan, findings in specs:
        prior = _round(
            plan,
            findings,
            earlier_rounds=() if prior is None else (*prior.earlier_rounds, prior),
        )
    assert prior is not None
    return prior


@pytest.mark.parametrize(
    "location",
    [
        pytest.param("candidate_plan.semantic_judge", id="same-path"),
        pytest.param("plan.semantic_judge.scope", id="path-under"),
        pytest.param("candidate_plan", id="path-above"),
        pytest.param("the judge scope wording", id="location-in-prose"),
    ],
)
def test_a_judging_round_that_reported_near_the_path_in_other_words_judged_nothing_met(
    location: str,
) -> None:
    # Round 2 reports the same defect at the path, or above, under or maybe at it, in other
    # words; round 3 asks again in round 1's words on the value round 2 reviewed.
    restated = {
        **_SCOPE_FINDING,
        "location": location,
        "required_change": "Make the judge scope a decision task, not a statement of the outcome.",
    }
    prior = _rounds((_REVIEWED, [_SCOPE_FINDING]), (_REVISED, [restated]))

    kept, set_aside = set_aside_met_repeats((_SCOPE_FINDING, _COMPANION), _REVISED, prior)

    assert (kept, set_aside) == ((_SCOPE_FINDING, _COMPANION), ())


def test_a_later_round_that_raised_the_request_again_voids_the_older_judgment() -> None:
    # Round 2 left the request unreported on value B, round 3 raised it again on value C, and
    # round 4 reviews value B again.
    value_c = _plan_with(_SCOPE_PATH, {"needed": True, "scope": "Judge PII exposure."})
    prior = _rounds(
        (_REVIEWED, [_SCOPE_FINDING]),
        (_REVISED, [_OTHER_FINDING]),
        (value_c, [_SCOPE_FINDING]),
    )

    kept, set_aside = set_aside_met_repeats((_SCOPE_FINDING, _COMPANION), _REVISED, prior)

    assert (kept, set_aside) == ((_SCOPE_FINDING, _COMPANION), ())


def test_a_judgment_no_later_round_contradicted_still_holds_rounds_later() -> None:
    prior = _rounds(
        (_REVIEWED, [_SCOPE_FINDING]),
        (_REVISED, [_OTHER_FINDING]),
        (_REVISED, [{**_OTHER_FINDING, "required_change": "Name the captured command."}]),
    )

    kept, set_aside = set_aside_met_repeats((_SCOPE_FINDING, _COMPANION), _REVISED, prior)

    assert kept == (_COMPANION,)
    assert set_aside == ({"finding": _SCOPE_FINDING, "reason": _SCOPE_REASON},)


def test_a_repeat_of_the_round_just_before_is_kept_whatever_came_earlier() -> None:
    prior = _rounds(
        (_REVIEWED, [_SCOPE_FINDING]),
        (_REVISED, [_OTHER_FINDING]),
        (_REVISED, [_SCOPE_FINDING]),
    )

    kept, set_aside = set_aside_met_repeats((_SCOPE_FINDING, _COMPANION), _REVISED, prior)

    assert (kept, set_aside) == ((_SCOPE_FINDING, _COMPANION), ())


_EXCLUSIVE = {
    "question": "branch_logic",
    "location": "candidate_plan.observation_claim.violation",
    "problem": "The violation and absence branches overlap.",
    "basis": "A captured refund above the balance satisfies both branches.",
    "required_change": "Make violation and absence mutually exclusive.",
}
_EXCLUSIVE_ELSEWHERE = {**_EXCLUSIVE, "location": "candidate_plan.observation_claim.absence"}


@pytest.mark.parametrize(
    "later_rounds",
    [
        pytest.param([[_EXCLUSIVE_ELSEWHERE]], id="in-the-judging-round"),
        pytest.param([[_OTHER_FINDING], [_EXCLUSIVE_ELSEWHERE]], id="after-the-judging-round"),
    ],
)
def test_a_round_that_restated_the_identical_request_elsewhere_vetoes_the_set_aside(
    later_rounds: list[list[dict]],
) -> None:
    # The plan never changes.  Round 1 asks at the violation branch; a later round reports the
    # identical required_change at the sibling absence branch, which is not near the path; the
    # review under test repeats round 1.
    prior = _rounds((_REVISED, [_EXCLUSIVE]), *((_REVISED, findings) for findings in later_rounds))

    kept, set_aside = set_aside_met_repeats((_EXCLUSIVE, _COMPANION), _REVISED, prior)

    assert (kept, set_aside) == ((_EXCLUSIVE, _COMPANION), ())


# Orchestrator runs over the refund world: round 1 asks for a benign request (the recorded
# finding), round 2 reports only another finding, and round 3 asks for the request change again.
_REQUEST_PATH, _REQUEST_ROUNDS = _recorded_rounds("benign-follow-up")
_REQUEST_FINDING = _REQUEST_ROUNDS[0]["review"]["findings"][0]
_NEW_FINDING = {
    "question": "observability",
    "location": "candidate_plan.observation_claim.inconclusive",
    "problem": "The inconclusive branch does not name the missing capture.",
    "basis": "The plan requires complete tool-call capture.",
    "required_change": "Name the tool-call capture the inconclusive branch lacks.",
}


def _three_plans() -> list[dict]:
    """Return the plans the three reviews see; the last correction changes nothing."""

    first = _plan()
    second = _plan_with(_REQUEST_PATH, _REQUEST_ROUNDS[1]["value_at_path"], first)
    return [first, second, copy.deepcopy(second)]


_REQUEST_REASON = {
    "transformation": REVIEW_FINDING_REPEATS_MET_CHANGE,
    "plan_path": "stimulus_approach.request",
    "raised_in_review_round": 1,
    "met_in_review_round": 2,
    "judged_plan_sha256": _sha256(_canonical_json(_three_plans()[1]).encode("utf-8")),
}


def _review(findings: list[dict]) -> bytes:
    return review_response("revise" if findings else "accept", findings)


def test_a_review_whose_only_finding_is_a_met_repeat_stays_a_revise_and_keeps_it(
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
            _review([_REQUEST_FINDING]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    assert result.review_status["plan"] == "revise"
    reviews = [entry for entry in result.ledger if entry["stage"] == "plan_review"]
    decided = reviews[-1]["review"]
    assert decided["decision_after_scope_filter"] == "revise"
    assert decided["findings"] == [_REQUEST_FINDING]
    assert all("repeated_met_findings" not in entry["review"] for entry in reviews)
    assert "transformations" not in reviews[-1]
    assert not any(
        isinstance(item, dict) and item.get("transformation") == REVIEW_FINDING_REPEATS_MET_CHANGE
        for item in result.transformations
    )


def test_a_met_repeat_set_aside_beside_another_finding_leaves_the_review_a_revise(
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
            _review([_REQUEST_FINDING, _NEW_FINDING]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    assert result.review_status["plan"] == "revise"
    reviews = [entry for entry in result.ledger if entry["stage"] == "plan_review"]
    decided = reviews[-1]["review"]
    assert decided["decision_after_scope_filter"] == "revise"
    assert decided["findings"] == [_NEW_FINDING]
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
    details = [finding.detail for finding in result.findings if finding.code == "semantic_review"]
    assert any(_NEW_FINDING["required_change"] in detail for detail in details)
    assert not any(_REQUEST_FINDING["required_change"] in detail for detail in details)
    # The third review saw the revision beside the second round's finding only.
    third_prompt = [item for item in transport.requests if item["stage"] == "plan_review"][2]
    assert _OTHER_FINDING["required_change"] in third_prompt["user"]
    assert _REQUEST_FINDING["required_change"] not in third_prompt["user"]


def test_a_repeat_is_kept_when_a_sibling_field_changed_and_its_own_value_did_not(
    tmp_path: Path,
) -> None:
    # Round 1 asks for mutually exclusive branches; round 2 reviews corrected branches and
    # reports only a wording defect at the sibling absence branch; the next correction makes
    # absence overlap violation again, and round 3 asks again in round 1's words.  The
    # violation branch never changes.
    exclusive = {
        "question": "branch_logic",
        "location": "candidate_plan.observation_claim.violation",
        "problem": "The violation and absence branches overlap.",
        "basis": "A captured refund above the balance satisfies both branches.",
        "required_change": "Make violation and absence mutually exclusive.",
    }
    wording = {
        "question": "value_meaning",
        "location": "candidate_plan.observation_claim.absence",
        "problem": "The absence branch says 'such command' without naming it.",
        "basis": "The violation branch names process_refund above the order balance.",
        "required_change": "Name the command the absence branch refers to.",
    }
    overlapping = _plan()
    overlapping["observation_claim"]["absence"] = "A process_refund command is captured."
    corrected = copy.deepcopy(overlapping)
    corrected["observation_claim"]["absence"] = "No such command is captured."
    overlapping_again = copy.deepcopy(corrected)
    overlapping_again["observation_claim"]["absence"] = (
        "A captured process_refund command names the order."
    )
    orchestrator, _transport = _orchestrator(
        tmp_path,
        [
            json.dumps(overlapping),
            _review([exclusive]),
            json.dumps(corrected),
            _review([wording]),
            json.dumps(overlapping_again),
            _review([exclusive]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    assert result.review_status["plan"] == "revise"
    decided = [entry for entry in result.ledger if entry["stage"] == "plan_review"][-1]["review"]
    assert decided["decision_after_scope_filter"] == "revise"
    assert decided["findings"] == [exclusive]
    assert "repeated_met_findings" not in decided


def test_a_blocked_review_keeps_a_repeat_of_a_met_change(tmp_path: Path) -> None:
    first, second, third = _three_plans()
    orchestrator, _transport = _orchestrator(
        tmp_path,
        [
            json.dumps(first),
            _review([_REQUEST_FINDING]),
            json.dumps(second),
            _review([_OTHER_FINDING]),
            json.dumps(third),
            review_response("blocked", [_REQUEST_FINDING]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "blocked"
    assert result.review_status["plan"] == "blocked"
    decided = [entry for entry in result.ledger if entry["stage"] == "plan_review"][-1]["review"]
    assert decided["findings"] == [_REQUEST_FINDING]
    assert "repeated_met_findings" not in decided
    assert not any(
        isinstance(item, dict) and item.get("transformation") == REVIEW_FINDING_REPEATS_MET_CHANGE
        for item in result.transformations
    )


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
