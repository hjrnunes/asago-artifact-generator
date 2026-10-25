"""The rubric judge of a reply episode: Garak's requests, and a three-valued check.

``fixtures/rubric/turns.json`` holds two- and three-turn conversations as MiDojo hands a
verifier and every judge request Garak's ``SequentialInjection`` and ``RubricJudge`` made
for them (``generate_turns.py``, Garak at 968cc22). The episode's judge must send the same
requests for ``judge_turns`` ``final`` and ``all``. MiDojo itself grades the verdicts
(``fixtures/episode/reply-single``): ``supported`` passes the check and ``unresolved`` is
unknown. The Garak runs had runtime facts and an
episode carries none, so the comparison gives the check's rubric the facts those runs had.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from asago_bundle_core.testing import JUDGE
from asago_bundle_midojo.bundle_files import asago_episode_judge as judge

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/rubric/turns.json").read_text(encoding="utf-8")
)
CASES = [pytest.param(case, id=case["id"]) for case in FIXTURE["cases"]]
MODES = ["final", "all"]
SUPPORTED = json.dumps({"verdict": "supported", "evidence_refs": ["messages[0].content"]})
CONTRADICTED = json.dumps({"verdict": "contradicted", "evidence_refs": ["messages[0].content"]})


class Judge:
    """A scripted judge call that keeps the messages it was sent."""

    def __init__(self, reply: Any = SUPPORTED) -> None:
        self.reply = reply
        self.sent: list[list[dict[str, str]]] = []

    def __call__(self, messages: list[dict[str, str]]) -> Any:
        self.sent.append(messages)
        return self.reply


def spec(turns: list[str], judge_turns: str = "final", **rubric: Any) -> dict[str, Any]:
    return {"rubric": {**JUDGE, **rubric}, "turns": turns, "judge_turns": judge_turns}


def context(
    turns: list[dict[str, Any]], calls: list[Any] | None = None, complete: bool = True
) -> SimpleNamespace:
    return SimpleNamespace(
        turns=turns,
        function_calls=calls or [],
        agent_output=turns[-1]["reply"] if turns else "",
        function_calls_complete=complete,
    )


def one_turn(reply: str = "PLAN-21 moved.") -> list[dict[str, Any]]:
    return [{"message": "Show my order.", "reply": reply}]


@pytest.mark.parametrize("judge_turns", MODES)
@pytest.mark.parametrize("case", CASES)
def test_the_episode_judge_sends_garak_s_requests(case: dict[str, Any], judge_turns: str) -> None:
    garak = case["garak"][judge_turns]
    call = Judge()
    verifier = judge.EpisodeRubricJudgeVerifier(call)
    check = verifier.parse(
        spec(
            case["user_turns"],
            judge_turns,
            runtime_facts=case["values"]["judge_runtime_facts"],
        )
    )

    records = verifier.judge(
        check, context(case["midojo"]["turns"], case["midojo"]["function_calls"])
    )

    assert [json.loads(messages[1]["content"]) for messages in call.sent] == garak["requests"]
    assert [m for messages in call.sent for m in messages] == [
        m for body in garak["http_bodies"] for m in body["messages"]
    ]
    assert records[-1] == garak["final"]
    assert len(call.sent) == garak["request_count"]


@pytest.mark.parametrize(
    ("reply", "passed"),
    [(SUPPORTED, True), (CONTRADICTED, False), ("not json", None)],
)
def test_the_verdict_is_three_valued(reply: str, passed: bool | None) -> None:
    verifier = judge.EpisodeRubricJudgeVerifier(Judge(reply))
    check = verifier.parse(spec(["Show my order."]))

    (record,) = verifier.judge(check, context(one_turn()))

    assert judge.PASSED.get(record["verdict"]) is passed
    assert (
        record["verdict"] == {True: "supported", False: "contradicted", None: "unresolved"}[passed]
    )


@pytest.mark.parametrize(
    ("turns", "reason"),
    [
        ([], judge.TURNS_MISSING),
        (one_turn() * 2, judge.TURN_COUNT_MISMATCH),
        ([{"message": "Something else.", "reply": "ok"}], judge.TURN_MESSAGE_MISMATCH),
        ([{"message": "Show my order.", "reply": None}], judge.TURN_REPLY_MISSING),
    ],
)
def test_a_conversation_other_than_the_episode_s_is_unresolved_without_a_request(
    turns: list[dict[str, Any]], reason: str
) -> None:
    call = Judge()
    verifier = judge.EpisodeRubricJudgeVerifier(call)

    (record,) = verifier.judge(verifier.parse(spec(["Show my order."])), context(turns))

    assert (record["verdict"], record["reason"]) == ("unresolved", reason)
    assert call.sent == []


def test_all_turns_needs_every_call_placed_in_a_turn() -> None:
    call = Judge()
    verifier = judge.EpisodeRubricJudgeVerifier(call)
    turns = [{"message": "One.", "reply": "a"}, {"message": "Two.", "reply": "b"}]
    calls = [{"function": "lookup_order", "args": {}, "result": "{}", "turn": None}]

    (record,) = verifier.judge(
        verifier.parse(spec(["One.", "Two."], "all")), context(turns, calls)
    )

    assert (record["verdict"], record["reason"]) == ("unresolved", judge.CALL_TURN_INVALID)
    assert call.sent == []


@pytest.mark.parametrize(
    ("calls", "complete"),
    [
        ([{"function": "f", "args": {}, "result": "{}", "complete": False}], True),
        ([{"function": "f", "args": {}, "result": "{}"}], False),
        ([{"function": "f", "args": {}, "result": None, "error": "no_response"}], True),
    ],
    ids=["unfinished-call", "incomplete-capture", "unsettled-call"],
)
def test_calls_midojo_holds_as_incomplete_make_the_evidence_partial(
    calls: list[dict[str, Any]], complete: bool
) -> None:
    call = Judge()
    verifier = judge.EpisodeRubricJudgeVerifier(call)

    verifier.judge(verifier.parse(spec(["Show my order."])), context(one_turn(), calls, complete))

    (request,) = [json.loads(messages[1]["content"]) for messages in call.sent]
    assert request["evidence"]["completeness"] == {"messages": "partial", "tool_calls": "partial"}


def test_complete_calls_leave_the_evidence_complete() -> None:
    call = Judge()
    verifier = judge.EpisodeRubricJudgeVerifier(call)
    calls = [{"function": "f", "args": {}, "result": "{}", "complete": True, "turn": 0}]

    verifier.judge(verifier.parse(spec(["Show my order."])), context(one_turn(), calls))

    (request,) = [json.loads(messages[1]["content"]) for messages in call.sent]
    assert request["evidence"]["completeness"] == {
        "messages": "complete",
        "tool_calls": "complete",
    }


@pytest.mark.parametrize(
    "bad",
    [
        {"rubric": JUDGE, "turns": ["x"]},
        {**spec(["x"]), "judge_url": "http://judge"},
        {**spec(["x"]), "rubric": {"criteria": []}},
        {**spec(["x"]), "turns": "x"},
        {**spec(["x"]), "turns": []},
        {**spec(["x"]), "turns": [""]},
        {**spec(["x"]), "judge_turns": "some"},
    ],
)
def test_a_spec_that_is_not_an_episode_check_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=judge.EPISODE_RUBRIC_NAME):
        judge.parse_spec(bad)


def test_the_check_is_named_after_the_rubric_judge() -> None:
    assert judge.EPISODE_RUBRIC_NAME == "asago_rubric_judge_episode"
    assert judge.SPEC_KEYS == {"rubric", "turns", "judge_turns"}
