"""parse: a sealed snapshot and its verdict become an execution receipt.

Every snapshot and verdict under ``fixtures/episode/<case>/<scenario>/`` is what MiDojo
wrote at b6e0f8f when it ran that case's episode with a scripted agent and graded it with
``midojo.grade`` (``fixtures/episode/generate.py``); ``recording.json`` lists the security
grade each got.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from episode_cases import EPISODES, SEQUENTIAL_TURNS, SINGLE_TURNS

from asago_bundle_midojo.sealed import ParseError, compiled_digest, parse_sealed
from conftest import RECEIPT_SCHEMA

SCHEMA = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
RECORDING = json.loads((EPISODES / "recording.json").read_text(encoding="utf-8"))
SCENARIOS = [
    pytest.param(case, scenario, id=f"{case}/{scenario}")
    for case, scenarios in sorted(RECORDING["security"].items())
    for scenario in sorted(scenarios)
]
OUTCOMES = {True: "detected", False: "not_detected", None: "inconclusive"}


def recorded(case: str, scenario: str, name: str) -> Any:
    return json.loads((EPISODES / case / scenario / name).read_text(encoding="utf-8"))


def run_dir(
    tmp_path: Path,
    case: str,
    scenario: str,
    *,
    snapshot: Any = None,
    verdict: Any = None,
) -> Path:
    """Copy a recorded run into ``tmp_path/run``; ``snapshot`` or ``verdict`` replace its own."""

    run = tmp_path / "run"
    run.mkdir()
    for name, replacement in (("snapshot.json", snapshot), ("verdict.json", verdict)):
        if replacement is None:
            shutil.copy(EPISODES / case / scenario / name, run / name)
        else:
            (run / name).write_text(json.dumps(replacement), encoding="utf-8")
    return run


def parsed(tmp_path: Path, case: str, scenario: str, **changes: Any) -> dict[str, Any]:
    run = run_dir(tmp_path, case, scenario, **changes)
    receipt = parse_sealed(
        EPISODES / case,
        run / "snapshot.json",
        run / "verdict.json",
        run / "receipt.json",
        tool_revision="b6e0f8fb",
        adapter_revision="abc123",
    )
    assert receipt == json.loads((run / "receipt.json").read_text(encoding="utf-8"))
    jsonschema.validate(receipt, SCHEMA)
    return receipt


@pytest.mark.parametrize(("case", "scenario"), SCENARIOS)
def test_each_recorded_run_gives_a_valid_receipt_with_midojo_s_three_valued_grade(
    tmp_path: Path, case: str, scenario: str
) -> None:
    security = RECORDING["security"][case][scenario]

    receipt = parsed(tmp_path, case, scenario)

    (attempt,) = receipt["attempts"]
    assert attempt["result"]["outcome"] == OUTCOMES[security]
    assert [item["outcome"] for item in attempt["detections"]] == [OUTCOMES[security]]
    assert receipt["episode"]["verdict"]["security"] is security


@pytest.mark.parametrize(("case", "scenario"), SCENARIOS)
def test_the_receipt_names_the_evaluation_and_the_snapshot_graded(
    tmp_path: Path, case: str, scenario: str
) -> None:
    snapshot = recorded(case, scenario, "snapshot.json")
    verdict = recorded(case, scenario, "verdict.json")

    episode = parsed(tmp_path, case, scenario)["episode"]

    assert episode["evaluation_id"] == snapshot["evaluation_id"] == verdict["evaluation_id"]
    assert episode["snapshot_id"] == snapshot["snapshot_id"] == verdict["snapshot_id"]
    assert episode["snapshot_id"].startswith("sha256:")
    assert episode["evidence_status"] == snapshot["evidence_status"]
    assert episode["verdict"] == {
        "evaluation_id": verdict["evaluation_id"],
        "snapshot_id": verdict["snapshot_id"],
        "security": verdict["grade"]["security"],
        "reason": verdict["grade"]["security_reason"],
        "validity": verdict["grade"]["grade_validity"]["security"]["status"],
    }


def test_a_detected_run_completes_with_the_calls_and_the_reply(tmp_path: Path) -> None:
    receipt = parsed(tmp_path, "command-single", "detected")

    assert (receipt["execution_status"], receipt["runtime_status"]) == ("completed", "completed")
    assert receipt["incomplete_reason"] is None
    assert receipt["tool"] == {
        "id": "midojo",
        "revision": "b6e0f8fb",
        "adapter_revision": "abc123",
    }
    assert receipt["package"]["id"] == "SCN-101"
    (attempt,) = receipt["attempts"]
    assert attempt["result"] == {
        "outcome": "detected",
        "claim_level": "command_attempt",
        "reason": "the condition holds on the captured calls",
    }
    assert attempt["detections"] == [
        {
            "detector": "ToolCallCondition",
            "outcome": "detected",
            "reason": "the condition holds on the captured calls",
            "matched_calls": None,
        }
    ]
    assert attempt["judge"] is None
    assert [call["name"] for call in attempt["observation"]["tool_calls"]] == [
        "lookup_order",
        "schedule_payment",
    ]
    assert attempt["observation"]["tool_calls"][1] == {
        "name": "schedule_payment",
        "arguments": {"plan_id": "PLAN-21", "due_date": "2026-12-01"},
        "result": {"status": "scheduled"},
        "error": None,
        "status": None,
        "turn_index": 0,
    }
    assert attempt["observation"]["assistant_messages"] == ["I moved PLAN-21."]
    assert attempt["observation"]["source"] == "tool_native"
    assert attempt["turns"] == [
        {
            "index": 0,
            "request": {"messages": [{"role": "user", "content": SINGLE_TURNS[0]}]},
            "response": {"assistant_messages": ["I moved PLAN-21."], "tool_call_indices": [0, 1]},
        }
    ]


def test_an_undecided_check_stays_inconclusive_and_the_verdict_stays_unknown(
    tmp_path: Path,
) -> None:
    receipt = parsed(tmp_path, "command-single", "unfinished")

    (attempt,) = receipt["attempts"]
    assert receipt["execution_status"] == "completed"
    assert attempt["result"]["outcome"] == "inconclusive"
    assert receipt["episode"]["verdict"]["security"] is None
    assert receipt["episode"]["verdict"]["validity"] == "invalid"
    unfinished = attempt["observation"]["tool_calls"][1]
    assert (unfinished["name"], unfinished["status"], unfinished["result"]) == (
        "schedule_payment",
        "unfinished",
        None,
    )


def test_an_interrupted_run_fails_its_turn_and_keeps_midojo_s_unknown_verdict(
    tmp_path: Path,
) -> None:
    receipt = parsed(tmp_path, "command-single", "interrupted")

    assert (receipt["execution_status"], receipt["runtime_status"]) == ("failed", "failed")
    assert receipt["incomplete_reason"] == "turn_failed:1"
    (attempt,) = receipt["attempts"]
    assert attempt["result"]["outcome"] == "inconclusive"
    assert attempt["generation"]["response_status"] is None
    assert receipt["episode"]["evidence_status"] == {
        "agent_output": "incomplete",
        "function_calls": "incomplete",
        "turns": "incomplete",
    }


def test_a_sequential_run_places_each_call_in_its_turn(tmp_path: Path) -> None:
    (attempt,) = parsed(tmp_path, "command-sequential", "detected")["attempts"]

    assert [turn["request"]["messages"][0]["content"] for turn in attempt["turns"]] == list(
        SEQUENTIAL_TURNS
    )
    assert [turn["response"]["tool_call_indices"] for turn in attempt["turns"]] == [[0], [1]]
    assert [call["turn_index"] for call in attempt["observation"]["tool_calls"]] == [0, 1]
    assert attempt["observation"]["assistant_messages"] == ["Found ORD-101.", "I moved PLAN-21."]


@pytest.mark.parametrize(
    ("scenario", "verdict", "outcome"),
    [("supported", "supported", "detected"), ("unresolved", "unresolved", "inconclusive")],
)
def test_a_reply_run_has_the_judge_record_the_grade_stands_for(
    tmp_path: Path, scenario: str, verdict: str, outcome: str
) -> None:
    grade = recorded("reply-single", scenario, "verdict.json")["grade"]

    (attempt,) = parsed(tmp_path, "reply-single", scenario)["attempts"]

    assert attempt["result"]["outcome"] == outcome
    assert attempt["detections"][0]["detector"] == "RubricJudge"
    assert attempt["judge"] == {
        "verdict": verdict,
        "reason": grade["security_reason"],
        "request_count": None,
    }


def test_the_native_files_are_the_snapshot_and_the_verdict(tmp_path: Path) -> None:
    receipt = parsed(tmp_path, "command-single", "detected")

    assert [item["path"] for item in receipt["native"]] == ["snapshot.json", "verdict.json"]
    assert receipt["bundle_digest"] == compiled_digest(EPISODES / "command-single")


def with_grade(case: str, scenario: str, **grade: Any) -> dict[str, Any]:
    verdict = recorded(case, scenario, "verdict.json")
    return {**verdict, "grade": {**verdict["grade"], **grade}}


def invalid(reason: str) -> dict[str, Any]:
    return {"status": "invalid", "reasons": [{"check": "tool_call_condition", "reason": reason}]}


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {
                "verdict": {
                    **recorded("command-single", "detected", "verdict.json"),
                    "evaluation_id": "other",
                }
            },
            "evaluation_mismatch",
        ),
        (
            {
                "verdict": recorded("command-single", "not-detected", "verdict.json")
                | {
                    "evaluation_id": recorded("command-single", "detected", "snapshot.json")[
                        "evaluation_id"
                    ]
                }
            },
            "snapshot_mismatch",
        ),
        (
            {
                "snapshot": recorded("command-single", "detected", "snapshot.json")
                | {"evidence_status": {"function_calls": "incomplete"}}
            },
            "evidence_status_mismatch",
        ),
        (
            {
                "snapshot": recorded("command-single", "detected", "snapshot.json")
                | {
                    "turns": [
                        {
                            **recorded("command-single", "detected", "snapshot.json")["turns"][0],
                            "message": "Something else.",
                        }
                    ]
                }
            },
            "turn_input_mismatch",
        ),
        (
            {
                "snapshot": recorded("command-single", "detected", "snapshot.json")
                | {"pre_environment": {"orders": {}}}
            },
            "seed_not_provisioned",
        ),
        (
            {
                "verdict": with_grade(
                    "command-single",
                    "detected",
                    grade_validity={"security": invalid("x"), "utility": {"status": "valid"}},
                )
            },
            "verdict_malformed",
        ),
        (
            {"verdict": with_grade("command-single", "unfinished", security=False)},
            "verdict_malformed",
        ),
        ({"verdict": {"grade": {}}}, "verdict_malformed"),
        ({"snapshot": {"evaluation_id": "x"}}, "snapshot_malformed"),
    ],
    ids=[
        "evaluation",
        "snapshot",
        "evidence",
        "turn-message",
        "seed",
        "unknown-claimed-decided",
        "decided-claimed-unknown",
        "verdict-shape",
        "snapshot-shape",
    ],
)
def test_a_sealed_result_that_does_not_hold_together_fails_the_receipt(
    tmp_path: Path, changes: dict[str, Any], reason: str
) -> None:
    receipt = parsed(tmp_path, "command-single", "detected", **changes)

    assert (receipt["execution_status"], receipt["incomplete_reason"]) == ("failed", reason)
    assert receipt["attempts"] == []
    assert receipt["runtime_status"] is None


def test_a_call_outside_the_turns_fails_the_receipt(tmp_path: Path) -> None:
    snapshot = recorded("command-single", "detected", "snapshot.json")
    snapshot["function_calls"][0]["turn"] = 3

    receipt = parsed(tmp_path, "command-single", "detected", snapshot=snapshot)

    assert receipt["incomplete_reason"] == "call_turn_out_of_range"


def test_missing_files_fail_the_receipt_and_name_what_is_known(tmp_path: Path) -> None:
    run = run_dir(tmp_path, "command-single", "detected")
    (run / "verdict.json").unlink()

    receipt = parse_sealed(
        EPISODES / "command-single", run / "snapshot.json", run / "verdict.json", run / "r.json"
    )

    jsonschema.validate(receipt, SCHEMA)
    assert receipt["incomplete_reason"] == "verdict_missing"
    assert receipt["episode"]["verdict"] is None
    assert (
        receipt["episode"]["snapshot_id"]
        == recorded("command-single", "detected", "snapshot.json")["snapshot_id"]
    )
    assert [item["path"] for item in receipt["native"]] == ["snapshot.json"]


def test_an_episode_directory_without_an_episode_cannot_be_parsed(tmp_path: Path) -> None:
    run = run_dir(tmp_path, "command-single", "detected")

    with pytest.raises(ParseError, match="midojo-episode-v1"):
        parse_sealed(tmp_path, run / "snapshot.json", run / "verdict.json", run / "r.json")


def test_native_files_outside_the_receipt_directory_are_refused(tmp_path: Path) -> None:
    run = run_dir(tmp_path, "command-single", "detected")

    with pytest.raises(ParseError, match="inside the receipt's directory"):
        parse_sealed(
            EPISODES / "command-single",
            run / "snapshot.json",
            run / "verdict.json",
            tmp_path / "elsewhere" / "r.json",
        )
