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


def test_snapshot_reads_source_and_never_edits_it(tmp_path: Path) -> None:
    source_path = tmp_path / "source.yaml"
    source_path.write_bytes(b"source: exact\n")
    before = source_path.read_bytes()

    snapshot = snapshot_input(source_path, tmp_path / "snapshots")

    assert source_path.read_bytes() == before
    assert snapshot.sha256 == hashlib.sha256(before).hexdigest()
    assert snapshot.snapshot_path is not None
    assert Path(snapshot.snapshot_path).read_bytes() == before


def test_non_handoff_source_is_rejected(tmp_path: Path) -> None:
    source_path = tmp_path / "native.yaml"
    source_path.write_text("scenario_id: native\n", encoding="utf-8")

    with pytest.raises(InputSourceError, match="scenario-handoff-v1"):
        load_input(source_path)


def test_tampered_handoff_fails_before_a_view_is_created(tmp_path: Path) -> None:
    payload = CONTRACT_HANDOFF.read_bytes().replace(b"adversarial", b"functional", 1)
    source_path = tmp_path / "tampered.json"
    source_path.write_bytes(payload)

    with pytest.raises(InputSourceError, match="content_digest"):
        load_input(source_path, kind=InputKind.SCENARIO_HANDOFF_V1)
