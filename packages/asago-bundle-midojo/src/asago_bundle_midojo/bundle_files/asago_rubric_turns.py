"""The rubric judge of a sequential reply bundle: the final turn, with the earlier user turns.

Only a sequential reply bundle's suite package gets this file; every other bundle keeps
``asago_rubric.py`` alone, unchanged. It imports the standard library and its sibling
``asago_rubric``; ``midojo.verifiers`` is imported when the verifier is registered.

MiDojo grades once, after the last turn, and hands a verifier the final reply and the
recorded calls only (``midojo.verifiers.VerificationContext`` at 29d2b6e). So the judge
grades the final turn (``judge_turns`` is ``final``): its ``user_text`` is the last user
turn, its ``history`` the earlier user turns, and its ``tool_calls`` every call of the
conversation, as in Garak's final turn. Garak's history also interleaves the target's
earlier replies; MiDojo gives a verifier no route to them, so this history lacks them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .asago_rubric import (
    RUBRIC_NAME,
    SPEC_KEYS,
    RubricCheck,
    RubricJudgeVerifier,
    build_evidence,
    is_complete,
    judge_output,
    parse_spec,
    write_verdict,
)

TURNS_RUBRIC_NAME = f"{RUBRIC_NAME}_turns"
TURN_KEYS = frozenset({"history", "judge_turns"})
FINAL_TURN = "final"


@dataclass(frozen=True)
class TurnsCheck:
    """A parsed sequential spec: the rubric check of the final turn and the earlier turns."""

    check: RubricCheck
    history: tuple[dict[str, str], ...]


def parse_turns_spec(spec: Any) -> TurnsCheck:
    """Return the check of a sequential rubric spec, or raise ``ValueError``."""

    if not isinstance(spec, dict) or set(spec) != SPEC_KEYS | TURN_KEYS:
        raise ValueError(
            f"{TURNS_RUBRIC_NAME} needs exactly the keys {sorted(SPEC_KEYS | TURN_KEYS)}"
        )
    if spec["judge_turns"] != FINAL_TURN:
        raise ValueError(
            f"{TURNS_RUBRIC_NAME} grades the final turn only, not {spec['judge_turns']!r}"
        )
    history = spec["history"]
    if not isinstance(history, list) or not all(
        isinstance(turn, dict)
        and set(turn) == {"role", "content"}
        and turn["role"] == "user"
        and isinstance(turn["content"], str)
        and turn["content"]
        for turn in history
    ):
        raise ValueError(f"{TURNS_RUBRIC_NAME} history is not a list of user turns")
    check = parse_spec({key: spec[key] for key in SPEC_KEYS})
    return TurnsCheck(check, tuple({"role": "user", "content": t["content"]} for t in history))


class TurnsRubricJudgeVerifier(RubricJudgeVerifier):
    """Judge the final reply with the rubric, the earlier user turns as its history."""

    name = TURNS_RUBRIC_NAME

    def parse(self, spec: Any) -> TurnsCheck:  # type: ignore[override]
        return parse_turns_spec(spec)

    def grade(self, check: TurnsCheck, ctx: Any) -> dict[str, Any]:  # type: ignore[override]
        """Judge the final reply, write the verdict file, and return the judge record."""

        rubric = check.check
        calls = list(ctx.function_calls)
        evidence = build_evidence(
            rubric.user_text,
            check.history,
            ctx.agent_output or "",
            calls,
            complete=is_complete(calls),
        )
        call = self._judge(rubric.judge_url, rubric.judge_model)
        record = judge_output(rubric.rubric, evidence, call)
        write_verdict(rubric.verdict_file, record)
        return record


def register() -> None:
    """Register the verifier; a module imported twice registers it once."""

    from midojo.verifiers import register_verifier

    try:
        register_verifier(TurnsRubricJudgeVerifier())
    except ValueError:
        pass
