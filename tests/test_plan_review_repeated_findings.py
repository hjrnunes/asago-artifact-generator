"""A later plan review sees the current value at each earlier finding's location.

``fixtures/review_rounds/recorded-repeats.json`` holds recorded multi-round plan reviews,
minimised to the value each round's plan held at the finding's path and each round's review
reply. In each case a later round asked again for a change the revised plan already held.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.review import (
    PriorReviewRound,
    build_plan_review_packet,
    build_plan_reviewer_context,
    finding_plan_path,
)

from .support import world_builders

_RECORDED = json.loads(
    (Path(__file__).parent / "fixtures" / "review_rounds" / "recorded-repeats.json").read_text(
        encoding="utf-8"
    )
)["cases"]

_inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "inventory", "plan", "runtime_contract", "view"
)


def _plan_with(plan_path: str, value: Any) -> dict:
    """Return the refund plan with ``value`` at ``plan_path``."""

    changed = copy.deepcopy(_plan())
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


def _round(plan: dict, findings: list[dict]) -> PriorReviewRound:
    return PriorReviewRound(findings=tuple(findings), reviewed_plan=plan)


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
