"""parse: MiDojo's control-plane records become an execution-receipt-v1 document."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_bundle_midojo.parse import ParseError, parse_bundle
from conftest import (
    CALL,
    PROMPT,
    RECEIPT_SCHEMA,
    evaluation_dump,
    run_dump,
    verdict_dump,
    write_native,
)

VALIDATOR = jsonschema.Draft202012Validator(json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8")))


def receipt_of(bundle: Path, **options: Any) -> dict[str, Any]:
    receipt = parse_bundle(bundle, bundle / "receipt.json", **options)
    assert list(VALIDATOR.iter_errors(receipt)) == []
    assert json.loads((bundle / "receipt.json").read_text(encoding="utf-8")) == receipt
    return receipt


def only_attempt(receipt: dict[str, Any]) -> dict[str, Any]:
    (attempt,) = receipt["attempts"]
    return attempt


@pytest.mark.parametrize(
    ("security", "outcome"),
    [(True, "detected"), (False, "not_detected"), (False, "inconclusive")],
)
def test_a_graded_evaluation_gives_a_completed_receipt(
    bundle: Path, security: bool, outcome: str
) -> None:
    write_native(
        bundle,
        evaluation=evaluation_dump(security=security),
        verdict=verdict_dump(outcome),
    )

    receipt = receipt_of(bundle)

    assert receipt["execution_status"] == "completed"
    assert receipt["runtime_status"] == "completed"
    assert receipt["incomplete_reason"] is None
    attempt = only_attempt(receipt)
    assert attempt["result"] == {
        "outcome": outcome,
        "claim_level": "command_attempt",
        "reason": verdict_dump(outcome)["reason"],
    }
    assert attempt["detections"] == [
        {
            "detector": "ToolCallCondition",
            "outcome": outcome,
            "reason": verdict_dump(outcome)["reason"],
            "matched_calls": verdict_dump(outcome)["matched_calls"],
        }
    ]
    assert attempt["judge"] is None


def test_the_receipt_names_the_tool_the_package_and_the_target(bundle: Path) -> None:
    write_native(bundle)
    manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))

    receipt = receipt_of(bundle, tool_revision="9ceb22e3", adapter_revision="abc1234")

    assert receipt["tool"] == {
        "id": "midojo",
        "revision": "9ceb22e3",
        "adapter_revision": "abc1234",
    }
    assert receipt["package"] == {
        "id": manifest["scenario_id"],
        "digest": manifest["package_digest"],
    }
    assert receipt["target"] == {
        "mode": "orch_hosted",
        "image_digest": None,
        "policy_digest": None,
        "sandbox_id": None,
    }


def test_the_attempt_carries_the_turn_the_reply_and_the_native_tool_calls(bundle: Path) -> None:
    write_native(bundle)

    attempt = only_attempt(receipt_of(bundle))

    assert attempt["index"] == 0
    assert attempt["generation"] == {
        "request_count": None,
        "response_status": "completed",
        "error_type": None,
    }
    assert attempt["turns"] == [
        {
            "index": 0,
            "request": {"messages": [{"role": "user", "content": PROMPT}]},
            "response": {"assistant_messages": ["Done."], "tool_call_indices": [0]},
        }
    ]
    assert attempt["observation"] == {
        "source": "tool_native",
        "assistant_messages": ["Done."],
        "tool_calls": [
            {
                "name": "get_referral",
                "arguments": {"patient_id": "PAT-201"},
                "result": {"referral": "cardiology"},
                "error": None,
                "status": None,
                "turn_index": 0,
            }
        ],
    }
    assert attempt["runtime_observations"]["source"] is None


def test_an_empty_reply_gives_no_assistant_message(bundle: Path) -> None:
    write_native(bundle, evaluation=evaluation_dump(output=None, calls=()))

    attempt = only_attempt(receipt_of(bundle))

    assert attempt["turns"][0]["response"] == {"assistant_messages": [], "tool_call_indices": []}
    assert attempt["observation"]["tool_calls"] == []


def test_a_call_error_and_a_text_result_are_kept(bundle: Path) -> None:
    call = {**CALL, "result": "plain text", "error": "boom"}
    write_native(bundle, evaluation=evaluation_dump(calls=(call,)))

    (tool_call,) = only_attempt(receipt_of(bundle))["observation"]["tool_calls"]

    assert tool_call["result"] == "plain text"
    assert tool_call["error"] == "boom"


def test_the_native_list_hashes_each_output_beside_the_receipt(bundle: Path) -> None:
    write_native(bundle)

    native = receipt_of(bundle)["native"]

    assert [item["path"] for item in native] == [
        "control-plane/run.json",
        "control-plane/evaluation.json",
        "midojo-logs/results.json",
        "verdict.json",
    ]
    for item in native:
        digest = hashlib.sha256((bundle / item["path"]).read_bytes()).hexdigest()
        assert item["sha256"] == digest


FAILURES = {
    "control_plane_missing": lambda b: (b / "control-plane/run.json").unlink(),
    "control_plane_missing:evaluation": lambda b: (b / "control-plane/evaluation.json").unlink(),
    "verdict_missing": lambda b: (b / "verdict.json").unlink(),
}


@pytest.mark.parametrize("name", sorted(FAILURES))
def test_a_missing_native_file_gives_a_failed_receipt(bundle: Path, name: str) -> None:
    write_native(bundle)
    FAILURES[name](bundle)

    receipt = receipt_of(bundle)

    assert receipt["execution_status"] == "failed"
    assert receipt["runtime_status"] is None
    assert receipt["incomplete_reason"] == name.split(":")[0]
    assert receipt["attempts"] == []


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("control-plane/run.json", "control_plane_malformed"),
        ("control-plane/evaluation.json", "control_plane_malformed"),
        ("verdict.json", "verdict_malformed"),
    ],
)
@pytest.mark.parametrize("content", ["{not json", "[]", '"text"'])
def test_a_malformed_native_file_gives_a_failed_receipt(
    bundle: Path, name: str, reason: str, content: str
) -> None:
    write_native(bundle)
    (bundle / name).write_text(content, encoding="utf-8")

    receipt = receipt_of(bundle)

    assert receipt["incomplete_reason"] == reason
    assert receipt["attempts"] == []


@pytest.mark.parametrize(
    ("case", "reason", "native"),
    [
        ("two evaluations", "evaluation_count_mismatch", {"run": run_dump(("eval-1", "eval-2"))}),
        ("no evaluation", "evaluation_count_mismatch", {"run": run_dump(())}),
        ("other evaluation", "evaluation_mismatch", {"run": run_dump(("eval-9",))}),
        ("ungraded", "evaluation_ungraded", {"evaluation": evaluation_dump(security=None)}),
        ("not completed", "evaluation_ungraded", {"evaluation": evaluation_dump(completed=False)}),
        (
            "detected but not secure",
            "verdict_disagrees",
            {"evaluation": evaluation_dump(security=False), "verdict": verdict_dump("detected")},
        ),
        (
            "secure but not detected",
            "verdict_disagrees",
            {
                "evaluation": evaluation_dump(security=True),
                "verdict": verdict_dump("not_detected"),
            },
        ),
        (
            "secure but inconclusive",
            "verdict_disagrees",
            {
                "evaluation": evaluation_dump(security=True),
                "verdict": verdict_dump("inconclusive"),
            },
        ),
        (
            "unknown outcome",
            "verdict_malformed",
            {"verdict": {**verdict_dump(), "outcome": "maybe"}},
        ),
        ("wrong claim", "verdict_malformed", {"verdict": verdict_dump(claim_level="reply")}),
        ("no reason", "verdict_malformed", {"verdict": verdict_dump(reason="")}),
        ("matched not a list", "verdict_malformed", {"verdict": verdict_dump(matched_calls=3)}),
        (
            "other prompt",
            "agent_input_mismatch",
            {"evaluation": evaluation_dump(prompt="Another question.")},
        ),
        (
            "calls not a list",
            "function_calls_malformed",
            {"evaluation": {**evaluation_dump(), "function_calls": "x"}},
        ),
        (
            "call not an object",
            "function_calls_malformed",
            {"evaluation": {**evaluation_dump(), "function_calls": [3]}},
        ),
    ],
)
def test_an_inconsistent_record_gives_a_failed_receipt(
    bundle: Path, case: str, reason: str, native: dict[str, Any]
) -> None:
    del case  # names the parametrized row in the report
    write_native(bundle, **native)

    receipt = receipt_of(bundle)

    assert receipt["execution_status"] == "failed"
    assert receipt["incomplete_reason"] == reason
    assert receipt["attempts"] == []
    assert len(receipt["native"]) == 4


def write_records(
    directory: Path,
    calls: list[dict[str, Any]],
    *,
    requests: int | None = 1,
) -> Path:
    capture = directory / "mcp_capture"
    capture.mkdir(parents=True)
    (capture / "calls.jsonl").write_text(
        "".join(json.dumps(call) + "\n" for call in calls), encoding="utf-8"
    )
    if requests is not None:
        (directory / "boundary").mkdir()
        (directory / "boundary/gateway-accounting.json").write_text(
            json.dumps({"responses_request_count": requests}), encoding="utf-8"
        )
    return directory


def proxy_call(**changes: Any) -> dict[str, Any]:
    return {
        "sequence": 0,
        "session": "s-1",
        "jsonrpc_id": 4,
        "name": "get_referral",
        "arguments": {"patient_id": "PAT-201"},
        "status": "ok",
        "result": {"content": [{"type": "text", "text": '{"referral": "cardiology"}'}]},
        "error": None,
        "sink": "ok",
        **changes,
    }


def test_proxy_records_replace_the_control_plane_calls_and_give_the_request_count(
    bundle: Path, tmp_path: Path
) -> None:
    write_native(bundle)
    records = write_records(tmp_path / "records", [proxy_call()])

    attempt = only_attempt(receipt_of(bundle, records_dir=records))

    assert attempt["generation"]["request_count"] == 1
    assert attempt["observation"]["source"] == "mcp_recording_proxy"
    assert attempt["observation"]["tool_calls"] == [
        {
            "name": "get_referral",
            "arguments": {"patient_id": "PAT-201"},
            "result": {"referral": "cardiology"},
            "error": None,
            "status": "ok",
            "turn_index": 0,
        }
    ]


def test_a_proxy_record_flagged_as_an_error_keeps_the_text_as_the_error(
    bundle: Path, tmp_path: Path
) -> None:
    write_native(bundle)
    failing = proxy_call(
        status="error",
        result={"isError": True, "content": [{"type": "text", "text": "no such patient"}]},
    )
    records = write_records(tmp_path / "records", [failing])

    (call,) = only_attempt(receipt_of(bundle, records_dir=records))["observation"]["tool_calls"]

    assert call["result"] is None
    assert call["error"] == "no such patient"


@pytest.mark.parametrize("sink", ["failed", "no_token", None, 3])
def test_a_proxy_record_the_sink_did_not_deliver_gives_a_failed_receipt(
    bundle: Path, tmp_path: Path, sink: Any
) -> None:
    write_native(bundle)
    records = write_records(tmp_path / "records", [proxy_call(sink=sink)])

    receipt = receipt_of(bundle, records_dir=records)

    assert receipt["incomplete_reason"] == "sink_failed"
    assert receipt["attempts"] == []


def test_records_from_a_run_without_a_sink_pass_unchecked(bundle: Path, tmp_path: Path) -> None:
    write_native(bundle)
    call = proxy_call()
    del call["sink"]
    records = write_records(tmp_path / "records", [call])

    assert receipt_of(bundle, records_dir=records)["execution_status"] == "completed"


@pytest.mark.parametrize("count", [0, 2])
def test_a_proxy_and_control_plane_that_disagree_on_the_call_count_give_a_failed_receipt(
    bundle: Path, tmp_path: Path, count: int
) -> None:
    write_native(bundle)
    records = write_records(tmp_path / "records", [proxy_call()] * count)

    receipt = receipt_of(bundle, records_dir=records)

    assert receipt["incomplete_reason"] == "tool_call_count_mismatch"


def test_records_without_an_accounting_file_leave_the_request_count_unknown(
    bundle: Path, tmp_path: Path
) -> None:
    write_native(bundle)
    records = write_records(tmp_path / "records", [proxy_call()], requests=None)

    assert (
        only_attempt(receipt_of(bundle, records_dir=records))["generation"]["request_count"]
        is None
    )


def test_a_proxy_record_that_is_not_json_gives_a_failed_receipt(
    bundle: Path, tmp_path: Path
) -> None:
    write_native(bundle)
    records = tmp_path / "records"
    (records / "mcp_capture").mkdir(parents=True)
    (records / "mcp_capture/calls.jsonl").write_text("{broken\n", encoding="utf-8")

    receipt = receipt_of(bundle, records_dir=records)

    assert receipt["incomplete_reason"] == "records_malformed"


def test_one_undelivered_record_among_sunk_ones_gives_a_failed_receipt(
    bundle: Path, tmp_path: Path
) -> None:
    write_native(bundle, evaluation=evaluation_dump(calls=(CALL, CALL)))
    unsunk = proxy_call()
    del unsunk["sink"]
    records = write_records(tmp_path / "records", [proxy_call(), unsunk])

    assert receipt_of(bundle, records_dir=records)["incomplete_reason"] == "sink_failed"


def test_the_bundle_digest_covers_the_suite_the_code_and_the_manifest(bundle: Path) -> None:
    write_native(bundle)
    before = receipt_of(bundle)["bundle_digest"]

    (bundle / "results-note.txt").write_text("x", encoding="utf-8")
    write_native(bundle, results={"run_id": "other"})
    unchanged = receipt_of(bundle)["bundle_digest"]
    changed = {}
    for name in (
        "asago_suite/suite.yaml",
        "asago_suite/asago_verifiers.py",
        "asago_suite/suite.py",
        "asago_suite/__init__.py",
        "run_midojo.py",
        "bundle.json",
    ):
        original = (bundle / name).read_bytes()
        (bundle / name).write_bytes(original + b"\n")
        changed[name] = receipt_of(bundle)["bundle_digest"]
        (bundle / name).write_bytes(original)

    assert unchanged == before
    assert len({before, *changed.values()}) == 7


def test_a_bundle_that_cannot_be_read_raises(tmp_path: Path) -> None:
    with pytest.raises(ParseError, match="unreadable"):
        parse_bundle(tmp_path, tmp_path / "receipt.json")


def test_a_bundle_manifest_missing_keys_raises(bundle: Path) -> None:
    (bundle / "bundle.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ParseError, match="lacks"):
        parse_bundle(bundle, bundle / "receipt.json")


def test_a_native_file_outside_the_receipt_directory_raises(bundle: Path, tmp_path: Path) -> None:
    write_native(bundle)
    (tmp_path / "elsewhere").mkdir()

    with pytest.raises(ParseError, match="receipt's directory"):
        parse_bundle(bundle, tmp_path / "elsewhere" / "receipt.json")
