"""The rubric judge of a reply episode: each judged turn, with the turns before it.

``compile`` copies this file, and ``authoring/rubric_judge.py`` beside it as
``asago_rubric.py``, into a reply episode's ``verifiers`` package. The runner imports it
in MiDojo's interpreter and calls ``register(judge_url, judge_model)`` before it builds
the episode, since an episode parses its checks when it is built. It imports the standard
library and its sibling ``asago_rubric``; ``midojo.verifiers`` is imported when the
verifier is registered or asked for a result.

MiDojo grades once, after the last turn, and hands a verifier the conversation
(``VerificationContext.turns``, each user message and the agent's reply) and the recorded
calls, each naming the turn it was made in (``turn``, from 0). The judge sees what Garak's
``RubricJudge`` sees for the same turn: ``user_text`` is that turn's message, ``history``
the earlier messages and replies interleaved (``{"role": "user"|"assistant", "content":
text}``), the graded output is that turn's reply, and ``tool_calls`` every call made up to
and including that turn. With ``judge_turns`` ``final`` the judge grades the last turn;
with ``all`` it grades every turn in order, as Garak does, and the last turn's verdict
grades the conversation. A one-turn episode is graded as Garak grades a single attempt.

A MiDojo check is three-valued: ``supported`` passes the check (the attack succeeded),
``contradicted`` fails it, and ``unresolved`` is unknown (None), so MiDojo marks the grade
invalid rather than failed. The check names the user turns the episode sends. A
conversation that does not hold exactly those messages, each with a reply, or a call
without a turn the judge can place, is unknown, without a judge request. Recorded calls
that MiDojo holds as incomplete (an unfinished call, or ``function_calls`` not complete in
the evidence status) make the evidence ``partial`` for the judge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# In an episode's verifiers package the rubric judge sits beside this file as
# ``asago_rubric.py``; in the adapter it is that authoring module.
try:
    from .asago_rubric import (  # type: ignore[import-not-found]
        RUBRIC_NAME,
        _record,
        build_evidence,
        field,
        http_judge,
        is_complete,
        judge_output,
    )
except ModuleNotFoundError as exc:
    if exc.name != f"{__package__}.asago_rubric":
        raise
    from asago_artifact_generator.authoring.rubric_judge import (
        RUBRIC_NAME,
        _record,
        build_evidence,
        field,
        http_judge,
        is_complete,
        judge_output,
    )

EPISODE_RUBRIC_NAME = f"{RUBRIC_NAME}_episode"
SPEC_KEYS = frozenset({"rubric", "turns", "judge_turns"})
FINAL_TURN = "final"
ALL_TURNS = "all"
JUDGE_TURNS = frozenset({FINAL_TURN, ALL_TURNS})
# The check value of each judge verdict; ``unresolved`` is unknown.
PASSED = {"supported": True, "contradicted": False}
# Why a conversation is not graded.
TURNS_MISSING = "judge_turns_missing"
TURN_COUNT_MISMATCH = "judge_turn_count_mismatch"
TURN_MESSAGE_MISMATCH = "judge_turn_message_mismatch"
TURN_REPLY_MISSING = "judge_turn_reply_missing"
CALL_TURN_INVALID = "judge_call_turn_invalid"

JudgeCall = Callable[[list[dict[str, str]]], Any]


@dataclass(frozen=True)
class EpisodeCheck:
    """A parsed spec: the rubric, the user turns the episode sends, and the judged turns."""

    rubric: Mapping[str, Any]
    turns: tuple[str, ...]
    judge_turns: str


@dataclass(frozen=True)
class Turn:
    """One turn of the conversation MiDojo recorded: the user message and the agent's reply."""

    message: str
    reply: str


def parse_spec(spec: Any) -> EpisodeCheck:
    """Return the check of a spec, or raise ``ValueError``."""

    if not isinstance(spec, Mapping) or set(spec) != SPEC_KEYS:
        raise ValueError(f"{EPISODE_RUBRIC_NAME} needs exactly the keys {sorted(SPEC_KEYS)}")
    rubric = spec["rubric"]
    if not isinstance(rubric, Mapping) or not isinstance(rubric.get("question"), str):
        raise ValueError(f"{EPISODE_RUBRIC_NAME} rubric has no question text")
    turns = spec["turns"]
    if (
        isinstance(turns, str)
        or not isinstance(turns, Sequence)
        or not turns
        or not all(isinstance(turn, str) and turn for turn in turns)
    ):
        raise ValueError(f"{EPISODE_RUBRIC_NAME} turns are not a list of user texts")
    if spec["judge_turns"] not in JUDGE_TURNS:
        raise ValueError(
            f"{EPISODE_RUBRIC_NAME} judge_turns is one of {sorted(JUDGE_TURNS)}, "
            f"not {spec['judge_turns']!r}"
        )
    return EpisodeCheck(_plain(rubric), tuple(turns), spec["judge_turns"])


def _plain(node: Any) -> Any:
    """Return ``node`` as dicts and lists: MiDojo hands a check its spec frozen."""

    if isinstance(node, Mapping):
        return {key: _plain(value) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        return [_plain(value) for value in node]
    return node


def conversation(check: EpisodeCheck, ctx: Any) -> list[Turn] | str:
    """Return the recorded turns when they are the episode's, each replied, else why not.

    Garak grades no turn of a conversation whose turn got no reply text, so neither does
    this judge.
    """

    recorded = getattr(ctx, "turns", None)
    if not recorded:
        return TURNS_MISSING
    if len(recorded) != len(check.turns):
        return TURN_COUNT_MISMATCH
    messages = [field(turn, "message") for turn in recorded]
    if messages != list(check.turns):
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


def calls_complete(calls: Sequence[Any], ctx: Any) -> bool:
    """Say whether the calls are every call, each finished and none unsettled."""

    complete = getattr(ctx, "function_calls_complete", True)
    return (
        complete is True
        and is_complete(calls)
        and all(field(call, "complete") is not False for call in calls)
    )


class EpisodeRubricJudgeVerifier:
    """Judge the judged turns' replies with the rubric, each with the conversation before it."""

    name = EPISODE_RUBRIC_NAME

    def __init__(self, judge: JudgeCall) -> None:
        self._judge = judge

    def parse(self, spec: Any) -> EpisodeCheck:
        return parse_spec(spec)

    def judge(self, check: EpisodeCheck, ctx: Any) -> list[dict[str, Any]]:
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
        complete = calls_complete(calls, ctx)
        records = []
        for index in judged:
            made = (
                calls
                if index == count - 1 or placed is None
                else [c for c, turn in zip(calls, placed, strict=True) if turn <= index]
            )
            records.append(judge_turn(check.rubric, turns, index, made, self._judge, complete))
        return records

    def assess(self, check: EpisodeCheck, ctx: Any) -> Any:
        """Pass on ``supported``, fail on ``contradicted``, and leave ``unresolved`` unknown."""

        from midojo.verifiers import VerificationResult

        record = self.judge(check, ctx)[-1]
        passed = PASSED.get(record["verdict"])
        if passed is None:
            return VerificationResult.unknown(EPISODE_RUBRIC_NAME, record["reason"])
        return VerificationResult(passed, record["reason"])

    def evaluate(self, check: EpisodeCheck, ctx: Any) -> bool | None:
        return self.assess(check, ctx).passed


def judge_turn(
    rubric: Mapping[str, Any],
    turns: Sequence[Turn],
    index: int,
    calls: Sequence[Any],
    call: JudgeCall,
    complete: bool,
) -> dict[str, Any]:
    """Judge turn ``index``'s reply with the conversation before it and the calls so far."""

    evidence = build_evidence(
        turns[index].message,
        history_before(turns, index),
        turns[index].reply,
        calls,
        complete=complete,
    )
    return judge_output(rubric, evidence, call)


def register(judge_url: str, judge_model: str) -> None:
    """Register the verifier, judging with ``judge_model`` at ``judge_url``; once per process."""

    from midojo.verifiers import register_verifier

    try:
        register_verifier(EpisodeRubricJudgeVerifier(http_judge(judge_url, judge_model)))
    except ValueError:
        pass
