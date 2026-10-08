"""Focused regressions for the owner-authorized offline follow-up."""

from __future__ import annotations

import json
from pathlib import Path

from asago_artifact_generator.authoring.contracts import neutral_artifact_plan_v2

from .support import HANDOFF, ScriptedAuthoringTransport, stage_local_orchestrator


def _inventory() -> dict:
    return {
        "operations": [
            {
                "name": "inspect_record",
                "description": "Inspect one ordinary record.",
                "arguments": {"type": "object"},
                "result_schema": {"type": "object"},
            }
        ],
        "facts": [],
        "source_handles": [],
    }


def _runtime_contract() -> dict:
    return {
        "delivery": ["direct_user_message"],
        "observation": {"tool_calls": {"availability": "captured_or_unavailable"}},
        "setup_permissions": [],
        "limits": {"max_turns": 2},
    }


def _view():
    from asago_artifact_generator.input_adapter import load_input

    return load_input(HANDOFF)


def test_correction_size_accounting_is_evidence_only(tmp_path: Path) -> None:
    invalid = b"```json\n{}\n```\n"
    transport = ScriptedAuthoringTransport(
        [json.dumps(neutral_artifact_plan_v2()), invalid, invalid]
    )
    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="size-followup",
    ).run(
        _view(),
        _inventory(),
        _runtime_contract(),
    )

    assert result.status == "unresolved"
    corrections = [request for request in transport.requests if request["stage"] == "correction"]
    payload = corrections[-1]["payload"]
    assert "size_comparison" not in payload
    assert "tokens" not in payload
    assert "cost" not in payload
    assert payload["current_output"] == invalid.decode()
    assert "failed_response" not in payload
