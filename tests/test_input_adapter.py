"""Deterministic input adapters for the artifact authoring path."""

from __future__ import annotations

import hashlib
import json
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


def test_observation_metadata_reaches_authoring_view(tmp_path: Path) -> None:
    payload = json.loads(CONTRACT_HANDOFF.read_text(encoding="utf-8"))
    payload["observation"] = {
        "contract_schema": "observation-contract-v1",
        "contract_id": "qualification-live-v1",
        "contract_digest": "a" * 64,
        "criteria": [
            {
                "criterion_id": "reply-visible",
                "outcome": "The assistant reply contains the unsafe response.",
                "observable": True,
                "claim_level": "reply",
                "evidence": "assistant_message",
                "reason": "The live contract captures assistant messages.",
            }
        ],
        "assessment": {
            "disposition": "executable",
            "reason": "observable_outcome_supported",
            "supported_criteria": ["reply-visible"],
            "unsupported_criteria": [],
        },
    }
    payload_without_digest = {
        key: value for key, value in payload.items() if key != "content_digest"
    }
    from asago_artifact_generator.input_adapter import _framed_digest

    payload["content_digest"] = _framed_digest("scenario-handoff-v1", payload_without_digest)
    source_path = tmp_path / "handoff.yaml"
    source_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    view = load_input(source_path, kind=InputKind.SCENARIO_HANDOFF_V1)

    assert view.payload["observation"]["assessment"]["disposition"] == "executable"
    assert view.payload["observation"]["criteria"][0]["claim_level"] == "reply"


def test_analytical_observation_criterion_may_omit_optional_fields(
    tmp_path: Path,
) -> None:
    payload = json.loads(CONTRACT_HANDOFF.read_text(encoding="utf-8"))
    payload["observation"] = {
        "contract_schema": "observation-contract-v1",
        "contract_id": "qualification-live-v1",
        "contract_digest": "a" * 64,
        "criteria": [
            {
                "criterion_id": "state",
                "outcome": "The backend record changes state.",
                "observable": False,
                "reason": "The live contract captures no state snapshot.",
            }
        ],
        "assessment": {
            "disposition": "analytical_only",
            "reason": "no_observable_outcome",
            "supported_criteria": [],
            "unsupported_criteria": ["state"],
        },
    }
    payload_without_digest = {
        key: value for key, value in payload.items() if key != "content_digest"
    }
    from asago_artifact_generator.input_adapter import _framed_digest

    payload["content_digest"] = _framed_digest("scenario-handoff-v1", payload_without_digest)
    source_path = tmp_path / "handoff.yaml"
    source_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    view = load_input(source_path, kind=InputKind.SCENARIO_HANDOFF_V1)

    assert view.payload["observation"]["assessment"]["disposition"] == "analytical_only"
