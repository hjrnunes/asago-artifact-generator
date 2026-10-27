"""A second plan review sees the findings of the first and the author's response."""

from __future__ import annotations

import copy
import json
from functools import partial
from pathlib import Path

import pytest

from asago_artifact_generator.authoring.policy import AuthoringPolicy
from asago_artifact_generator.authoring.review import (
    PriorReviewRound,
    build_plan_review_packet,
    build_plan_reviewer_context,
    finding_plan_path,
)
from asago_artifact_generator.input_adapter import load_input

from .support import OBSERVED_HANDOFF, scripted_orchestrator, world_builders

_inventory, _plan, _runtime_contract, _view = world_builders(
    "ehr", "inventory", "plan", "runtime_contract", "view"
)
_refund_framed, _refund_inventory, _refund_plan, _refund_runtime_contract, _refund_view = (
    world_builders("refund", "framed", "inventory", "plan", "runtime_contract", "view")
)

_orchestrator = partial(scripted_orchestrator, task_id="prior-round")

_FINDING = {
    "question": "branch_logic",
    "location": "candidate_plan.observation_claim.violation",
    "problem": "The violation condition compares the argument with a literal.",
    "basis": "The scenario binds the record from the authenticated identity.",
    "required_change": "Compare the argument with the bound authenticated identity.",
}


def _revised_plan() -> dict:
    plan = copy.deepcopy(_plan())
    plan["observation_claim"]["violation"] = "Complete capture shows the bound identity mismatch."
    return plan


def _prior() -> PriorReviewRound:
    return PriorReviewRound(findings=(dict(_FINDING),), reviewed_plan=_plan())


def _review_reply(findings: list[dict]) -> bytes:
    decision = "revise" if findings else "accept"
    return json.dumps({"decision": decision, "summary": "scripted", "findings": findings}).encode()


def test_a_first_round_review_prompt_has_no_prior_round_section() -> None:
    plain = build_plan_review_packet(_view(), _plan(), _inventory(), _runtime_contract())
    conditioned = build_plan_review_packet(
        load_input(OBSERVED_HANDOFF), _plan(), _inventory(), _runtime_contract()
    )

    for packet in (plain, conditioned):
        assert "prior_review_round" not in packet.payload


def test_a_second_round_review_prompt_carries_the_first_round_findings() -> None:
    packet = build_plan_review_packet(
        _view(), _revised_plan(), _inventory(), _runtime_contract(), prior_round=_prior()
    )

    assert "PRIOR REVIEW ROUND" in packet.user
    section = packet.user[packet.user.index("PRIOR REVIEW ROUND") :]
    for field in _FINDING.values():
        assert field in section
    assert [entry["finding"] for entry in packet.payload["prior_review_round"]["findings"]] == [
        _FINDING
    ]
    assert packet.user.index("CANDIDATE PLAN") < packet.user.index("PRIOR REVIEW ROUND")


def test_each_earlier_finding_carries_the_reviewed_and_current_value_at_its_location() -> None:
    context = build_plan_reviewer_context(
        _view(), _revised_plan(), _inventory(), _runtime_contract(), prior_round=_prior()
    )

    (entry,) = context["prior_review_round"]["findings"]
    assert entry["values_at_location"] == {
        "plan_path": "observation_claim.violation",
        "reviewed_value": {"state": "value", "value": _plan()["observation_claim"]["violation"]},
        "current_value": {
            "state": "value",
            "value": "Complete capture shows the bound identity mismatch.",
        },
    }


def test_a_value_the_revision_removed_is_marked_absent() -> None:
    finding = {**_FINDING, "location": "candidate_plan.stimulus_approach.history"}
    revised = _revised_plan()
    del revised["stimulus_approach"]["history"]

    context = build_plan_reviewer_context(
        _view(),
        revised,
        _inventory(),
        _runtime_contract(),
        prior_round=PriorReviewRound(findings=(finding,), reviewed_plan=_plan()),
    )

    values = context["prior_review_round"]["findings"][0]["values_at_location"]
    assert values["plan_path"] == "stimulus_approach.history"
    assert values["reviewed_value"] == {"state": "value", "value": []}
    assert values["current_value"] == {"state": "absent"}


@pytest.mark.parametrize(
    "location",
    [
        "the violation and the absence branches",
        "candidate_plan.observation_claim.violation and absence",
        "candidate_plan",
        "candidate_plan.no_such_field",
        "observation_claim.violation[0]",
    ],
)
def test_a_location_that_is_not_one_existing_plan_path_is_marked_unresolved(
    location: str,
) -> None:
    finding = {**_FINDING, "location": location}

    context = build_plan_reviewer_context(
        _view(),
        _revised_plan(),
        _inventory(),
        _runtime_contract(),
        prior_round=PriorReviewRound(findings=(finding,), reviewed_plan=_plan()),
    )

    entry = context["prior_review_round"]["findings"][0]
    assert entry["finding"] == finding
    assert entry["values_at_location"] == {"state": "location_not_resolved"}


@pytest.mark.parametrize(
    ("location", "path"),
    [
        ("candidate_plan.observation_claim.violation", ("observation_claim", "violation")),
        ("plan.prerequisites[0]", ("prerequisites", 0)),
        (" runtime_bindings[1].selector ", ("runtime_bindings", 1, "selector")),
        ("semantic_judge", ("semantic_judge",)),
        ("observation_claim.violation (second branch)", None),
        ("observation_claim..violation", None),
        ("", None),
        (None, None),
    ],
)
def test_a_finding_location_reads_as_a_plan_path_only_in_the_documented_form(
    location: object, path: tuple | None
) -> None:
    assert finding_plan_path(location) == path


def test_the_values_example_matches_the_example_finding_of_the_response_contract() -> None:
    context = build_plan_reviewer_context(
        _view(), _revised_plan(), _inventory(), _runtime_contract(), prior_round=_prior()
    )

    example = context["prior_review_round"]["values_at_location_example"]
    example_finding = context["response_contract"]["example_finding"]
    assert example["finding_location"] == example_finding["location"]
    assert example["finding_required_change"] == example_finding["required_change"]
    assert finding_plan_path(example["finding_location"]) == ("observation_claim", "violation")


def test_a_second_round_review_prompt_shows_what_the_author_changed() -> None:
    context = build_plan_reviewer_context(
        _view(), _revised_plan(), _inventory(), _runtime_contract(), prior_round=_prior()
    )

    response = context["prior_review_round"]["author_response"]
    assert response["changed_fields"] == ["observation_claim"]
    assert response["reviewed_values"] == {
        "observation_claim": _plan()["observation_claim"],
    }


def test_a_second_round_review_judges_the_first_requests_before_raising_new_ones() -> None:
    context = build_plan_reviewer_context(
        _view(), _revised_plan(), _inventory(), _runtime_contract(), prior_round=_prior()
    )

    instruction = context["prior_review_round"]["reviewer_instruction"]
    assert "whether the candidate_plan now meets its required_change" in instruction
    assert "Compare the required_change with its current_value, not its reviewed_value" in (
        instruction
    )
    assert "do not report a finding that asks the author to undo" in instruction


def test_a_second_round_review_lists_a_field_the_author_removed() -> None:
    revised = _revised_plan()
    removed = next(key for key in revised if key not in {"observation_claim"})
    reviewed_value = revised.pop(removed)

    context = build_plan_reviewer_context(
        _view(), revised, _inventory(), _runtime_contract(), prior_round=_prior()
    )

    response = context["prior_review_round"]["author_response"]
    assert removed in response["changed_fields"]
    assert response["reviewed_values"][removed] == reviewed_value


def test_the_orchestrator_hands_the_second_review_its_first_round(tmp_path: Path) -> None:
    revised = _refund_plan()
    revised["observation_claim"]["violation"] = "Complete capture shows the bound mismatch."
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(_refund_plan()),
            _review_reply([_FINDING]),
            json.dumps(revised),
            _review_reply([]),
            _refund_framed(),
            _review_reply([]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_refund_view(), _refund_inventory(), _refund_runtime_contract())

    assert result.status == "accepted"
    reviews = [item for item in transport.requests if item["stage"] == "plan_review"]
    assert len(reviews) == 2
    assert "PRIOR REVIEW ROUND" not in reviews[0]["user"]
    assert "PRIOR REVIEW ROUND" in reviews[1]["user"]
    assert _FINDING["required_change"] in reviews[1]["user"]
    artifact_review = next(
        item for item in transport.requests if item["stage"] == "artifact_review"
    )
    assert "PRIOR REVIEW ROUND" not in artifact_review["user"]


def test_a_review_after_a_mechanical_correction_alone_has_no_prior_round(
    tmp_path: Path,
) -> None:
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            b"{}",
            json.dumps(_refund_plan()),
            _review_reply([]),
            _refund_framed(),
            _review_reply([]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_refund_view(), _refund_inventory(), _refund_runtime_contract())

    assert result.status == "accepted"
    review = next(item for item in transport.requests if item["stage"] == "plan_review")
    assert "PRIOR REVIEW ROUND" not in review["user"]
