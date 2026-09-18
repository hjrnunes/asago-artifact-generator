"""Deterministic input adapters for the artifact authoring path."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from asago_artifact_generator.input_adapter import (
    InputKind,
    InputSourceError,
    load_input,
    snapshot_input,
)

CONTRACT_HANDOFF = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "scenario-handoff"
    / "handoff-v1"
    / "valid"
    / "adversarial-refund.json"
)


def test_handoff_view_preserves_source_hash_and_authoritative_content() -> None:
    source = CONTRACT_HANDOFF.read_bytes()

    view = load_input(CONTRACT_HANDOFF, kind=InputKind.SCENARIO_HANDOFF_V1)

    assert view.kind is InputKind.SCENARIO_HANDOFF_V1
    assert view.source_sha256 == hashlib.sha256(source).hexdigest()
    assert view.narrative == yaml.safe_load(source)["narrative"]
    assert view.gherkin["scenario"] == "Refund command exceeds the remaining balance of the order"
    assert view.reference_label is None


def test_native_semantic_yaml_preserves_dictionary_gherkin_and_feature_bytes(
    tmp_path: Path,
) -> None:
    scenario = {
        "scenario_id": "native-1",
        "narrative": {"summary": "A native scenario"},
        "behavior_spec": {
            "gherkin_text": "Feature: Exact\n  Scenario: Keep bytes\n    Given a fact\n"
        },
    }
    source_path = tmp_path / "native.yaml"
    feature_path = source_path.with_suffix(".feature")
    source_path.write_text(yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8")
    feature_path.write_bytes(b"Feature: Exact\n  Scenario: Keep bytes\n    Given a fact\n")

    view = load_input(source_path, kind=InputKind.NATIVE_SEMANTIC_YAML)

    assert view.scenario_id == "native-1"
    assert view.narrative == scenario["narrative"]
    assert view.gherkin_text == feature_path.read_bytes().decode("utf-8")
    assert view.gherkin_bytes == feature_path.read_bytes()


def test_reference_task_is_labeled_without_branching_on_its_id(tmp_path: Path) -> None:
    source_path = tmp_path / "gold-cases.yaml"
    source_path.write_text(
        yaml.safe_dump(
            {
                "target_environment": "example",
                "gold_cases": [
                    {
                        "id": "CASE-1",
                        "constraint_meaning": "Keep records private.",
                        "stimulus": {"turns": [{"role": "user", "text": "Hello"}]},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    view = load_input(
        source_path,
        kind=InputKind.REFERENCE_TASK,
        reference_label="development-fixture",
        reference_id="CASE-1",
    )

    assert view.kind is InputKind.REFERENCE_TASK
    assert view.reference_label == "development-fixture"
    assert view.reference_id == "CASE-1"
    assert view.payload["id"] == "CASE-1"


def test_snapshot_reads_source_and_never_edits_it(tmp_path: Path) -> None:
    source_path = tmp_path / "gold.yaml"
    source_path.write_bytes(b"gold: exact\n")
    before = source_path.read_bytes()

    snapshot = snapshot_input(source_path, tmp_path / "snapshots")

    assert source_path.read_bytes() == before
    assert snapshot.sha256 == hashlib.sha256(before).hexdigest()
    assert snapshot.snapshot_path is not None
    assert Path(snapshot.snapshot_path).read_bytes() == before


def test_tampered_handoff_fails_before_a_view_is_created(tmp_path: Path) -> None:
    payload = CONTRACT_HANDOFF.read_bytes().replace(b"adversarial", b"functional", 1)
    source_path = tmp_path / "tampered.json"
    source_path.write_bytes(payload)

    with pytest.raises(InputSourceError, match="content_digest"):
        load_input(source_path, kind=InputKind.SCENARIO_HANDOFF_V1)
