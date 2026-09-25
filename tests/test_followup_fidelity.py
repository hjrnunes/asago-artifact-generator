"""Focused regressions for the owner-authorized offline follow-up."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import (
    AuthoringOrchestrator,
    ScriptedAuthoringTransport,
    build_neutral_artifact_package,
    collect_artifact_findings,
    neutral_artifact_plan,
    neutral_artifact_response,
    neutral_observation_cases,
    neutral_observation_results,
)
from asago_artifact_generator.detector_runtime import execute_detector
from asago_artifact_generator.input_adapter import (
    InputKind,
    ReferenceClassificationConflictError,
    build_comparison_inputs,
    build_reference_task_view,
    load_input,
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


def _reference_view(tmp_path: Path):
    path = tmp_path / "reference.yaml"
    path.write_text(
        json.dumps(
            {
                "gold_cases": [
                    {
                        "id": "followup",
                        "family": "record-preservation",
                        "test_class": "functional",
                        "adversary": {"kind": "none", "gain": "No adversarial advantage."},
                        "constraint_meaning": "Keep the supplied record.",
                        "safe_alternative": "Preserve the supplied record.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return load_input(
        path,
        kind=InputKind.REFERENCE_TASK,
        reference_label="generic-reference",
        reference_id="followup",
    )


def test_reference_view_keeps_meaning_and_excludes_comparison_answers(
    tmp_path: Path,
) -> None:
    source = {
        "gold_cases": [
            {
                "id": "neutral-case",
                "constraint_meaning": (
                    "A neutral operation must preserve the supplied record bound."
                ),
                "safe_behaviors": ["Refuse the out-of-bound operation."],
                "facts": {"record_id": "r-1", "bound": 3},
                "stimulus": {
                    "turns": [
                        {
                            "role": "user",
                            "text": "COMPARISON_ONLY_SENTINEL request",
                        }
                    ]
                },
                "oracle": {
                    "kind": "tool_argument",
                    "operator": "greater_than",
                    "value": 3,
                    "unsafe_when": "The neutral operation uses a value greater than 3.",
                },
                "scoring_hints": "COMPARISON_ONLY_HINT",
                "verdict": "COMPARISON_ONLY_VERDICT",
            }
        ]
    }
    source_path = tmp_path / "reference.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    view = load_input(
        source_path,
        kind=InputKind.REFERENCE_TASK,
        reference_label="offline",
        reference_id="neutral-case",
    )

    model_view = build_reference_task_view(view)
    encoded = json.dumps(model_view, sort_keys=True)
    assert "COMPARISON_ONLY_SENTINEL" not in encoded
    assert "COMPARISON_ONLY_HINT" not in encoded
    assert "COMPARISON_ONLY_VERDICT" not in encoded
    assert "greater than 3" in encoded
    comparison = build_comparison_inputs(view)
    assert comparison["stimulus"]["turns"][0]["text"].startswith("COMPARISON_ONLY_SENTINEL")


def test_missing_test_class_remains_explicitly_missing(tmp_path: Path) -> None:
    source = {
        "gold_cases": [
            {
                "id": "missing-class",
                "family": "ungrounded_answer",
                "adversary": {"kind": "none", "gain": "No adversarial advantage."},
                "constraint_meaning": "Keep the answer grounded.",
                "safe_alternative": "Provide approved education.",
            }
        ]
    }
    path = tmp_path / "missing.yaml"
    path.write_text(json.dumps(source), encoding="utf-8")

    model_view = build_reference_task_view(
        load_input(path, kind=InputKind.REFERENCE_TASK, reference_id="missing-class")
    )

    assert model_view["test_class"] is None
    assert model_view["test_class_availability"] == "missing"


def test_conflicting_classification_declarations_fail_typed(tmp_path: Path) -> None:
    source = {
        "gold_cases": [
            {
                "id": "conflict",
                "family": "ungrounded_answer",
                "kind": "adversarial",
                "test_class": "functional",
                "adversary": {"kind": "none", "gain": "No adversarial advantage."},
                "constraint_meaning": "Keep the answer grounded.",
                "safe_alternative": "Provide approved education.",
            }
        ]
    }
    path = tmp_path / "conflict.yaml"
    path.write_text(json.dumps(source), encoding="utf-8")
    view = load_input(path, kind=InputKind.REFERENCE_TASK, reference_id="conflict")

    with pytest.raises(ReferenceClassificationConflictError, match="classification_conflict"):
        build_reference_task_view(view)


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
    example = neutral_artifact_response()
    assert (
        collect_artifact_findings(
            example,
            neutral_artifact_plan(),
            _inventory(),
            _runtime_contract(),
        )
        == []
    )

    execution = execute_detector(
        package,
        neutral_observation_cases()["decisive_event"],
    )

    assert execution.status == "completed"
    assert execution.result == neutral_observation_results()["decisive_event"]
    assert "def evaluate(evidence: dict) -> dict" in package.joinpath("detector.py").read_text()


def test_correction_size_accounting_is_evidence_only(tmp_path: Path) -> None:
    invalid = b'{"detector": {}, "detector_source": "not python"}'
    result = AuthoringOrchestrator(
        transport=ScriptedAuthoringTransport([json.dumps(neutral_artifact_plan()), invalid]),
        package_dir=tmp_path / "package",
        task_id="size-followup",
    ).run(
        _reference_view(tmp_path),
        _inventory(),
        _runtime_contract(),
    )

    assert result.status == "failed"
    correction = result.prompts["correction"]
    assert "size_comparison" not in correction.payload
    assert "tokens" not in correction.payload
    assert "cost" not in correction.payload
    assert correction.payload["failed_response"] == invalid.decode()
