"""Sequential bundles: one suite turn per user turn in, one receipt turn per planned turn out.

The evaluation records here are written in the shape MiDojo 29d2b6e's control plane
answers: ``turns`` with each turn's message and reply, and every call stamped with the
``turn`` most recently started when it was recorded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_artifact_generator.package_io import SEED_MEMBER, build_package, write_package
from asago_bundle_core.testing import sequential_stimulus, write_test_package
from asago_bundle_midojo.bundle_files import asago_rubric as rubric
from asago_bundle_midojo.compiler import compile_package
from asago_bundle_midojo.instantiate import InstantiateError, instantiate_bundle
from asago_bundle_midojo.parse import parse_bundle
from conftest import (
    CALL,
    RECEIPT_SCHEMA,
    SEQUENTIAL_REPLY_VALUES,
    SEQUENTIAL_VALUES,
    TURNS,
    VALUES,
    compile_options,
    reply_verdict_dump,
    turn,
    turns_evaluation_dump,
    write_native,
)

REPO = Path(__file__).resolve().parents[3]
SCN_021 = REPO / "tests/fixtures/author_report/author/output/SCN-021"
VALIDATOR = jsonschema.Draft202012Validator(json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8")))
LOOKUP = {**CALL, "function": "lookup_order", "args": {"order": "ORD-101"}, "turn": 0}
REFERRAL = {**CALL, "turn": 1}


def make_bundle(
    tmp_path: Path, claim_level: str = "command_attempt", values: dict[str, Any] | None = None
) -> Path:
    package = write_test_package(
        tmp_path / "packages", claim_level=claim_level, stimulus=sequential_stimulus()
    )
    compile_package(package, tmp_path / "template", **compile_options())
    if values is None:
        values = SEQUENTIAL_REPLY_VALUES if claim_level == "reply" else SEQUENTIAL_VALUES
    instantiate_bundle(tmp_path / "template", values, tmp_path / "bundle")
    return tmp_path / "bundle"


def scn_021_package(root: Path) -> Path:
    """A package of the authored SCN-021 stimulus, seed and condition."""

    members = {
        "plan.json": json.dumps(
            {"observation_claim": {"claim_level": "command_attempt"}}
        ).encode(),
        **{
            name: (SCN_021 / name).read_bytes()
            for name in ("stimulus.json", SEED_MEMBER, "tool_call_condition.json")
        },
    }
    package = build_package(
        package_id="SCN-021-SCN-021",
        scenario_id="SCN-021",
        input_kind="scenario-handoff-v5",
        source_digests={"handoff": "0" * 64},
        members=members,
    )
    return write_package(root / "SCN-021", package)


def suite_of(directory: Path, name: str = "asago_suite/suite.yaml") -> dict[str, Any]:
    return json.loads((directory / name).read_text(encoding="utf-8"))


def receipt_of(bundle: Path, **options: Any) -> dict[str, Any]:
    receipt = parse_bundle(bundle, bundle / "receipt.json", **options)
    assert list(VALIDATOR.iter_errors(receipt)) == []
    return receipt


def write_records(directory: Path, calls: list[dict[str, Any]]) -> Path:
    (directory / "mcp_capture").mkdir(parents=True)
    (directory / "mcp_capture/calls.jsonl").write_text(
        "".join(json.dumps(call) + "\n" for call in calls), encoding="utf-8"
    )
    return directory


def proxy_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "arguments": arguments,
        "status": "ok",
        "result": {"content": [{"type": "text", "text": '{"ok": true}'}]},
        "error": None,
        "sink": "ok",
    }


# --- compile -------------------------------------------------------------------------------


def test_a_two_turn_package_compiles_to_one_suite_turn_slot_per_user_turn(
    tmp_path: Path,
) -> None:
    package = write_test_package(tmp_path / "packages", stimulus=sequential_stimulus())

    manifest = compile_package(package, tmp_path / "template", **compile_options())

    (task,) = suite_of(tmp_path / "template", "suite.template.json")["user_tasks"]
    assert "prompt" not in task
    assert len(task["turns"]) == 2
    assert manifest["delivery"] == "sequential"
    assert manifest["tool_revision_range"] == ">=29d2b6e9"
    assert {"user_turn_1", "user_turn_2"} <= set(manifest["requires"])
    assert "messages" not in manifest["requires"]
    assert "judge_turns" not in manifest["requires"]


def test_a_single_package_keeps_its_prompt_and_its_revision_range(
    command_package: Path, tmp_path: Path
) -> None:
    manifest = compile_package(command_package, tmp_path / "template", **compile_options())

    (task,) = suite_of(tmp_path / "template", "suite.template.json")["user_tasks"]
    assert "turns" not in task
    assert manifest["delivery"] == "single"
    assert manifest["tool_revision_range"] == ">=5efb3de8"
    assert "messages" in manifest["requires"]


def test_the_authored_sequential_fixture_compiles_with_its_seed(tmp_path: Path) -> None:
    stimulus = json.loads((SCN_021 / "stimulus.json").read_text(encoding="utf-8"))

    package = scn_021_package(tmp_path / "packages")

    manifest = compile_package(package, tmp_path / "template", **compile_options())

    suite = suite_of(tmp_path / "template", "suite.template.json")
    assert len(suite["user_tasks"][0]["turns"]) == stimulus["turn_count"] == 2
    assert suite["injection_tasks"][0]["probes"]
    assert manifest["delivery"] == "sequential"


def test_instantiate_writes_the_turns_in_order(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)

    (task,) = suite_of(bundle)["user_tasks"]
    assert task["turns"] == list(TURNS)
    assert "prompt" not in task


def test_a_reply_bundle_judges_the_final_turn_with_the_earlier_turns_as_history(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path, "reply")

    manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    spec = suite_of(bundle)["injection_tasks"][0]["security"][rubric.RUBRIC_NAME]
    assert "judge_turns" in manifest["requires"]
    assert spec["user_text"] == TURNS[-1]
    assert spec["history"] == [{"role": "user", "content": TURNS[0]}]
    assert spec["judge_turns"] == "final"


# --- instantiate ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("user_turn_2", None),
        ("user_turn_1", ""),
        ("user_turn_1", "   "),
        ("user_turn_2", ["Show PAT-201."]),
        ("user_turn_2", "Show {asago_injection_task:seed_0} now."),
    ],
)
def test_instantiate_checks_each_turn_as_one_prompt(tmp_path: Path, key: str, bad: Any) -> None:
    package = write_test_package(tmp_path / "packages", stimulus=sequential_stimulus())
    compile_package(package, tmp_path / "template", **compile_options())
    values = {**SEQUENTIAL_VALUES, key: bad}
    if bad is None:
        del values[key]

    with pytest.raises(InstantiateError, match=key):
        instantiate_bundle(tmp_path / "template", values, tmp_path / "bundle")


@pytest.mark.parametrize("judge_turns", ["all", "first", None])
def test_instantiate_refuses_judge_turns_other_than_final(
    tmp_path: Path, judge_turns: Any
) -> None:
    package = write_test_package(
        tmp_path / "packages", claim_level="reply", stimulus=sequential_stimulus()
    )
    compile_package(package, tmp_path / "template", **compile_options())

    with pytest.raises(InstantiateError, match="judge_turns"):
        instantiate_bundle(
            tmp_path / "template",
            {**SEQUENTIAL_REPLY_VALUES, "judge_turns": judge_turns},
            tmp_path / "bundle",
        )


# --- parse ---------------------------------------------------------------------------------


def test_a_two_turn_run_gives_one_receipt_turn_per_planned_turn(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    write_native(bundle, evaluation=turns_evaluation_dump(calls=(LOOKUP, REFERRAL)))

    receipt = receipt_of(bundle)

    assert receipt["execution_status"] == "completed"
    assert receipt["incomplete_reason"] is None
    (attempt,) = receipt["attempts"]
    assert [t["index"] for t in attempt["turns"]] == [0, 1]
    assert [t["request"]["messages"] for t in attempt["turns"]] == [
        [{"role": "user", "content": text}] for text in TURNS
    ], "each turn's request is that turn's user message"
    assert [t["response"]["assistant_messages"] for t in attempt["turns"]] == [["Hi."], ["Done."]]
    assert [t["response"]["tool_call_indices"] for t in attempt["turns"]] == [[0], [1]]
    calls = attempt["observation"]["tool_calls"]
    assert [(c["name"], c["turn_index"]) for c in calls] == [
        ("lookup_order", 0),
        ("get_referral", 1),
    ]
    assert attempt["observation"]["source"] == "tool_native"
    assert attempt["observation"]["assistant_messages"] == ["Hi.", "Done."]
    assert attempt["result"]["outcome"] == "detected"
    assert attempt["generation"]["response_status"] == "completed"


def test_proxy_calls_take_their_turn_from_the_control_plane(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    write_native(bundle, evaluation=turns_evaluation_dump(calls=(LOOKUP, REFERRAL)))
    records = write_records(
        tmp_path / "records",
        [
            proxy_call("lookup_order", {"order": "ORD-101"}),
            proxy_call("get_referral", {"patient_id": "PAT-201"}),
        ],
    )

    (attempt,) = receipt_of(bundle, records_dir=records)["attempts"]

    calls = attempt["observation"]["tool_calls"]
    assert attempt["observation"]["source"] == "mcp_recording_proxy"
    assert [(c["name"], c["turn_index"]) for c in calls] == [
        ("lookup_order", 0),
        ("get_referral", 1),
    ]
    assert [t["response"]["tool_call_indices"] for t in attempt["turns"]] == [[0], [1]]


def test_proxy_calls_that_disagree_in_count_fail_the_receipt(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    write_native(bundle, evaluation=turns_evaluation_dump(calls=(LOOKUP, REFERRAL)))
    records = write_records(tmp_path / "records", [proxy_call("lookup_order", {})])

    receipt = receipt_of(bundle, records_dir=records)

    assert receipt["incomplete_reason"] == "tool_call_count_mismatch"
    assert receipt["attempts"] == []


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        ({**CALL, "turn": None}, "call_turn_missing"),
        (CALL, "call_turn_missing"),
        ({**CALL, "turn": 2}, "call_turn_out_of_range"),
        ({**CALL, "turn": -1}, "call_turn_out_of_range"),
        ({**CALL, "turn": "1"}, "call_turn_out_of_range"),
        ({**CALL, "turn": True}, "call_turn_out_of_range"),
    ],
)
def test_a_call_without_a_planned_turn_fails_the_receipt(
    tmp_path: Path, call: dict[str, Any], reason: str
) -> None:
    bundle = make_bundle(tmp_path)
    write_native(bundle, evaluation=turns_evaluation_dump(calls=(LOOKUP, call)))

    receipt = receipt_of(bundle)

    assert receipt["execution_status"] == "failed"
    assert receipt["incomplete_reason"] == reason
    assert receipt["attempts"] == []


def test_a_run_that_ends_before_the_second_turn_names_it(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    evaluation = turns_evaluation_dump(
        turns=(turn(0, "Hi."),), calls=(LOOKUP,), completed=False, security=None
    )
    write_native(bundle, evaluation=evaluation)

    receipt = receipt_of(bundle)

    assert receipt["execution_status"] == "failed"
    assert receipt["incomplete_reason"] == "turn_failed:2"
    (attempt,) = receipt["attempts"]
    assert [t["response"]["assistant_messages"] for t in attempt["turns"]] == [["Hi."], []]
    assert [t["response"]["tool_call_indices"] for t in attempt["turns"]] == [[0], []]
    assert attempt["generation"]["response_status"] is None
    assert attempt["result"]["outcome"] is None
    assert attempt["detections"] == []
    assert attempt["judge"] is None


@pytest.mark.parametrize(
    ("turns", "reason"),
    [
        ((turn(0, "Hi."), turn(1, None)), "turn_failed:2"),
        ((turn(0, None),), "turn_failed:1"),
        ((), "turn_failed:1"),
    ],
)
def test_a_turn_without_a_reply_fails_the_receipt(
    tmp_path: Path, turns: tuple[dict[str, Any], ...], reason: str
) -> None:
    bundle = make_bundle(tmp_path)
    write_native(
        bundle,
        evaluation=turns_evaluation_dump(turns=turns, calls=(), completed=False, security=None),
    )

    receipt = receipt_of(bundle)

    assert receipt["incomplete_reason"] == reason
    (attempt,) = receipt["attempts"]
    assert len(attempt["turns"]) == 2


@pytest.mark.parametrize(
    "turns",
    [
        (turn(0, "Hi.", message="Hello!"), turn(1)),
        (turn(0, "Hi."), turn(1), {**turn(1), "index": 2, "message": "More."}),
    ],
)
def test_turns_other_than_the_bundle_s_fail_the_receipt(
    tmp_path: Path, turns: tuple[dict[str, Any], ...]
) -> None:
    bundle = make_bundle(tmp_path)
    write_native(bundle, evaluation=turns_evaluation_dump(turns=turns))

    receipt = receipt_of(bundle)

    assert receipt["incomplete_reason"] == "turn_input_mismatch"
    assert receipt["attempts"] == []


@pytest.mark.parametrize(
    "turns", [None, "Hi.", [turn(1)], [{"index": 0, "message": None, "reply": "Hi."}]]
)
def test_malformed_turns_fail_the_receipt(tmp_path: Path, turns: Any) -> None:
    bundle = make_bundle(tmp_path)
    evaluation = turns_evaluation_dump()
    evaluation["turns"] = turns
    write_native(bundle, evaluation=evaluation)

    assert receipt_of(bundle)["incomplete_reason"] == "turns_malformed"


def test_a_sequential_seed_missing_from_the_pre_environment_fails_the_receipt(
    tmp_path: Path,
) -> None:
    stimulus = json.loads((SCN_021 / "stimulus.json").read_text(encoding="utf-8"))
    texts = [item["content"] for item in stimulus["history"]] + [stimulus["user_text"]]
    compile_package(
        scn_021_package(tmp_path / "packages"), tmp_path / "template", **compile_options()
    )
    values = {
        **{key: value for key, value in VALUES.items() if key != "messages"},
        **{f"user_turn_{number}": text for number, text in enumerate(texts, start=1)},
    }
    instantiate_bundle(tmp_path / "template", values, tmp_path / "bundle")
    turns = tuple(turn(index, "Ok.", message=text) for index, text in enumerate(texts))
    (probe,) = suite_of(tmp_path / "bundle")["injection_tasks"][0]["probes"].values()
    provisioned = {"orders": {"ORD-101": {"item": probe["payload"]}}}
    schedule = {**CALL, "function": "schedule_payment", "args": {"plan_id": "PLAN-21"}, "turn": 1}

    write_native(
        tmp_path / "bundle",
        evaluation=turns_evaluation_dump(turns=turns, calls=(schedule,), pre_environment={}),
    )
    assert receipt_of(tmp_path / "bundle")["incomplete_reason"] == "seed_not_provisioned"

    write_native(
        tmp_path / "bundle",
        evaluation=turns_evaluation_dump(
            turns=turns, calls=(schedule,), pre_environment=provisioned
        ),
    )
    receipt = receipt_of(tmp_path / "bundle")
    assert receipt["execution_status"] == "completed"
    assert receipt["attempts"][0]["result"]["outcome"] == "detected"


# --- the reply claim -----------------------------------------------------------------------


def test_a_two_turn_reply_run_gives_the_final_turn_s_verdict(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, "reply")
    write_native(
        bundle,
        evaluation=turns_evaluation_dump(calls=(LOOKUP, REFERRAL)),
        verdict=reply_verdict_dump(),
    )

    receipt = receipt_of(bundle)

    assert receipt["execution_status"] == "completed"
    (attempt,) = receipt["attempts"]
    assert [t["response"]["assistant_messages"] for t in attempt["turns"]] == [["Hi."], ["Done."]]
    assert [t["response"]["tool_call_indices"] for t in attempt["turns"]] == [[0], [1]]
    assert attempt["result"]["outcome"] == "detected"
    assert attempt["judge"] == {
        "verdict": "supported",
        "reason": "judge_completed",
        "request_count": 1,
    }


def test_a_two_turn_reply_run_that_failed_a_turn_is_not_graded(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, "reply")
    evaluation = turns_evaluation_dump(turns=(turn(0, "Hi."),), completed=False, security=None)
    write_native(bundle, evaluation={**evaluation, "function_calls": []})

    receipt = receipt_of(bundle)

    assert receipt["incomplete_reason"] == "turn_failed:2"
    assert receipt["attempts"][0]["judge"] is None


class Context:
    """The part of MiDojo's verification context the verifier reads: the final reply."""

    agent_output = "Done."
    function_calls = [LOOKUP, REFERRAL]


def test_the_bundle_s_verifier_shows_the_judge_the_earlier_turns(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path, "reply")
    spec = suite_of(bundle)["injection_tasks"][0]["security"][rubric.RUBRIC_NAME]
    seen: list[Any] = []

    def judge(_url: str, _model: str) -> Any:
        def call(messages: list[dict[str, str]]) -> str:
            seen.append(json.loads(messages[1]["content"]))
            return json.dumps({"verdict": "supported", "evidence_refs": ["messages[0].content"]})

        return call

    record = rubric.RubricJudgeVerifier(judge).grade(rubric.parse_spec(spec), Context())

    assert record["verdict"] == "supported"
    (request,) = seen
    assert request["evidence"]["user_text"] == TURNS[-1]
    assert request["evidence"]["history"] == [{"role": "user", "content": TURNS[0]}]
    assert request["evidence"]["messages"] == [{"role": "assistant", "content": "Done."}]
    assert [call["name"] for call in request["evidence"]["tool_calls"]] == [
        "lookup_order",
        "get_referral",
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"judge_turns": "all"},
        {"history": [{"role": "assistant", "content": "Hi."}]},
        {"history": [{"role": "user", "content": ""}]},
        {"history": "Hello."},
    ],
)
def test_the_verifier_refuses_a_spec_it_cannot_judge_faithfully(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    bundle = make_bundle(tmp_path, "reply")
    spec = suite_of(bundle)["injection_tasks"][0]["security"][rubric.RUBRIC_NAME]

    with pytest.raises(ValueError):
        rubric.parse_spec({**spec, **changes})
