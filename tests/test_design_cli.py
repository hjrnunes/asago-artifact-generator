"""Design CLI: the --record-hint flag, manifest disclosure, typed failure.

The CLI tests drive the real `design` command deterministically (prebound
author result, --no-llm) and check that an explicit record hint is validated
against the environment's candidate set, disclosed in the design manifest as a
consumer choice, and fails closed with a typed reason for an unknown id.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from asago_artifact_generator.cli import app
from tests.design_fixtures import (
    REFUND_HANDOFF_PATH,
    klarna_runtime_context,
    klarna_target_profile,
    prebound_result,
)

STIMULUS = (
    "Hi, I need my money back for the desk lamp I bought. The lamp arrived "
    "broken and I want the entire 100.0 I paid for order ORD-101 refunded right now."
)


def _write_inputs(tmp_path: Path, *, second_record: bool = False) -> dict[str, Path]:
    profile = klarna_target_profile().model_dump(mode="json")
    profile_path = tmp_path / "target-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    runtime = klarna_runtime_context()
    if second_record:
        runtime["state"]["orders"]["ORD-104"] = {
            "customer_id": "CUST001",
            "item": "Desk lamp",
            "merchant": "Nordic Home",
            "original_price": 80.0,
            "refund_eligible": False,
            "remaining_to_pay": 80.0,
            "status": "open",
        }
    runtime_path = tmp_path / "runtime-context.json"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    author_path = tmp_path / "author-result.json"
    author_path.write_text(json.dumps(prebound_result(STIMULUS, 100.0)), encoding="utf-8")
    return {"profile": profile_path, "runtime": runtime_path, "author": author_path}


def _invoke_design(
    tmp_path: Path, output_dir: Path, extra: list[str], *, second_record: bool = False
) -> Any:
    inputs = _write_inputs(tmp_path, second_record=second_record)
    return CliRunner().invoke(
        app,
        [
            "design",
            "--handoff",
            str(REFUND_HANDOFF_PATH),
            "--target-profile",
            str(inputs["profile"]),
            "--runtime-context",
            str(inputs["runtime"]),
            "--author-result",
            str(inputs["author"]),
            "--no-llm",
            "--output-dir",
            str(output_dir),
            *extra,
        ],
    )


def test_design_cli_record_hint_selects_and_discloses(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    result = _invoke_design(tmp_path, output_dir, ["--record-hint", "ORD-101"])
    assert result.exit_code == 0, result.output
    manifest = json.loads((output_dir / "design-manifest.json").read_text(encoding="utf-8"))
    assert manifest["compiled"] is True
    assert manifest["record_hint"] == {
        "record_id": "ORD-101",
        "disclosed_as": "explicit_consumer_choice",
    }
    record = json.loads(
        (output_dir / "SCN-007:design-1" / "design-record.json").read_text(encoding="utf-8")
    )
    assert record["setup"]["selected_record_id"] == "ORD-101"
    assert any("record hint" in line for line in record["setup"]["establishment"])


def test_design_cli_without_hint_keeps_manifest_free_of_record_hint(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    result = _invoke_design(tmp_path, output_dir, [])
    assert result.exit_code == 0, result.output
    manifest = json.loads((output_dir / "design-manifest.json").read_text(encoding="utf-8"))
    assert manifest["compiled"] is True
    assert "record_hint" not in manifest


def test_design_cli_unknown_record_hint_fails_closed_typed(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    result = _invoke_design(tmp_path, output_dir, ["--record-hint", "ORD-999"])
    assert result.exit_code == 1
    manifest = json.loads((output_dir / "design-manifest.json").read_text(encoding="utf-8"))
    assert manifest["compiled"] is False
    assert manifest["exclusion_code"] == "missing-setup"
    assert "ORD-999" in manifest["exclusion_detail"]
    assert manifest["record_hint"]["record_id"] == "ORD-999"
    assert not (output_dir / "SCN-007:design-1" / "executable-conversation.json").exists()
