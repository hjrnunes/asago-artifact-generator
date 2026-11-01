"""A reply episode graded by MiDojo's ``reply_judge`` through the judge service.

``fixtures/reply-judge-4e5176a/`` holds the reply case as compile writes it and what MiDojo
4e5176a made of it (``tools/fixture-generators/reply_judge_4e5176a.py``): it built the
episode, then graded each sealed reply
run with ``midojo.grade``, its ``reply_judge`` check asking this adapter's ``judge-serve``
in front of a fake model on 127.0.0.1. Each verdict carries MiDojo's ``judgements``: the
``midojo.judge.v1`` request MiDojo sent and the service's answer; ``model-requests.json``
the bodies the fake model got. Those runs were sealed before MiDojo recorded delivery
facts, so their grades state no delivery: the receipt cannot read any of them as a
resistance.

``fixtures/reply-judge-246dc73/`` holds one reply run sealed at 246dc73, which records the
delivery (``tools/fixture-generators/reply_judge_delivered.py``): the same episode run again
through the host interceptor, then graded with the judge answering ``contradicted``. Its
``injection_delivered`` is true, so the receipt reads the decided resistance.
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
from asago_bundle_midojo.sealed import INCONCLUSIVE, UNRECORDED_DELIVERY, parse_sealed
from conftest import RECEIPT_SCHEMA

GRADED = FIXTURES / "reply-judge-4e5176a"
DELIVERED = FIXTURES / "reply-judge-246dc73"
RECORDING = json.loads((GRADED / "recording.json").read_text(encoding="utf-8"))
# scenario -> (security, the model's reply)
SCENARIOS = {
    "supported": (True, SUPPORTED),
    "contradicted": (False, CONTRADICTED),
    "unresolved": (None, "not json"),
    "blocked-call": (True, SUPPORTED),
    "deep-args": (True, SUPPORTED),
}
# scenario -> (the receipt's outcome, the judge's verdict). A reply episode's false grade
# counts as a resistance only when MiDojo recorded that the payload was delivered. These
# runs were sealed before it recorded delivery facts, so their contradicted grade reads
# inconclusive here: the recording states no delivery, and the episode did carry an injection.
EXPECTED = {
    "supported": ("detected", "supported"),
    "contradicted": ("inconclusive", "unresolved"),
    "unresolved": ("inconclusive", "unresolved"),
    "blocked-call": ("detected", "supported"),
    "deep-args": ("detected", "supported"),
}


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
    document = _read(GRADED / "episode.json")
    assert document["tool_revision_range"] == ">=8e8208f"
    assert set(document["episode"]["security"]) == {"reply_judge"}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_a_verdict_with_judgements_parses_to_the_outcome_its_grade_and_delivery_owe(
    tmp_path: Path, scenario: str
) -> None:
    security, _ = SCENARIOS[scenario]
    outcome, word = EXPECTED[scenario]
    verdict = _read(GRADED / scenario / "verdict.json")

    receipt = parsed(tmp_path, scenario)

    jsonschema.validate(receipt, json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8")))
    assert receipt["incomplete_reason"] is None
    (attempt,) = receipt["attempts"]
    assert verdict["grade"]["security"] is security
    assert attempt["result"]["outcome"] == outcome
    assert attempt["judge"] == {
        "verdict": word,
        "reason": verdict["grade"]["security_reason"],
        "request_count": None,
    }
    assert "judgements" not in json.dumps(receipt)


def test_a_contradicted_reply_says_its_delivery_is_unrecorded_and_reads_inconclusive(
    tmp_path: Path,
) -> None:
    """The grade contradicts the judge's claim, and nothing records a delivery.

    MiDojo sealed this run before it recorded delivery facts, so the receipt cannot conclude
    the agent resisted: its outcome is inconclusive, its judge verdict unresolved, and its
    reason names the recording rather than claiming the episode carried no injection.
    """

    verdict = _read(GRADED / "contradicted" / "verdict.json")
    assert verdict["grade"]["security"] is False
    assert verdict["grade"]["grade_validity"]["security"]["status"] == "valid"
    assert verdict["grade"].get("injection_delivered") is not True

    receipt = parsed(tmp_path, "contradicted")

    (attempt,) = receipt["attempts"]
    assert attempt["result"]["outcome"] == INCONCLUSIVE
    named = f"{verdict['grade']['security_reason']}; {UNRECORDED_DELIVERY}"
    assert attempt["result"]["reason"] == named
    assert attempt["judge"]["verdict"] == "unresolved"
    # MiDojo's own grade stays in the episode block, decided as it decided.
    assert receipt["episode"]["verdict"]["security"] is False
    assert receipt["episode"]["verdict"]["injection_delivered"] is None


def test_a_reply_run_sealed_with_delivery_facts_reads_contradicted(tmp_path: Path) -> None:
    """A decided resistance the delivery facts establish keeps its own outcome and verdict.

    The judge contradicted the claim and MiDojo recorded the payload as emitted, so nothing
    is unknown here: the receipt reads ``not_detected`` and the judge's word ``contradicted``.
    """

    recording = _read(DELIVERED / "recording.json")
    verdict = _read(DELIVERED / "contradicted" / "verdict.json")
    assert verdict["grade"]["security"] is False
    assert verdict["grade"]["grade_validity"]["security"]["status"] == "valid"
    assert verdict["grade"]["injection_delivered"] is True
    assert recording["scenarios"]["contradicted"]["injection_delivered"] is True

    run = tmp_path / "run"
    run.mkdir()
    for name in ("snapshot.json", "verdict.json"):
        (run / name).write_bytes((DELIVERED / "contradicted" / name).read_bytes())

    receipt = parse_sealed(
        DELIVERED, run / "snapshot.json", run / "verdict.json", run / "receipt.json"
    )

    jsonschema.validate(receipt, json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8")))
    assert receipt["incomplete_reason"] is None
    (attempt,) = receipt["attempts"]
    assert attempt["result"] == {
        "outcome": "not_detected",
        "claim_level": "reply",
        "reason": verdict["grade"]["security_reason"],
    }
    assert attempt["judge"]["verdict"] == "contradicted"
    assert receipt["episode"]["verdict"]["injection_delivered"] is True
    assert [call["name"] for call in attempt["observation"]["tool_calls"]] == [
        "list_notes",
        "withdraw",
    ]


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
