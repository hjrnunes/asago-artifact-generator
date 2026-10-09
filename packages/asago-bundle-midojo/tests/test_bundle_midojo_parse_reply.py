"""parse: a reply bundle's rubric verdict becomes the receipt's detection and judge record."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_bundle_midojo.parse import parse_bundle
from conftest import (
    CALL,
    PROMPT,
    RECEIPT_SCHEMA,
    evaluation_dump,
    reply_verdict_dump,
    run_dump,
    write_native,
)

VALIDATOR = jsonschema.Draft202012Validator(json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8")))


def receipt_of(bundle: Path, **options: Any) -> dict[str, Any]:
    receipt = parse_bundle(bundle, bundle / "receipt.json", **options)
    assert list(VALIDATOR.iter_errors(receipt)) == []
    return receipt


def only_attempt(receipt: dict[str, Any]) -> dict[str, Any]:
    (attempt,) = receipt["attempts"]
    return attempt


@pytest.mark.parametrize(
    ("verdict", "outcome", "security"),
    [
        ("supported", "detected", True),
        ("contradicted", "not_detected", False),
        ("unresolved", "inconclusive", False),
    ],
)
def test_a_graded_reply_gives_the_rubric_detection_and_the_judge_record(
    reply_bundle: Path, verdict: str, outcome: str, security: bool
) -> None:
    write_native(
        reply_bundle,
        evaluation=evaluation_dump(security=security),
        verdict=reply_verdict_dump(verdict),
    )

    receipt = receipt_of(reply_bundle)

    assert receipt["execution_status"] == "completed"
    attempt = only_attempt(receipt)
    assert attempt["result"] == {
        "outcome": outcome,
        "claim_level": "reply",
        "reason": "judge_completed",
    }
    assert attempt["detections"] == [
        {
            "detector": "RubricJudge",
            "outcome": outcome,
            "reason": "judge_completed",
            "matched_calls": None,
        }
    ]
    assert attempt["judge"] == {
        "verdict": verdict,
        "reason": "judge_completed",
        "request_count": 1,
    }


def test_a_reply_receipt_keeps_the_turn_the_reply_and_the_recorded_calls(
    reply_bundle: Path,
) -> None:
    write_native(reply_bundle, verdict=reply_verdict_dump())

    attempt = only_attempt(receipt_of(reply_bundle))

    assert attempt["turns"] == [
        {
            "index": 0,
            "request": {"messages": [{"role": "user", "content": PROMPT}]},
            "response": {"assistant_messages": ["Done."], "tool_call_indices": [0]},
        }
    ]
    assert attempt["observation"]["source"] == "tool_native"
    assert attempt["observation"]["tool_calls"][0]["name"] == CALL["function"]


@pytest.mark.parametrize(
    "reason", ["judge_failed:APIConnectionError", "judge_support_unresolved", "judge_parse_failed"]
)
def test_a_judge_that_did_not_decide_is_an_inconclusive_reply_with_its_reason(
    reply_bundle: Path, reason: str
) -> None:
    write_native(
        reply_bundle,
        evaluation=evaluation_dump(security=False),
        verdict=reply_verdict_dump("unresolved", reason=reason),
    )

    attempt = only_attempt(receipt_of(reply_bundle))

    assert attempt["result"]["outcome"] == "inconclusive"
    assert attempt["result"]["reason"] == reason
    assert attempt["judge"]["reason"] == reason


def test_a_judge_that_was_never_reached_counts_no_request(reply_bundle: Path) -> None:
    write_native(
        reply_bundle,
        evaluation=evaluation_dump(security=False),
        verdict=reply_verdict_dump("unresolved", request_count=0),
    )

    assert only_attempt(receipt_of(reply_bundle))["judge"]["request_count"] == 0


def write_exchanges(directory: Path, lines: int) -> Path:
    (directory / "boundary").mkdir(parents=True)
    (directory / "boundary/judge-exchanges.jsonl").write_text(
        "".join(json.dumps({"status": 200}) + "\n" for _ in range(lines)), encoding="utf-8"
    )
    return directory


@pytest.mark.parametrize("lines", [0, 1, 2])
def test_the_boundary_judge_exchanges_give_the_request_count(
    reply_bundle: Path, tmp_path: Path, lines: int
) -> None:
    write_native(reply_bundle, verdict=reply_verdict_dump())
    records = write_exchanges(tmp_path / "records", lines)

    receipt = receipt_of(reply_bundle, records_dir=records)

    assert only_attempt(receipt)["judge"]["request_count"] == lines


def test_without_exchanges_the_verdict_files_count_stands(
    reply_bundle: Path, tmp_path: Path
) -> None:
    write_native(reply_bundle, verdict=reply_verdict_dump(request_count=0))
    records = tmp_path / "records"
    (records / "boundary").mkdir(parents=True)
    (records / "boundary/gateway-accounting.json").write_text(
        json.dumps({"responses_request_count": 1}), encoding="utf-8"
    )

    attempt = only_attempt(receipt_of(reply_bundle, records_dir=records))

    assert attempt["judge"]["request_count"] == 0
    assert attempt["generation"]["request_count"] == 1


@pytest.mark.parametrize(
    ("case", "verdict"),
    [
        ("command claim", reply_verdict_dump(claim_level="command_attempt")),
        ("no judge verdict", {k: v for k, v in reply_verdict_dump().items() if k != "verdict"}),
        ("unknown judge verdict", {**reply_verdict_dump(), "verdict": "maybe"}),
        ("outcome does not follow verdict", reply_verdict_dump(outcome="not_detected")),
        ("refs not a list", reply_verdict_dump(evidence_refs="messages[0]")),
        ("ref not text", reply_verdict_dump(evidence_refs=[3])),
        ("negative count", reply_verdict_dump(request_count=-1)),
        ("count not an int", reply_verdict_dump(request_count="1")),
        ("count is a bool", reply_verdict_dump(request_count=True)),
        ("no reason", reply_verdict_dump(reason="")),
    ],
)
def test_a_malformed_reply_verdict_gives_a_failed_receipt(
    reply_bundle: Path, case: str, verdict: dict[str, Any]
) -> None:
    del case
    write_native(reply_bundle, verdict=verdict)

    receipt = receipt_of(reply_bundle)

    assert receipt["incomplete_reason"] == "verdict_malformed"
    assert receipt["attempts"] == []


def test_a_command_verdict_in_a_reply_bundle_is_malformed(reply_bundle: Path) -> None:
    from conftest import verdict_dump

    write_native(reply_bundle, verdict=verdict_dump())

    assert receipt_of(reply_bundle)["incomplete_reason"] == "verdict_malformed"


def test_a_reply_verdict_in_a_command_bundle_is_malformed(bundle: Path) -> None:
    write_native(bundle, verdict=reply_verdict_dump())

    assert receipt_of(bundle)["incomplete_reason"] == "verdict_malformed"


def test_a_supported_verdict_with_a_false_security_flag_disagrees(reply_bundle: Path) -> None:
    write_native(
        reply_bundle, evaluation=evaluation_dump(security=False), verdict=reply_verdict_dump()
    )

    assert receipt_of(reply_bundle)["incomplete_reason"] == "verdict_disagrees"


def test_an_ungraded_reply_evaluation_stays_ungraded(reply_bundle: Path) -> None:
    write_native(reply_bundle, evaluation=evaluation_dump(security=None))
    (reply_bundle / "verdict.json").unlink()

    assert receipt_of(reply_bundle)["incomplete_reason"] == "evaluation_ungraded"


def test_a_reply_run_still_needs_exactly_one_evaluation(reply_bundle: Path) -> None:
    write_native(reply_bundle, run=run_dump(("eval-1", "eval-2")), verdict=reply_verdict_dump())

    assert receipt_of(reply_bundle)["incomplete_reason"] == "evaluation_count_mismatch"
