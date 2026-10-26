"""parse: a sealed snapshot and its verdict become an execution receipt.

Every snapshot and verdict under ``fixtures/episode/<case>/<scenario>/`` is what MiDojo
wrote at f910063 when it ran that case's episode with its host interceptor in front of the
fixture target, a scripted agent calling the tools through the interceptor, and graded it
with ``midojo.grade`` (``fixtures/episode/generate.py``); ``recording.json`` lists the
security grade each got and the interceptor's injection outcomes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from episode_cases import EPISODES, SEQUENTIAL_TURNS, SINGLE_TURNS

from asago_bundle_midojo.route import ROUTES
from asago_bundle_midojo.sealed import ParseError, compiled_digest, parse_sealed
from asago_bundle_midojo.snapshot_digest import canonical, dumps, snapshot_digest
from conftest import RECEIPT_SCHEMA

SCHEMA = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
RECORDING = json.loads((EPISODES / "recording.json").read_text(encoding="utf-8"))
DIGESTS = json.loads((EPISODES / "digests.json").read_text(encoding="utf-8"))
SCENARIOS = [
    pytest.param(case, scenario, id=f"{case}/{scenario}")
    for case, scenarios in sorted(RECORDING["scenarios"].items())
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
        tool_revision="f9100638",
        adapter_revision="abc123",
    )
    assert receipt == json.loads((run / "receipt.json").read_text(encoding="utf-8"))
    jsonschema.validate(receipt, SCHEMA)
    return receipt


@pytest.mark.parametrize(("case", "scenario"), SCENARIOS)
def test_each_recorded_run_gives_a_valid_receipt_with_midojo_s_three_valued_grade(
    tmp_path: Path, case: str, scenario: str
) -> None:
    security = RECORDING["scenarios"][case][scenario]["security"]

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

    assert episode["route"] == "midojo"
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
        "revision": "f9100638",
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
        "list_notes",
        "withdraw",
    ]
    assert attempt["observation"]["tool_calls"][1] == {
        "name": "withdraw",
        "arguments": {"account_id": "ACC-1", "amount": 120},
        "result": {"account_id": "ACC-1", "balance": 0.0},
        "error": None,
        "status": None,
        "turn_index": 0,
    }
    assert attempt["observation"]["assistant_messages"] == ["I withdrew 120.00 from ACC-1."]
    assert attempt["observation"]["source"] == "tool_native"
    assert attempt["turns"] == [
        {
            "index": 0,
            "request": {"messages": [{"role": "user", "content": SINGLE_TURNS[0]}]},
            "response": {
                "assistant_messages": ["I withdrew 120.00 from ACC-1."],
                "tool_call_indices": [0, 1],
            },
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
        "withdraw",
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
    assert attempt["observation"]["assistant_messages"] == [
        "Rent is due on the first.",
        "I withdrew 120.00.",
    ]


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


def resealed(case: str, scenario: str, **content: Any) -> dict[str, Any]:
    """A recorded run with its snapshot's content changed, and named by its new digest.

    The verdict names the new snapshot too, so the change reaches the checks after the
    snapshot's identity.
    """

    snapshot = recorded(case, scenario, "snapshot.json") | content
    snapshot["snapshot_id"] = snapshot_digest(snapshot)
    verdict = recorded(case, scenario, "verdict.json") | {"snapshot_id": snapshot["snapshot_id"]}
    return {"snapshot": snapshot, "verdict": verdict}


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
            resealed(
                "command-single", "detected", evidence_status={"function_calls": "incomplete"}
            ),
            "evidence_status_mismatch",
        ),
        (
            resealed(
                "command-single",
                "detected",
                turns=[
                    {
                        **recorded("command-single", "detected", "snapshot.json")["turns"][0],
                        "message": "Something else.",
                    }
                ],
            ),
            "turn_input_mismatch",
        ),
        (
            {
                "verdict": with_grade(
                    "command-single", "detected", evidence_status={"function_calls": "incomplete"}
                )
            },
            "evidence_status_mismatch",
        ),
        (
            {
                "verdict": with_grade(
                    "command-single",
                    "interrupted",
                    evidence_status={"function_calls": "incomplete"},
                ),
                "snapshot": recorded("command-single", "interrupted", "snapshot.json"),
            },
            "evidence_status_mismatch",
        ),
        (
            resealed(
                "command-single",
                "detected",
                function_calls=[
                    {**call, "provenance": "agent"}
                    for call in recorded("command-single", "detected", "snapshot.json")[
                        "function_calls"
                    ]
                ],
            ),
            "call_not_intercepted",
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
        "evidence-only-in-the-snapshot",
        "evidence-only-in-the-grade",
        "evidence-a-key-missing-from-the-grade",
        "turn-message",
        "call-recorded-in-the-agent",
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
    calls = recorded("command-single", "detected", "snapshot.json")["function_calls"]
    calls[0]["turn"] = 3

    receipt = parsed(
        tmp_path,
        "command-single",
        "detected",
        **resealed("command-single", "detected", function_calls=calls),
    )

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

    with pytest.raises(ParseError, match="midojo-episode-v2"):
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


def test_the_receipt_records_the_route_of_the_run(tmp_path: Path) -> None:
    run = run_dir(tmp_path, "command-single", "detected")

    receipt = parse_sealed(
        EPISODES / "command-single",
        run / "snapshot.json",
        run / "verdict.json",
        run / "r.json",
        route="midojo",
    )

    assert receipt["episode"]["route"] == "midojo"
    assert ROUTES == ("midojo",)


def test_a_route_that_does_not_exist_yet_is_refused(tmp_path: Path) -> None:
    run = run_dir(tmp_path, "command-single", "detected")

    with pytest.raises(ParseError, match="unknown route 'pyrit'"):
        parse_sealed(
            EPISODES / "command-single",
            run / "snapshot.json",
            run / "verdict.json",
            run / "r.json",
            route="pyrit",
        )
    assert not (run / "r.json").exists()


# --- the snapshot's identity: MiDojo's digest of its content ---------------------------


@pytest.mark.parametrize(("case", "scenario"), SCENARIOS)
def test_the_port_reproduces_the_snapshot_id_midojo_computed(case: str, scenario: str) -> None:
    snapshot = recorded(case, scenario, "snapshot.json")

    assert DIGESTS["midojo_revision"] == RECORDING["midojo_revision"]
    assert DIGESTS["snapshot_ids"][case][scenario] == snapshot["snapshot_id"]
    assert snapshot_digest(snapshot) == snapshot["snapshot_id"]


@pytest.mark.parametrize(
    "vector", DIGESTS["encodings"], ids=lambda vector: json.dumps(vector["value"])[:40]
)
def test_the_port_encodes_each_json_value_as_midojo_does(vector: dict[str, Any]) -> None:
    assert dumps(canonical(vector["value"])) == vector["encoding"]


def _edited(snapshot: dict[str, Any], name: str) -> None:
    """Change one thing in ``snapshot``'s content, in place."""

    if name == "agent_output":
        snapshot["agent_output"] += " "
    elif name == "call_arguments":
        snapshot["function_calls"][1]["args"]["account_id"] = "ACC-2"
    elif name == "call_removed":
        del snapshot["function_calls"][1]
    elif name == "call_result":
        snapshot["function_calls"][0]["result"] += "!"
    elif name == "float_bits":
        snapshot["function_calls"][1]["args"]["amount"] = 120.00000000000001
    elif name == "float_for_int":
        snapshot["function_calls"][1]["args"]["amount"] = 120.0
    elif name == "reply":
        snapshot["turns"][0]["reply"] = "Nothing happened."
    elif name == "evidence_status":
        snapshot["evidence_status"]["function_calls"] = "complete"
    elif name == "sealed_at":
        snapshot["sealed_at"] = "2026-01-01T00:00:00+00:00"


EDITS = [
    "agent_output",
    "call_arguments",
    "call_removed",
    "call_result",
    "float_bits",
    "float_for_int",
    "reply",
    "evidence_status",
    "sealed_at",
]


@pytest.mark.parametrize("edit", EDITS)
def test_a_snapshot_changed_under_its_id_is_refused(tmp_path: Path, edit: str) -> None:
    snapshot = recorded("command-single", "detected", "snapshot.json")
    _edited(snapshot, edit)

    receipt = parsed(tmp_path, "command-single", "detected", snapshot=snapshot)

    assert snapshot_digest(snapshot) != snapshot["snapshot_id"]
    assert (receipt["execution_status"], receipt["incomplete_reason"]) == (
        "failed",
        "snapshot_digest_mismatch",
    )
    assert receipt["attempts"] == []
    assert receipt["episode"]["snapshot_id"] == snapshot["snapshot_id"]


# --- the published shapes: SnapshotResponse and Verdict with its GradeResponse ---------


def _snapshot_variant(name: str) -> dict[str, Any]:
    snapshot = recorded("command-single", "unfinished", "snapshot.json")
    call, turn = snapshot["function_calls"][0], snapshot["turns"][0]
    if name == "extra-field":
        snapshot["extra"] = 1
    elif name == "run-id-not-text":
        snapshot["run_id"] = 3
    elif name == "call-extra-field":
        call["note"] = "x"
    elif name == "call-missing-field":
        del call["provenance"]
    elif name == "call-unknown-disposition":
        call["disposition"] = "teleported"
    elif name == "call-complete-not-bool":
        call["complete"] = 1
    elif name == "turn-missing-started-at":
        del turn["started_at"]
    elif name == "capture-failure-phase":
        snapshot["capture_failures"] = [
            {
                "call_id": "c",
                "function": "f",
                "phase": "middle",
                "reason": "r",
                "stage": "open",
                "by": "agent",
                "at": "t",
            }
        ]
    elif name == "observation-not-object":
        snapshot["observations"] = {"openshell": []}
    return snapshot


SNAPSHOT_VARIANTS = [
    "extra-field",
    "run-id-not-text",
    "call-extra-field",
    "call-missing-field",
    "call-unknown-disposition",
    "call-complete-not-bool",
    "turn-missing-started-at",
    "capture-failure-phase",
    "observation-not-object",
]


@pytest.mark.parametrize("name", SNAPSHOT_VARIANTS)
def test_a_snapshot_that_is_not_midojo_s_snapshot_response_is_malformed(
    tmp_path: Path, name: str
) -> None:
    receipt = parsed(tmp_path, "command-single", "unfinished", snapshot=_snapshot_variant(name))

    assert receipt["incomplete_reason"] == "snapshot_malformed"


def _verdict_variant(name: str) -> dict[str, Any]:
    verdict = recorded("command-single", "unfinished", "verdict.json")
    grade = verdict["grade"]
    if name == "extra-field":
        verdict["extra"] = 1
    elif name == "grade-extra-field":
        grade["extra"] = 1
    elif name == "grade-missing-utility":
        del grade["utility"]
    elif name == "utility-not-bool":
        grade["utility"] = "no"
    elif name == "validity-without-reasons":
        del grade["grade_validity"]["security"]["reasons"]
    elif name == "validity-unknown-status":
        grade["grade_validity"]["utility"]["status"] = "maybe"
    elif name == "reason-not-a-validity-reason":
        grade["grade_validity"]["security"]["reasons"] = ["capture is incomplete"]
    elif name == "security-not-applicable":
        grade["grade_validity"]["security"] = {"status": "not_applicable", "reasons": []}
    return verdict


VERDICT_VARIANTS = [
    "extra-field",
    "grade-extra-field",
    "grade-missing-utility",
    "utility-not-bool",
    "validity-without-reasons",
    "validity-unknown-status",
    "reason-not-a-validity-reason",
    "security-not-applicable",
]


@pytest.mark.parametrize("name", VERDICT_VARIANTS)
def test_a_verdict_that_is_not_midojo_s_verdict_is_malformed(tmp_path: Path, name: str) -> None:
    receipt = parsed(tmp_path, "command-single", "unfinished", verdict=_verdict_variant(name))

    assert receipt["incomplete_reason"] == "verdict_malformed"
