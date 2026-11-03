"""Which turns a reply episode's judge grades, and the model request each one may take.

Compile writes the mode into the rubric and sizes the check's timeout from it; the judge
service reads the same mode when it grades. The two live here so neither adapter depends on
the other for one vocabulary. ``TIMEOUT_SECONDS`` is the rubric judge's own per-request
timeout, re-exported so a caller reads the whole vocabulary from one module.
"""

from __future__ import annotations

from midojo_sdk.rubric_judge import TIMEOUT_SECONDS

__all__ = ["ALL_TURNS", "FINAL_TURN", "JUDGE_TURNS", "TIMEOUT_SECONDS", "judged_count"]

FINAL_TURN = "final"
ALL_TURNS = "all"
JUDGE_TURNS = frozenset({FINAL_TURN, ALL_TURNS})


def judged_count(judge_turns: str, turns: int) -> int:
    """Return how many turns the judge grades: the last, or each of ``turns``."""

    return turns if judge_turns == ALL_TURNS else 1
