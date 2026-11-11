"""The judge-turn vocabulary compile and the judge service share, in the library."""

from __future__ import annotations

import pytest
from midojo_spec.rubric_judge import TIMEOUT_SECONDS as JUDGE_TIMEOUT_SECONDS

import asago_injection_spec
from asago_injection_spec.judge_turns import (
    ALL_TURNS,
    FINAL_TURN,
    JUDGE_TURNS,
    TIMEOUT_SECONDS,
    judged_count,
)


def test_the_judge_turn_vocabulary_is_the_services() -> None:
    assert FINAL_TURN == "final"
    assert ALL_TURNS == "all"
    assert JUDGE_TURNS == {"final", "all"}


@pytest.mark.parametrize(
    ("judge_turns", "turns", "expected"),
    (("final", 3, 1), ("all", 3, 3), ("all", 1, 1)),
)
def test_judged_count_counts_the_turns_the_mode_grades(
    judge_turns: str, turns: int, expected: int
) -> None:
    assert judged_count(judge_turns, turns) == expected


def test_the_timeout_is_the_rubric_judges() -> None:
    assert TIMEOUT_SECONDS == JUDGE_TIMEOUT_SECONDS == 180.0


@pytest.mark.parametrize(
    "name", ("ALL_TURNS", "FINAL_TURN", "JUDGE_TURNS", "TIMEOUT_SECONDS", "judged_count")
)
def test_the_package_root_binds_the_same_objects(name: str) -> None:
    assert getattr(asago_injection_spec, name) is getattr(asago_injection_spec.judge_turns, name)
