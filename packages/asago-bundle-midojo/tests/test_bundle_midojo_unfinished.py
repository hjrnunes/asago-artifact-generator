"""parse: a call that started and never finished makes the attempt inconclusive.

The records here are written in the shapes their producers write: MiDojo 082bb49 holds a
started call with ``complete: false``, no result and the turn it started in; mini-agents
0290e6f logs it with ``start: ok`` and ``report: unfinished``; asago-orch's recording
proxy holds it as it arrived, ``status: no_response`` with no result, and no ``sink``
field, since a MiDojo run's server reports its calls itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_bundle_core.testing import sequential_stimulus, write_test_package
from asago_bundle_midojo.compiler import compile_package
from asago_bundle_midojo.instantiate import instantiate_bundle
from asago_bundle_midojo.parse import parse_bundle
from conftest import (
    CALL,
    RECEIPT_SCHEMA,
    SEQUENTIAL_REPLY_VALUES,
    SEQUENTIAL_VALUES,
    compile_options,
    evaluation_dump,
    reply_verdict_dump,
    server_line,
    turns_evaluation_dump,
    write_native,
)

VALIDATOR = jsonschema.Draft202012Validator(json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8")))
FINISHED = {**CALL, "call_id": "mini-agents:finished", "complete": True}
UNFINISHED = {
    **CALL,
    "call_id": "mini-agents:unfinished",
    "function": "lookup_order",
    "args": {"order": "ORD-101"},
    "result": None,
    "error": None,
    "complete": False,
}


def receipt_of(bundle: Path, **options: Any) -> dict[str, Any]:
    receipt = parse_bundle(bundle, bundle / "receipt.json", **options)
    assert list(VALIDATOR.iter_errors(receipt)) == []
    return receipt


def logged(call: dict[str, Any], sequence: int, **changes: Any) -> dict[str, Any]:
    """The server log line of one MiDojo call, as mini-agents 0290e6f writes it."""

    unfinished = call.get("complete") is False
    return server_line(
        sequence,
        call_id=call["call_id"],
        function=call["function"],
        start="ok",
        push=None if unfinished else "ok",
        report="unfinished" if unfinished else "ok",
        **changes,
    )


def proxied(call: dict[str, Any], sequence: int) -> dict[str, Any]:
    """The proxy line of one MiDojo call: a call the server never finished got no reply."""

    unfinished = call.get("complete") is False
    return {
        "sequence": sequence,
        "session": 1,
        "jsonrpc_id": sequence,
        "name": call["function"],
        "arguments": call["args"],
        "status": "no_response" if unfinished else "ok",
        "result": None
        if unfinished
        else {"content": [{"type": "text", "text": '{"referral": "cardiology"}'}]},
        "error": None,
    }


def write_records(
    directory: Path,
    server: list[dict[str, Any]] | None = None,
    proxy: list[dict[str, Any]] | None = None,
) -> Path:
    for name, lines in (("midojo_capture", server), ("mcp_capture", proxy)):
        if lines is None:
            continue
        (directory / name).mkdir(parents=True)
        (directory / name / "calls.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
    return directory


def all_records(directory: Path, calls: tuple[dict[str, Any], ...]) -> Path:
    numbered = list(enumerate(calls, start=1))
    return write_records(
        directory,
        server=[logged(call, sequence) for sequence, call in numbered],
        proxy=[proxied(call, sequence) for sequence, call in numbered],
    )


def unfinished_result(claim_level: str) -> dict[str, Any]:
    return {
        "outcome": "inconclusive",
        "claim_level": claim_level,
        "reason": "unfinished_call:lookup_order",
    }


# --- single bundles ----------------------------------------------------------------------


@pytest.mark.parametrize("security", [True, False])
def test_an_unfinished_call_makes_a_command_attempt_inconclusive(
    bundle: Path, tmp_path: Path, security: bool
) -> None:
    calls = (FINISHED, UNFINISHED)
    write_native(bundle, evaluation=evaluation_dump(security=security, calls=calls))

    receipt = receipt_of(bundle, records_dir=all_records(tmp_path / "records", calls))

    assert receipt["execution_status"] == "completed"
    assert receipt["runtime_status"] == "completed"
    assert receipt["incomplete_reason"] is None
    (attempt,) = receipt["attempts"]
    assert attempt["result"] == unfinished_result("command_attempt")
    (detection,) = attempt["detections"]
    assert detection["detector"] == "ToolCallCondition"
    assert (detection["outcome"], detection["reason"]) == (
        "inconclusive",
        "unfinished_call:lookup_order",
    )
    assert attempt["observation"]["source"] == "mcp_recording_proxy"
    assert [
        (call["name"], call["status"], call["result"], call["turn_index"])
        for call in attempt["observation"]["tool_calls"]
    ] == [
        ("get_referral", "ok", {"referral": "cardiology"}, 0),
        ("lookup_order", "no_response", None, 0),
    ], "the finished call keeps its result"
    assert attempt["turns"][0]["response"]["tool_call_indices"] == [0, 1]


def test_without_the_proxy_the_unfinished_native_call_says_so(
    bundle: Path, tmp_path: Path
) -> None:
    calls = (FINISHED, UNFINISHED)
    write_native(bundle, evaluation=evaluation_dump(calls=calls))
    numbered = enumerate(calls, start=1)
    records = write_records(
        tmp_path / "records", server=[logged(call, sequence) for sequence, call in numbered]
    )

    (attempt,) = receipt_of(bundle, records_dir=records)["attempts"]

    assert attempt["result"] == unfinished_result("command_attempt")
    assert [
        (call["name"], call["status"], call["result"])
        for call in attempt["observation"]["tool_calls"]
    ] == [("get_referral", None, {"referral": "cardiology"}), ("lookup_order", "unfinished", None)]


@pytest.mark.parametrize("security", [True, False])
def test_an_unfinished_call_makes_a_reply_inconclusive_and_the_judge_keeps_its_verdict(
    reply_bundle: Path, tmp_path: Path, security: bool
) -> None:
    calls = (FINISHED, UNFINISHED)
    write_native(
        reply_bundle,
        evaluation=evaluation_dump(security=security, calls=calls),
        verdict=reply_verdict_dump("supported"),
    )

    receipt = receipt_of(reply_bundle, records_dir=all_records(tmp_path / "records", calls))

    assert receipt["execution_status"] == "completed"
    (attempt,) = receipt["attempts"]
    assert attempt["result"] == unfinished_result("reply")
    assert attempt["detections"] == [
        {
            "detector": "RubricJudge",
            "outcome": "inconclusive",
            "reason": "unfinished_call:lookup_order",
            "matched_calls": None,
        }
    ]
    assert attempt["judge"] == {
        "verdict": "supported",
        "reason": "judge_completed",
        "request_count": 1,
    }


def test_the_reason_names_the_first_unfinished_call(bundle: Path, tmp_path: Path) -> None:
    second = {**UNFINISHED, "call_id": "mini-agents:second", "function": "get_referral"}
    calls = (UNFINISHED, second)
    write_native(bundle, evaluation=evaluation_dump(calls=calls))

    receipt = receipt_of(bundle, records_dir=all_records(tmp_path / "records", calls))

    (attempt,) = receipt["attempts"]
    assert attempt["result"]["reason"] == "unfinished_call:lookup_order"


# --- sequential bundles ------------------------------------------------------------------


def sequential_bundle(tmp_path: Path, claim_level: str) -> Path:
    package = write_test_package(
        tmp_path / "packages", claim_level=claim_level, stimulus=sequential_stimulus()
    )
    compile_package(package, tmp_path / "template", **compile_options())
    values = SEQUENTIAL_REPLY_VALUES if claim_level == "reply" else SEQUENTIAL_VALUES
    instantiate_bundle(tmp_path / "template", values, tmp_path / "bundle")
    return tmp_path / "bundle"


@pytest.mark.parametrize("claim_level", ["command_attempt", "reply"])
def test_a_sequential_run_places_the_unfinished_call_in_the_turn_midojo_recorded(
    tmp_path: Path, claim_level: str
) -> None:
    bundle = sequential_bundle(tmp_path, claim_level)
    calls = ({**FINISHED, "turn": 0}, {**UNFINISHED, "turn": 1})
    write_native(
        bundle,
        evaluation=turns_evaluation_dump(calls=calls),
        verdict=reply_verdict_dump() if claim_level == "reply" else None,
    )

    receipt = receipt_of(bundle, records_dir=all_records(tmp_path / "records", calls))

    assert receipt["execution_status"] == "completed"
    assert receipt["incomplete_reason"] is None
    (attempt,) = receipt["attempts"]
    assert attempt["result"] == unfinished_result(claim_level)
    assert [
        (call["name"], call["status"], call["turn_index"])
        for call in attempt["observation"]["tool_calls"]
    ] == [("get_referral", "ok", 0), ("lookup_order", "no_response", 1)]
    assert [t["response"]["tool_call_indices"] for t in attempt["turns"]] == [[0], [1]]
    assert [t["response"]["assistant_messages"] for t in attempt["turns"]] == [["Hi."], ["Done."]]


# --- records that disagree -----------------------------------------------------------------


def test_midojo_unfinished_but_the_server_finished_fails_with_unfinished_call_mismatch(
    bundle: Path, tmp_path: Path
) -> None:
    calls = (FINISHED, UNFINISHED)
    write_native(bundle, evaluation=evaluation_dump(calls=calls))
    records = write_records(
        tmp_path / "records",
        server=[logged(FINISHED, 1), logged({**UNFINISHED, "complete": True}, 2)],
        proxy=[proxied(call, sequence) for sequence, call in enumerate(calls, start=1)],
    )

    receipt = receipt_of(bundle, records_dir=records)

    assert receipt["execution_status"] == "failed"
    assert receipt["incomplete_reason"] == "unfinished_call_mismatch"
    assert receipt["attempts"] == []


@pytest.mark.parametrize(
    "midojo",
    [
        pytest.param((FINISHED, {**UNFINISHED, "complete": True}), id="midojo-finished"),
        pytest.param((FINISHED,), id="midojo-missing"),
        pytest.param(
            (FINISHED, {**UNFINISHED, "call_id": "mini-agents:other"}), id="another-call-id"
        ),
    ],
)
def test_a_server_unfinished_call_midojo_does_not_hold_unfinished_fails(
    bundle: Path, tmp_path: Path, midojo: tuple[dict[str, Any], ...]
) -> None:
    write_native(bundle, evaluation=evaluation_dump(calls=midojo))
    records = write_records(
        tmp_path / "records", server=[logged(FINISHED, 1), logged(UNFINISHED, 2)]
    )

    receipt = receipt_of(bundle, records_dir=records)

    assert receipt["incomplete_reason"] == "unfinished_call_mismatch"
    assert receipt["attempts"] == []


@pytest.mark.parametrize("proxy", [False, True], ids=["no-records", "proxy-only"])
def test_a_midojo_unfinished_call_without_a_server_log_fails_with_unfinished_call_mismatch(
    bundle: Path, tmp_path: Path, proxy: bool
) -> None:
    calls = (FINISHED, UNFINISHED)
    write_native(bundle, evaluation=evaluation_dump(calls=calls))
    options: dict[str, Any] = {}
    if proxy:
        options["records_dir"] = write_records(
            tmp_path / "records",
            proxy=[proxied(call, sequence) for sequence, call in enumerate(calls, start=1)],
        )

    receipt = receipt_of(bundle, **options)

    assert receipt["execution_status"] == "failed"
    assert receipt["incomplete_reason"] == "unfinished_call_mismatch"
    assert receipt["attempts"] == []


def test_agreeing_unfinished_calls_with_a_finished_count_mismatch_still_fail_the_count(
    bundle: Path, tmp_path: Path
) -> None:
    calls = (FINISHED, UNFINISHED)
    write_native(bundle, evaluation=evaluation_dump(calls=calls))
    records = write_records(
        tmp_path / "records",
        server=[logged(FINISHED, 1), logged(FINISHED, 2), logged(UNFINISHED, 3)],
    )

    assert (
        receipt_of(bundle, records_dir=records)["incomplete_reason"] == "tool_call_count_mismatch"
    )


def test_a_proxy_without_the_unfinished_call_fails_the_count(bundle: Path, tmp_path: Path) -> None:
    """orch's proxy records every call when it arrives, so its file holds the unfinished one."""

    calls = (FINISHED, UNFINISHED)
    write_native(bundle, evaluation=evaluation_dump(calls=calls))
    records = write_records(
        tmp_path / "records",
        server=[logged(call, sequence) for sequence, call in enumerate(calls, start=1)],
        proxy=[proxied(FINISHED, 1)],
    )

    assert (
        receipt_of(bundle, records_dir=records)["incomplete_reason"] == "tool_call_count_mismatch"
    )


# --- server failures and old logs ----------------------------------------------------------


def test_a_failed_start_fails_the_receipt_with_sink_failed(bundle: Path, tmp_path: Path) -> None:
    write_native(bundle)
    records = write_records(
        tmp_path / "records",
        server=[
            server_line(
                1,
                call_id="mini-agents:refused",
                load="ok",
                start="failed",
                push=None,
                report=None,
                failure="POST /agent/function-calls/start: HTTP 409",
            )
        ],
    )

    receipt = receipt_of(bundle, records_dir=records)

    assert receipt["execution_status"] == "failed"
    assert receipt["incomplete_reason"] == "sink_failed"
    assert receipt["attempts"] == []


def test_an_old_server_log_and_a_new_one_give_the_same_receipt_without_unfinished_calls(
    bundle: Path, tmp_path: Path
) -> None:
    write_native(bundle)
    old = write_records(tmp_path / "old", server=[server_line(1, load="ok")])
    assert "call_id" not in server_line() and "start" not in server_line()
    new = write_records(tmp_path / "new", server=[logged(FINISHED, 1, load="ok")])

    before = receipt_of(bundle, records_dir=old)
    after = receipt_of(bundle, records_dir=new)

    assert before["execution_status"] == "completed"
    assert before == after
    (attempt,) = before["attempts"]
    assert attempt["result"]["outcome"] == "detected"
    assert attempt["observation"]["tool_calls"][0]["status"] is None
