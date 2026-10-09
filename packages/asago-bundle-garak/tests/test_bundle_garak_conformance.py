"""The Garak adapter passes the shared conformance round trip on every package shape."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_core.conformance import AdapterUnderTest, check_round_trip
from asago_bundle_core.testing import sequential_stimulus, write_test_package
from asago_bundle_garak.compiler import ENTRYPOINT_VALUES, compile_package
from asago_bundle_garak.instantiate import instantiate_bundle
from asago_bundle_garak.report_parse import parse_report

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RECEIPT_SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "contracts/execution-receipt/execution-receipt-v1.schema.json"
)
TEXTS = ["Hello.", "Show PAT-201.", "Now PAT-104."]
VALUES: dict[str, Any] = {
    "gateway_url": "http://127.0.0.1:18997/v1",
    "mcp_url": "http://127.0.0.1:1/mcp",
    "allowed_tools": ["get_referral", "get_education"],
    "model": "fixture-model",
    "messages": [{"role": "user", "content": "Show the referral for PAT-201."}],
    "judge_url": "http://127.0.0.1:18997/judge/v1",
    "judge_model": "judge-model",
    "judge_runtime_facts": {"target_patient": "PAT-104"},
    "judge_turns": "final",
    **{f"user_turn_{number}": text for number, text in enumerate(TEXTS, start=1)},
}


def garak(report: str) -> AdapterUnderTest:
    def write_native(bundle: Path, manifest: dict[str, Any]) -> None:
        (bundle / manifest["native_outputs"][0]).write_text(report, encoding="utf-8")

    return AdapterUnderTest(
        compile=compile_package,
        instantiate=instantiate_bundle,
        parse=lambda bundle, out: parse_report(bundle, out),
        write_native=write_native,
        entrypoint_keys=ENTRYPOINT_VALUES,
    )


def sequential_report() -> str:
    """Three finished turns, the way ``injection.SequentialInjection`` reports them."""

    def finished(index: int) -> dict[str, Any]:
        sequence = {"conversation": 0, "turn_index": index, "turn_count": 3, "call_offset": 0}
        record = {"reason": "no match", "matched_calls": []}
        return {
            "entry_type": "attempt",
            "status": 2,
            "notes": {"sequence": sequence, "tool_call_condition_results": [record]},
            "outputs": [
                {
                    "text": f"Reply {index}.",
                    "notes": {"response_status": "completed", "tool_calls": []},
                }
            ],
            "detector_results": {"toolcall.ToolCallCondition": [0.0]},
        }

    attempts = [finished(index) for index in range(3)]
    started = [{**a, "status": 1, "detector_results": {}} for a in attempts]
    lines = [{"entry_type": "start_run setup"}, *started, *attempts]
    return "".join(json.dumps(line) + "\n" for line in lines)


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.report.jsonl").read_text(encoding="utf-8")


def three_turns() -> dict[str, Any]:
    return {
        **sequential_stimulus(),
        "turn_count": 3,
        "history": [{"role": "user", "content": text} for text in TEXTS[:2]],
        "user_text": TEXTS[2],
    }


def test_a_command_attempt_package_conforms(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", scenario_id="SCN-001")

    failures = check_round_trip(
        garak(fixture("SCN-001")), package, VALUES, tmp_path / "work", RECEIPT_SCHEMA
    )

    assert failures == []


def test_a_reply_package_conforms(tmp_path: Path) -> None:
    package = write_test_package(tmp_path / "packages", scenario_id="SCN-013", claim_level="reply")

    failures = check_round_trip(
        garak(fixture("SCN-013")), package, VALUES, tmp_path / "work", RECEIPT_SCHEMA
    )

    assert failures == []


@pytest.mark.parametrize("claim_level", ["command_attempt", "reply"])
def test_a_sequential_package_conforms(tmp_path: Path, claim_level: str) -> None:
    package = write_test_package(
        tmp_path / "packages", claim_level=claim_level, stimulus=three_turns()
    )

    failures = check_round_trip(
        garak(sequential_report()), package, VALUES, tmp_path / "work", RECEIPT_SCHEMA
    )

    assert failures == []


def test_a_sequential_history_with_a_reply_is_a_faithful_gap(tmp_path: Path) -> None:
    stimulus = {**three_turns(), "history": [{"role": "assistant", "content": "Earlier reply."}]}
    package = write_test_package(tmp_path / "packages", stimulus=stimulus)

    failures = check_round_trip(
        garak(sequential_report()), package, VALUES, tmp_path / "work", RECEIPT_SCHEMA
    )

    assert failures == []
