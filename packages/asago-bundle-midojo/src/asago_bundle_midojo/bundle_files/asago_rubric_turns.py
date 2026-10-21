"""The rubric judge of a sequential reply bundle: each judged turn, with the turns before it.

Only a sequential reply bundle's suite package gets this file; every other bundle keeps
``asago_rubric.py`` alone, unchanged. It imports the standard library and its sibling
``asago_rubric``; ``midojo.verifiers`` is imported when the verifier is registered.

MiDojo grades once, after the last turn, and hands a verifier the conversation
(``VerificationContext.turns``, each user message and the agent's reply, at bddb79b) and
the recorded calls, each naming the turn it was made in (``turn``, from 0). The judge sees
what Garak's ``RubricJudge`` sees on ``SequentialInjection``'s attempt for the same turn:
``user_text`` is that turn's message, ``history`` the earlier messages and replies
interleaved (``{"role": "user"|"assistant", "content": text}``), the graded output is that
turn's reply, and ``tool_calls`` every call made up to and including that turn. With
``judge_turns`` ``final`` the judge grades the last turn; with ``all`` it grades every turn
in order, as Garak does, and the last turn's verdict grades the conversation, with the
requests of every turn counted.

The spec's ``user_text`` and ``history`` name the user turns the bundle planned. A
conversation that does not hold exactly those messages, each with a reply, or a call
without a turn the judge can place, is not graded: the verifier writes an ``unresolved``
verdict naming why, without a judge request.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# In a suite the rubric judge sits beside this file as ``asago_rubric.py`` (the compiler
# copies ``authoring/rubric_judge.py`` there); in the adapter it is that authoring module.
try:
    from .asago_rubric import (  # type: ignore[import-not-found]
        CLAIM_LEVEL,
        INCONCLUSIVE,
        OUTCOMES,
        RUBRIC_NAME,
        SPEC_KEYS,
        RubricCheck,
        RubricJudgeVerifier,
        _record,
        build_evidence,
        field,
        is_complete,
        judge_output,
        parse_spec,
        request_count,
    )
except ModuleNotFoundError as exc:
    if exc.name != f"{__package__}.asago_rubric":
        raise
    from asago_artifact_generator.authoring.rubric_judge import (
        CLAIM_LEVEL,
        INCONCLUSIVE,
        OUTCOMES,
        RUBRIC_NAME,
        SPEC_KEYS,
        RubricCheck,
        RubricJudgeVerifier,
        _record,
        build_evidence,
        field,
        is_complete,
        judge_output,
        parse_spec,
        request_count,
    )

TURNS_RUBRIC_NAME = f"{RUBRIC_NAME}_turns"
TURN_KEYS = frozenset({"history", "judge_turns"})
FINAL_TURN = "final"
ALL_TURNS = "all"
JUDGE_TURNS = frozenset({FINAL_TURN, ALL_TURNS})
# Why a conversation is not graded.
TURNS_MISSING = "judge_turns_missing"
TURN_COUNT_MISMATCH = "judge_turn_count_mismatch"
TURN_MESSAGE_MISMATCH = "judge_turn_message_mismatch"
TURN_REPLY_MISSING = "judge_turn_reply_missing"
CALL_TURN_INVALID = "judge_call_turn_invalid"


@dataclass(frozen=True)
class TurnsCheck:
    """A parsed sequential spec: the rubric check, the planned user turns, and the judged turns."""

    check: RubricCheck
    user_turns: tuple[str, ...]
    judge_turns: str


@dataclass(frozen=True)
class Turn:
    """One turn of the conversation MiDojo recorded: the user message and the agent's reply."""

    message: str
    reply: str


def parse_turns_spec(spec: Any) -> TurnsCheck:
    """Return the check of a sequential rubric spec, or raise ``ValueError``."""

    if not isinstance(spec, dict) or set(spec) != SPEC_KEYS | TURN_KEYS:
        raise ValueError(
            f"{TURNS_RUBRIC_NAME} needs exactly the keys {sorted(SPEC_KEYS | TURN_KEYS)}"
        )
    if spec["judge_turns"] not in JUDGE_TURNS:
        raise ValueError(
            f"{TURNS_RUBRIC_NAME} judge_turns is one of {sorted(JUDGE_TURNS)}, "
            f"not {spec['judge_turns']!r}"
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
    user_turns = (*(turn["content"] for turn in history), check.user_text)
    return TurnsCheck(check, user_turns, spec["judge_turns"])


def conversation(check: TurnsCheck, ctx: Any) -> list[Turn] | str:
    """Return the recorded turns when they are the planned ones, each replied, else why not.

    Garak grades no turn of a conversation whose turn got no reply text, so neither does
    this judge.
    """

    recorded = getattr(ctx, "turns", None)
    if not recorded:
        return TURNS_MISSING
    if len(recorded) != len(check.user_turns):
        return TURN_COUNT_MISMATCH
    messages = [field(turn, "message") for turn in recorded]
    if messages != list(check.user_turns):
        return TURN_MESSAGE_MISMATCH
    replies = [field(turn, "reply") for turn in recorded]
    if not all(isinstance(reply, str) and reply for reply in replies):
        return TURN_REPLY_MISSING
    return [Turn(m, r) for m, r in zip(messages, replies, strict=True)]


def placed_turns(calls: Sequence[Any], count: int) -> list[int] | None:
    """Return the turn each call was made in, or None when one names no turn of ``count``."""

    turns = [field(call, "turn") for call in calls]
    if all(type(turn) is int and 0 <= turn < count for turn in turns):
        return turns
    return None


def history_before(turns: Sequence[Turn], index: int) -> list[dict[str, str]]:
    """Return the messages and replies before turn ``index``, interleaved as Garak's history."""

    history: list[dict[str, str]] = []
    for turn in turns[:index]:
        history += [
            {"role": "user", "content": turn.message},
            {"role": "assistant", "content": turn.reply},
        ]
    return history


def write_conversation_verdict(path: Path, record: dict[str, Any], requests: int) -> None:
    """Write the deciding record's verdict, with the judge requests of the whole conversation."""

    verdict = record["verdict"]
    document = {
        "outcome": OUTCOMES.get(verdict, INCONCLUSIVE),
        "reason": record["reason"],
        "claim_level": CLAIM_LEVEL,
        "verdict": verdict,
        "evidence_refs": record["evidence_refs"],
        "request_count": requests,
    }
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class TurnsRubricJudgeVerifier(RubricJudgeVerifier):
    """Judge the judged turns' replies with the rubric, each with the conversation before it."""

    name = TURNS_RUBRIC_NAME

    def parse(self, spec: Any) -> TurnsCheck:  # type: ignore[override]
        return parse_turns_spec(spec)

    def grade(self, check: TurnsCheck, ctx: Any) -> dict[str, Any]:  # type: ignore[override]
        """Judge the conversation, write the verdict file, and return the deciding record.

        The deciding record is the last turn's; a conversation that cannot be graded gives
        an ``unresolved`` record naming why, and no judge request.
        """

        records = self._judge_conversation(check, ctx)
        record = records[-1]
        requests = sum(request_count(item) for item in records)
        write_conversation_verdict(check.check.verdict_file, record, requests)
        return record

    def _judge_conversation(self, check: TurnsCheck, ctx: Any) -> list[dict[str, Any]]:
        """Return the record of each judged turn, in order, or the one record of why none is.

        Garak's attempt for a turn holds every call made so far, so an earlier turn takes
        the calls whose ``turn`` is at most its own; the last turn takes every call.
        """

        turns = conversation(check, ctx)
        if isinstance(turns, str):
            return [_record("unresolved", [], turns)]
        calls = list(ctx.function_calls)
        count = len(turns)
        placed = placed_turns(calls, count)
        if check.judge_turns == ALL_TURNS and count > 1 and placed is None:
            return [_record("unresolved", [], CALL_TURN_INVALID)]
        judged = range(count) if check.judge_turns == ALL_TURNS else [count - 1]
        call = self._judge(check.check.judge_url, check.check.judge_model)
        records = []
        for index in judged:
            made = (
                calls
                if index == count - 1 or placed is None
                else [c for c, turn in zip(calls, placed, strict=True) if turn <= index]
            )
            records.append(judge_turn(check.check.rubric, turns, index, made, call))
        return records


def judge_turn(
    rubric: Any,
    turns: Sequence[Turn],
    index: int,
    calls: Sequence[Any],
    call: Callable[[list[dict[str, str]]], Any],
) -> dict[str, Any]:
    """Judge turn ``index``'s reply with the conversation before it and the calls so far."""

    evidence = build_evidence(
        turns[index].message,
        history_before(turns, index),
        turns[index].reply,
        calls,
        complete=is_complete(calls),
    )
    return judge_output(rubric, evidence, call)


def register() -> None:
    """Register the verifier; a module imported twice registers it once."""

    from midojo.verifiers import register_verifier

    try:
        register_verifier(TurnsRubricJudgeVerifier())
    except ValueError:
        pass
