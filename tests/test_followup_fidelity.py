"""Focused regressions for the owner-authorized offline follow-up."""

from __future__ import annotations

import json
from pathlib import Path

from asago_artifact_generator.authoring.contracts import (
    neutral_artifact_plan_v2,
    neutral_observation_cases,
)
from asago_artifact_generator.detector_runtime import execute_detector

from .support import (
    ScriptedAuthoringTransport,
    build_neutral_artifact_package,
    neutral_observation_results,
    stage_local_orchestrator,
    validate_neutral_example,
)

HANDOFF = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "scenario-handoff"
    / "handoff-v1"
    / "valid"
    / "adversarial-refund.json"
)


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


def test_neutral_detector_scans_decisive_event_before_partial_capture(tmp_path: Path) -> None:
    package = build_neutral_artifact_package(tmp_path / "neutral")
    cases = neutral_observation_cases()
    results = neutral_observation_results()

    assert len(cases) == 7
    assert set(results) == set(cases)
    assert results["decisive_event_with_partial_capture"]["claim_level"] == "command_attempt"
    assert results["decisive_event_with_partial_capture"]["outcome"] == "detected"
    for name, evidence in cases.items():
        execution = execute_detector(package, evidence)
        assert execution.status == "completed", (name, execution.failure)
        assert execution.result == results[name]


def test_neutral_example_is_complete_and_runs_with_exact_detector_bytes(
    tmp_path: Path,
) -> None:
    package = build_neutral_artifact_package(tmp_path / "neutral")
    assert validate_neutral_example() == []

    execution = execute_detector(
        package,
        neutral_observation_cases()["decisive_event"],
    )

    assert execution.status == "completed"
    assert execution.result == neutral_observation_results()["decisive_event"]
    assert "def evaluate(evidence: dict) -> dict" in package.joinpath("detector.py").read_text()


def test_correction_size_accounting_is_evidence_only(tmp_path: Path) -> None:
    invalid = b"```json\n{}\n```\n```python\nnot python\n```\n"
    result = stage_local_orchestrator(
        transport=ScriptedAuthoringTransport(
            [json.dumps(neutral_artifact_plan_v2()), invalid, invalid]
        ),
        package_dir=tmp_path / "package",
        task_id="size-followup",
    ).run(
        _view(),
        _inventory(),
        _runtime_contract(),
    )

    assert result.status == "unresolved"
    correction = result.prompts["correction"]
    assert "size_comparison" not in correction.payload
    assert "tokens" not in correction.payload
    assert "cost" not in correction.payload
    assert correction.payload["failed_response"] == invalid.decode()
