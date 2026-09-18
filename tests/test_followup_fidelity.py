"""Focused regressions for the owner-authorized offline follow-up."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import (
    AuthoringOrchestrator,
    ScriptedAuthoringTransport,
    build_call1_packet,
    build_call2_packet,
    build_neutral_artifact_package,
    neutral_artifact_plan,
    neutral_observation_cases,
    neutral_observation_results,
)
from asago_artifact_generator.detector_runtime import execute_detector
from asago_artifact_generator.input_adapter import (
    InputKind,
    ReferenceClassificationConflictError,
    build_reference_task_view,
    load_input,
)

PROJECT_ROOT = Path(__file__).resolve().parents[4]
MINIOCCIAI_GOLD = PROJECT_ROOT / "data" / "gold" / "miniocciai" / "gold-cases.yaml"


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


def _load_occi_case(case_id: str):
    return load_input(
        MINIOCCIAI_GOLD,
        kind=InputKind.REFERENCE_TASK,
        reference_label="miniocciai-followup-control",
        reference_id=case_id,
    )


def test_actual_o04_rendered_views_keep_family_class_and_adversary_separate() -> None:
    view = _load_occi_case("O04")
    model_view = build_reference_task_view(view)
    call1 = build_call1_packet(view, _inventory(), _runtime_contract())
    call2 = build_call2_packet(
        view,
        neutral_artifact_plan(),
        _inventory(),
        _runtime_contract(),
    )

    assert model_view["family"] == "ungrounded_answer"
    assert model_view["test_class"] == "functional"
    assert model_view["adversary"] == {
        "kind": "none",
        "gain": (
            "Harm is an unsupported personalized claim replacing routine approved education; "
            "the legitimate patient request establishes no adversarial advantage."
        ),
    }
    for rendered in (call1.user, call2.user):
        assert '"family":"ungrounded_answer"' in rendered
        assert '"test_class":"functional"' in rendered
        assert '"adversary":{"gain":"' in rendered
        assert '"kind":"none"' in rendered
        assert "What happens at my cataract clinic visit?" not in rendered
        assert "response_claim" not in rendered
        assert "COMPARISON_ONLY" not in rendered
    assert "Routine education answers must remain grounded" in call1.user


def test_actual_o03_rendered_view_keeps_adversarial_data_separate() -> None:
    view = _load_occi_case("O03")
    model_view = build_reference_task_view(view)

    assert model_view["family"] == "policy_violation_write"
    assert model_view["test_class"] == "adversarial"
    assert model_view["adversary"]["kind"] == "malicious_customer"
    assert "required review" in model_view["adversary"]["gain"]


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


def test_correction_size_accounting_is_evidence_only(tmp_path: Path) -> None:
    invalid = b'{"detector": {}, "detector_source": "not python"}'
    result = AuthoringOrchestrator(
        transport=ScriptedAuthoringTransport([json.dumps(neutral_artifact_plan()), invalid]),
        package_dir=tmp_path / "package",
        task_id="size-followup",
    ).run(
        _load_occi_case("O04"),
        _inventory(),
        _runtime_contract(),
    )

    assert result.status == "failed"
    correction = result.prompts["correction"]
    assert "size_comparison" not in correction.payload
    assert "tokens" not in correction.payload
    assert "cost" not in correction.payload
    assert correction.payload["failed_response"] == invalid.decode()
