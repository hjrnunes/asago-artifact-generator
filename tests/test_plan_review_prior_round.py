"""A second plan review sees the findings of the first and the author's response."""

from __future__ import annotations

import copy
import hashlib
import json
from functools import partial
from pathlib import Path

from asago_artifact_generator.authoring.policy import AuthoringPolicy
from asago_artifact_generator.authoring.review import (
    PriorReviewRound,
    build_plan_review_packet,
    build_plan_reviewer_context,
)
from asago_artifact_generator.input_adapter import load_input

from . import test_versioned_authoring_wire as wire
from .support import scripted_orchestrator
from .test_handoff_v3 import _OBSERVED
from .test_versioned_authoring_wire import _framed
from .test_versioned_prompt_roles import _inventory, _plan, _runtime_contract, _view

_orchestrator = partial(scripted_orchestrator, task_id="prior-round")

# Digests of first-round review prompts rendered by consumer commit 9baa154.
_FIRST_ROUND_SYSTEM = "02eaec14793e9dec4d59370ac2413a32da957f3273e6c80a0ae1212cc15b35c7"
_FIRST_ROUND_USER = "856e69ef704e1002be3b6975351d5d1f903f8c8ec74238f111b106bb5289eb09"
_FIRST_ROUND_CONDITION_USER = "bcf22916bb917c8ff0dd80fb4e00bf755b31a99b501ca2234d12f86b8e021ca0"

_FINDING = {
    "question": "branch_logic",
    "location": "candidate_plan.observation_claim.violation",
    "problem": "The violation condition compares the argument with a literal.",
    "basis": "The scenario binds the record from the authenticated identity.",
    "required_change": "Compare the argument with the bound authenticated identity.",
}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _revised_plan() -> dict:
    plan = copy.deepcopy(_plan())
    plan["observation_claim"]["violation"] = "Complete capture shows the bound identity mismatch."
    return plan


def _prior() -> PriorReviewRound:
    return PriorReviewRound(findings=(dict(_FINDING),), reviewed_plan=_plan())


def _review_reply(findings: list[dict]) -> bytes:
    decision = "revise" if findings else "accept"
    return json.dumps({"decision": decision, "summary": "scripted", "findings": findings}).encode()


def test_a_first_round_review_prompt_is_the_one_9baa154_rendered() -> None:
    plain = build_plan_review_packet(_view(), _plan(), _inventory(), _runtime_contract())
    conditioned = build_plan_review_packet(
        load_input(_OBSERVED), _plan(), _inventory(), _runtime_contract()
    )

    assert _digest(plain.system) == _FIRST_ROUND_SYSTEM
    assert _digest(plain.user) == _FIRST_ROUND_USER
    assert _digest(conditioned.system) == _FIRST_ROUND_SYSTEM
    assert _digest(conditioned.user) == _FIRST_ROUND_CONDITION_USER
    assert "prior_review_round" not in plain.payload


def test_a_second_round_review_prompt_carries_the_first_round_findings() -> None:
    packet = build_plan_review_packet(
        _view(), _revised_plan(), _inventory(), _runtime_contract(), prior_round=_prior()
    )

    assert "PRIOR REVIEW ROUND" in packet.user
    section = packet.user[packet.user.index("PRIOR REVIEW ROUND") :]
    for field in _FINDING.values():
        assert field in section
    assert packet.payload["prior_review_round"]["findings"] == [_FINDING]
    assert packet.user.index("CANDIDATE PLAN") < packet.user.index("PRIOR REVIEW ROUND")


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
    revised = wire._plan()
    revised["observation_claim"]["violation"] = "Complete capture shows the bound mismatch."
    orchestrator, transport = _orchestrator(
        tmp_path,
        [
            json.dumps(wire._plan()),
            _review_reply([_FINDING]),
            json.dumps(revised),
            _review_reply([]),
            _framed(),
            _review_reply([]),
        ],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(wire._view(), wire._inventory(), wire._runtime_contract())

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
        [b"{}", json.dumps(wire._plan()), _review_reply([]), _framed(), _review_reply([])],
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(wire._view(), wire._inventory(), wire._runtime_contract())

    assert result.status == "accepted"
    review = next(item for item in transport.requests if item["stage"] == "plan_review")
    assert "PRIOR REVIEW ROUND" not in review["user"]
