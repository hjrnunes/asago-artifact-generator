"""The sequential judge sends the judge requests Garak's judge sends for the same package.

``fixtures/rubric/turns.json`` holds a two-turn and a three-turn reply package's Garak
conversation entry (the Garak adapter's ``instantiate``), the replies and calls the target
was scripted to give, the same conversation as MiDojo hands a verifier, and, from
``generate_turns.py`` run with Garak at 968cc22, every judge request
``SequentialInjection`` and ``RubricJudge`` made for ``judge_turns`` ``final`` and ``all``.
The MiDojo bundle compiled from the same package must judge with the same requests: the
same ``user_text``, the earlier messages and replies interleaved as ``history``, the same
graded reply and the same tool calls, turn by turn, and the same request count.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_core.testing import write_test_package
from asago_bundle_garak.compiler import compile_package as compile_garak
from asago_bundle_garak.instantiate import instantiate_bundle as instantiate_garak
from asago_bundle_midojo.bundle_files import asago_rubric as port
from asago_bundle_midojo.bundle_files import asago_rubric_turns as rubric
from asago_bundle_midojo.compiler import compile_package
from asago_bundle_midojo.instantiate import instantiate_bundle
from conftest import compile_options

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/rubric/turns.json").read_text(encoding="utf-8")
)
CASES = [pytest.param(case, id=case["id"]) for case in FIXTURE["cases"]]
MODES = ["final", "all"]
SERVICE_PORT = 18123


def stimulus(texts: list[str]) -> dict[str, Any]:
    return {
        "delivery": "direct",
        "mode": "sequential",
        "turn_count": len(texts),
        "history": [{"role": "user", "content": text} for text in texts[:-1]],
        "slots": [],
        "user_text": texts[-1],
    }


def turn_values(texts: list[str]) -> dict[str, str]:
    return {f"user_turn_{number}": text for number, text in enumerate(texts, start=1)}


def package_of(tmp_path: Path, case: dict[str, Any]) -> Path:
    return write_test_package(
        tmp_path / "packages", claim_level="reply", stimulus=stimulus(case["user_turns"])
    )


def midojo_spec(tmp_path: Path, case: dict[str, Any], judge_turns: str) -> dict[str, Any]:
    """The rubric spec of the MiDojo bundle the case's package compiles to."""

    compile_package(package_of(tmp_path, case), tmp_path / "template", **compile_options())
    values = {
        **{key: case["values"][key] for key in ("gateway_url", "mcp_url", "model")},
        **{
            key: case["values"][key] for key in ("judge_url", "judge_model", "judge_runtime_facts")
        },
        **turn_values(case["user_turns"]),
        "service_port": SERVICE_PORT,
        "judge_turns": judge_turns,
    }
    instantiate_bundle(tmp_path / "template", values, tmp_path / "bundle")
    suite = json.loads((tmp_path / "bundle/asago_suite/suite.yaml").read_text(encoding="utf-8"))
    return suite["injection_tasks"][0]["security"][rubric.TURNS_RUBRIC_NAME]


class Context:
    """The verification context MiDojo's control plane builds for the case's conversation."""

    def __init__(self, inputs: dict[str, Any]) -> None:
        self.turns = inputs["turns"]
        self.function_calls = inputs["function_calls"]
        self.agent_output = inputs["turns"][-1]["reply"]


def judged(tmp_path: Path, case: dict[str, Any], judge_turns: str) -> dict[str, Any]:
    """Run the MiDojo verifier on the case; return its requests, messages, record and verdict."""

    spec = midojo_spec(tmp_path, case, judge_turns)
    sent: list[list[dict[str, str]]] = []

    def judge(_url: str, _model: str) -> Any:
        def call(messages: list[dict[str, str]]) -> str:
            sent.append(messages)
            return json.dumps({"verdict": "supported", "evidence_refs": ["messages[0].content"]})

        return call

    verifier = rubric.TurnsRubricJudgeVerifier(judge)
    record = verifier.grade(verifier.parse(spec), Context(case["midojo"]))
    verdict = json.loads(Path(spec["verdict_file"]).read_text(encoding="utf-8"))
    requests = [json.loads(messages[1]["content"]) for messages in sent]
    return {"requests": requests, "messages": sent, "record": record, "verdict": verdict}


def test_the_fixture_names_garak_s_probe_and_detector_and_both_modes() -> None:
    assert FIXTURE["implementation"]["revision"].startswith("968cc22")
    assert [len(case["user_turns"]) for case in FIXTURE["cases"]] == [2, 3]
    for case in FIXTURE["cases"]:
        assert set(case["garak"]) == set(MODES)


@pytest.mark.parametrize("case", CASES)
def test_the_garak_entry_is_the_one_the_garak_adapter_writes_for_the_package(
    tmp_path: Path, case: dict[str, Any]
) -> None:
    compile_garak(package_of(tmp_path, case), tmp_path / "template")
    values = {**case["values"], **turn_values(case["user_turns"]), "judge_turns": "final"}
    instantiate_garak(tmp_path / "template", values, tmp_path / "bundle")

    entry = json.loads((tmp_path / "bundle/conversations.jsonl").read_text(encoding="utf-8"))
    assert entry == case["garak_entry"]


@pytest.mark.parametrize("case", CASES)
def test_the_midojo_conversation_is_the_scripted_one(case: dict[str, Any]) -> None:
    turns = case["midojo"]["turns"]

    assert [turn["message"] for turn in turns] == case["user_turns"]
    assert [turn["reply"] for turn in turns] == [step["text"] for step in case["script"]]
    calls = case["midojo"]["function_calls"]
    assert [(call["function"], call["turn"]) for call in calls] == [
        (call["name"], index)
        for index, step in enumerate(case["script"])
        for call in step["calls"]
    ]


@pytest.mark.parametrize("judge_turns", MODES)
@pytest.mark.parametrize("case", CASES)
def test_the_midojo_judge_sends_garak_s_requests(
    tmp_path: Path, case: dict[str, Any], judge_turns: str
) -> None:
    garak = case["garak"][judge_turns]

    seen = judged(tmp_path, case, judge_turns)

    assert seen["requests"] == garak["requests"]
    assert [m for messages in seen["messages"] for m in messages] == [
        m for body in garak["http_bodies"] for m in body["messages"]
    ]


@pytest.mark.parametrize("judge_turns", MODES)
@pytest.mark.parametrize("case", CASES)
def test_the_midojo_verdict_is_garak_s_final_turn_with_garak_s_request_count(
    tmp_path: Path, case: dict[str, Any], judge_turns: str
) -> None:
    garak = case["garak"][judge_turns]

    seen = judged(tmp_path, case, judge_turns)

    assert seen["record"] == garak["final"]
    assert seen["verdict"]["verdict"] == garak["final"]["verdict"]
    assert seen["verdict"]["reason"] == garak["final"]["reason"]
    assert seen["verdict"]["evidence_refs"] == garak["final"]["evidence_refs"]
    assert seen["verdict"]["request_count"] == garak["request_count"]


@pytest.mark.parametrize("case", CASES)
def test_garak_s_history_interleaves_the_earlier_messages_and_replies(
    case: dict[str, Any],
) -> None:
    """What the comparison above pins, spelled out on Garak's final request."""

    (request,) = case["garak"]["final"]["requests"]
    turns = case["midojo"]["turns"]
    expected = [
        {"role": role, "content": turn[key]}
        for turn in turns[:-1]
        for role, key in (("user", "message"), ("assistant", "reply"))
    ]

    assert request["evidence"]["history"] == expected
    assert request["evidence"]["user_text"] == turns[-1]["message"]
    assert request["evidence"]["messages"] == [
        {"role": "assistant", "content": turns[-1]["reply"]}
    ]
    assert len(request["evidence"]["tool_calls"]) == len(case["midojo"]["function_calls"])


@pytest.mark.parametrize("case", CASES)
def test_garak_s_all_judges_each_turn_with_the_calls_made_so_far(case: dict[str, Any]) -> None:
    requests = case["garak"]["all"]["requests"]
    calls = case["midojo"]["function_calls"]

    assert len(requests) == len(case["user_turns"])
    for index, request in enumerate(requests):
        made = [port.tool_call_of(call) for call in calls if call["turn"] <= index]
        assert request["evidence"]["tool_calls"] == made
        assert request["evidence"]["user_text"] == case["user_turns"][index]
        assert len(request["evidence"]["history"]) == 2 * index
