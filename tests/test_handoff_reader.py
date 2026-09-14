"""Handoff reader: version, digest, lineage and kit verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.handoff.reader import (
    HandoffValidationError,
    load_scenario_handoff,
    ownership_violations,
    verify_vendored_kit,
)
from tests.design_fixtures import (
    KIT_DIR,
    REFUND_HANDOFF_PATH,
    load_refund_payload,
    mutated_handoff_json,
    write_yaml_handoff,
)


def test_valid_handoff_records_version_digest_lineage() -> None:
    verified = load_scenario_handoff(REFUND_HANDOFF_PATH)
    record = verified.verification
    assert record.schema_version == "scenario-handoff-v1"
    assert record.digest_verified is True
    assert record.content_digest == verified.handoff.content_digest
    assert record.lineage_constraint_ids == ("SC-1",)
    assert record.lineage_hazard_ids == ("H-1",)
    assert record.lineage_loss_ids == ("L-1",)
    assert record.lineage_resolved is True


def test_valid_yaml_handoff_accepted(tmp_path: Path) -> None:
    path = write_yaml_handoff(tmp_path, load_refund_payload())
    verified = load_scenario_handoff(path)
    assert verified.handoff.scenario_id == "SCN-007"
    assert verified.verification.digest_verified is True


def test_corrupted_narrative_rejected_with_digest_reason(tmp_path: Path) -> None:
    payload = load_refund_payload()
    payload["narrative"] = payload["narrative"] + " Tampered."
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HandoffValidationError) as excinfo:
        load_scenario_handoff(path)
    assert excinfo.value.reason == "content_digest_mismatch"


def test_unknown_schema_version_rejected(tmp_path: Path) -> None:
    def mutate(payload: dict) -> dict:
        payload["schema_version"] = "scenario-handoff-v9"
        return payload

    path = mutated_handoff_json(tmp_path, mutate)
    with pytest.raises(HandoffValidationError) as excinfo:
        load_scenario_handoff(path)
    assert excinfo.value.reason == "unknown_schema_version"


def test_unresolvable_constraint_lineage_rejected(tmp_path: Path) -> None:
    def mutate(payload: dict) -> dict:
        payload["lineage"]["constraint_ids"] = ["SC-999"]
        return payload

    path = mutated_handoff_json(tmp_path, mutate)
    with pytest.raises(HandoffValidationError) as excinfo:
        load_scenario_handoff(path)
    assert excinfo.value.reason == "lineage_unresolved"
    assert "SC-999" in excinfo.value.detail


def test_unreferenced_governing_rule_rejected(tmp_path: Path) -> None:
    def mutate(payload: dict) -> dict:
        payload["governing_rules"].append(
            {"constraint_id": "SC-777", "statement": "An unreferenced rule."}
        )
        return payload

    path = mutated_handoff_json(tmp_path, mutate)
    with pytest.raises(HandoffValidationError) as excinfo:
        load_scenario_handoff(path)
    assert excinfo.value.reason == "lineage_unresolved"


def test_broken_ica_identity_spine_rejected(tmp_path: Path) -> None:
    def mutate(payload: dict) -> dict:
        payload["lineage"]["ica_id"] = "RESP-9:CA-9-9:MADE_UP:1"
        return payload

    path = mutated_handoff_json(tmp_path, mutate)
    with pytest.raises(HandoffValidationError) as excinfo:
        load_scenario_handoff(path)
    assert excinfo.value.reason == "lineage_unresolved"


def test_ownership_violations_match_vendored_kit_expectations() -> None:
    expected = json.loads(
        (KIT_DIR / "handoff-v1" / "expected-violations.json").read_text(encoding="utf-8")
    )
    for relative, codes in expected.items():
        payload = json.loads((KIT_DIR / "handoff-v1" / relative).read_text(encoding="utf-8"))
        assert ownership_violations(payload) == codes


def test_handoff_carrying_prepared_message_rejected(tmp_path: Path) -> None:
    def mutate(payload: dict) -> dict:
        payload["prepared_user_text"] = "A ready-to-send message."
        return payload

    path = mutated_handoff_json(tmp_path, mutate)
    with pytest.raises(HandoffValidationError) as excinfo:
        load_scenario_handoff(path)
    assert excinfo.value.reason.startswith("ownership_violation")


def test_valid_handoff_has_no_ownership_violations() -> None:
    assert ownership_violations(load_refund_payload()) == []


def test_tampered_kit_fails_closed(tmp_path: Path) -> None:
    tampered = tmp_path / "kit"
    tampered.mkdir()
    for item in KIT_DIR.rglob("*"):
        if item.is_file():
            relative = item.relative_to(KIT_DIR)
            destination = tampered / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(item.read_bytes())
    schema_copy = tampered / "handoff-v1" / "schema.json"
    schema_copy.write_text(schema_copy.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(HandoffValidationError) as excinfo:
        verify_vendored_kit(tampered)
    assert excinfo.value.reason == "kit_digest_mismatch"
    with pytest.raises(HandoffValidationError) as excinfo:
        load_scenario_handoff(REFUND_HANDOFF_PATH, kit_dir=tampered)
    assert excinfo.value.reason == "kit_digest_mismatch"


def test_intact_vendored_kit_verifies() -> None:
    result = verify_vendored_kit(KIT_DIR)
    assert result["contract"] == "scenario-handoff"
    assert result["schema_versions"] == ["scenario-handoff-v1"]
    assert result["files_verified"] > 0


def test_functional_handoff_accepted(tmp_path: Path) -> None:
    payload = json.loads(
        (KIT_DIR / "handoff-v1" / "valid" / "functional-no-attacker.json").read_text(
            encoding="utf-8"
        )
    )
    path = tmp_path / "functional.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    verified = load_scenario_handoff(path)
    assert verified.handoff.kind == "functional"
