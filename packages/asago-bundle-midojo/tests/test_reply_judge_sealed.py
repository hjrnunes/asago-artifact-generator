"""A reply episode graded by MiDojo's ``reply_judge`` through the judge service.

``fixtures/reply-judge-4e5176a/`` holds the reply case as compile writes it and what MiDojo
4e5176a made of it (``generate.py``): it built the episode, then graded each sealed reply
run with ``midojo.grade``, its ``reply_judge`` check asking this adapter's ``judge-serve``
in front of a fake model on 127.0.0.1. Each verdict carries MiDojo's ``judgements``: the
``midojo.judge.v1`` request MiDojo sent and the service's answer; ``model-requests.json``
the bodies the fake model got.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from episode_cases import FIXTURES
from judge_cases import (
    CONTRADICTED,
    SUPPORTED,
    Model,
    completion,
    judge_service,
    midojo_request,
    model_server,
    post,
)

from asago_bundle_midojo.judge_service import SCHEMA
from asago_bundle_midojo.sealed import parse_sealed
from conftest import RECEIPT_SCHEMA

GRADED = FIXTURES / "reply-judge-4e5176a"
RECORDING = json.loads((GRADED / "recording.json").read_text(encoding="utf-8"))
# scenario -> (security, the model's reply)
SCENARIOS = {
    "supported": (True, SUPPORTED),
    "contradicted": (False, CONTRADICTED),
    "unresolved": (None, "not json"),
    "blocked-call": (True, SUPPORTED),
    "deep-args": (True, SUPPORTED),
}
OUTCOMES = {True: "detected", False: "not_detected", None: "inconclusive"}
VERDICTS = {True: "supported", False: "contradicted", None: "unresolved"}


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parsed(tmp_path: Path, scenario: str, verdict: Any = None) -> dict[str, Any]:
    """Parse a recorded run copied beside the receipt; ``verdict`` replaces its own."""

    run = tmp_path / "run"
    run.mkdir()
    for name in ("snapshot.json", "verdict.json"):
        (run / name).write_bytes((GRADED / scenario / name).read_bytes())
    if verdict is not None:
        (run / "verdict.json").write_text(json.dumps(verdict), encoding="utf-8")
    return parse_sealed(GRADED, run / "snapshot.json", run / "verdict.json", run / "receipt.json")


def judgements(scenario: str) -> list[dict[str, Any]]:
    return _read(GRADED / scenario / "verdict.json")["grade"]["judgements"]


def test_the_fixture_was_graded_by_4e5176a_in_every_scenario() -> None:
    assert RECORDING["revision"].startswith("4e5176a")
    assert set(RECORDING["scenarios"]) == set(SCENARIOS)
    episode = _read(GRADED / "episode.json")
    assert episode["tool_revision_range"] == ">=cab2a35e"
    assert set(episode["episode"]["security"]) == {"reply_judge"}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_a_verdict_with_judgements_parses_to_the_grade_s_outcome(
    tmp_path: Path, scenario: str
) -> None:
    security, _ = SCENARIOS[scenario]
    verdict = _read(GRADED / scenario / "verdict.json")

    receipt = parsed(tmp_path, scenario)

    jsonschema.validate(receipt, json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8")))
    assert receipt["incomplete_reason"] is None
    (attempt,) = receipt["attempts"]
    assert verdict["grade"]["security"] is security
    assert attempt["result"]["outcome"] == OUTCOMES[security]
    assert attempt["judge"] == {
        "verdict": VERDICTS[security],
        "reason": verdict["grade"]["security_reason"],
        "request_count": None,
    }
    assert "judgements" not in json.dumps(receipt)


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_midojo_s_request_is_the_one_the_tests_build(scenario: str) -> None:
    """``judge_cases.midojo_request`` builds what MiDojo 4e5176a sent, ID and all."""

    episode = _read(GRADED / "episode.json")["episode"]
    spec = episode["security"]["reply_judge"]
    snapshot = _read(GRADED / scenario / "snapshot.json")
    calls = [call for call in snapshot["function_calls"]]
    (first, *rest) = judgements(scenario)

    built = midojo_request(spec["claim"], spec["rubric"], snapshot["turns"], calls)

    assert first["request"] == built
    assert first["request_id"] == built["id"]
    assert first["sent"] is True and first["http_status"] == 200 and first["failure"] is None
    if scenario == "blocked-call":
        (again,) = rest
        unblocked = [call for call in calls if not call["blocked"]]
        assert again["request"] == midojo_request(
            spec["claim"], spec["rubric"], snapshot["turns"], unblocked
        )
    else:
        assert rest == []


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_service_answers_midojo_s_request_as_it_did(scenario: str) -> None:
    """Asked MiDojo's recorded requests again, the service sends the model the recorded
    bodies and answers what MiDojo recorded."""

    _, reply = SCENARIOS[scenario]
    model = Model(body=completion(reply))
    with model_server(model) as url, judge_service(url) as judge:
        answers = [post(judge, item["request"]) for item in judgements(scenario)]

    recorded_bodies = _read(GRADED / scenario / "model-requests.json")
    assert [body["messages"] for body in model.bodies()] == [
        body["messages"] for body in recorded_bodies
    ]
    for (status, answer), item in zip(answers, judgements(scenario), strict=True):
        assert status == 200
        assert answer["schema"] == SCHEMA
        assert answer["id"] == item["request_id"]
        assert (answer["verdict"], answer["rationale"]) == (item["verdict"], item["rationale"])
        assert answer["details"] == item["details"]


def test_midojo_accepts_the_answer_to_a_request_with_deeply_nested_arguments() -> None:
    """The deep-args run's list_notes arguments nest 80 objects deep; the service's answer
    stays within MiDojo's nesting limit for details (64), so MiDojo keeps its verdict."""

    (item,) = judgements("deep-args")
    (call, *_) = item["request"]["tool_calls"]

    def depth(value: Any) -> int:
        return 1 + max(map(depth, value.values()), default=0) if isinstance(value, dict) else 0

    assert depth(call["args"]) > 64
    assert (item["failure"], item["verdict"]) == (None, "supported")
    assert _read(GRADED / "deep-args" / "verdict.json")["grade"]["security"] is True


def test_a_blocked_call_makes_midojo_ask_twice_and_the_service_answers_each() -> None:
    first, again = judgements("blocked-call")

    assert first["request_id"] != again["request_id"]
    assert [call["function"] for call in first["request"]["tool_calls"]] == [
        "list_notes",
        "withdraw",
    ]
    assert [call["function"] for call in again["request"]["tool_calls"]] == ["list_notes"]
    assert RECORDING["scenarios"]["blocked-call"]["model_requests"] == 2


def test_a_verdict_whose_judgements_are_not_a_list_of_objects_is_malformed(
    tmp_path: Path,
) -> None:
    verdict = _read(GRADED / "supported" / "verdict.json")
    verdict["grade"]["judgements"] = {"not": "a list"}

    receipt = parsed(tmp_path, "supported", verdict)

    assert receipt["incomplete_reason"] == "verdict_malformed"
