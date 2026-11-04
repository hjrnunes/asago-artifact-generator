"""The scenario-handoff kit: its mirrored spec, its documents and its refusals.

The producer owns ``data/contracts/scenario-handoff/``; this repository mirrors
the whole contract under ``contracts/scenario-handoff`` and the adapter reads
every input through the mirrored spec before its own checks. The mirror is byte
for byte the authority's, so the spec digest and the reader rules are the same
on both sides.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from asago_handoff import read_handoff
from asago_handoff.envelope import (
    ENVELOPE_FIELD,
    ContractSpec,
    SpecDigestMismatchError,
    SpecSet,
    UnknownContractError,
    UnknownFieldError,
    UnknownRequirementError,
    UnsupportedVersionError,
    load_specs,
    spec_digest,
    write_envelope,
)

from asago_artifact_generator.input_adapter import InputKind, load_input

CONTRACT = "scenario-handoff"
VERSION = "scenario-handoff-v5"
_CONTRACT_DIR = Path(__file__).resolve().parents[1] / "contracts" / CONTRACT
_VERSION_DIR = _CONTRACT_DIR / VERSION
_CASES = _VERSION_DIR / "cases"
_SCHEMA = _VERSION_DIR / "schema.json"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _specs() -> SpecSet:
    return load_specs(_CONTRACT_DIR.parent)


def _spec() -> ContractSpec:
    return _specs().spec(CONTRACT, VERSION)


def _cases(kind: str) -> list[Path]:
    return sorted((_CASES / kind).glob("*.json"))


def _enveloped(spec: ContractSpec, document: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return a case document with the envelope, overriding envelope fields."""

    envelope = {
        "id": spec.contract,
        "version": spec.version,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.handoff_contract",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
        **overrides,
    }
    return {**document, ENVELOPE_FIELD: envelope}


def _recorded_documents() -> list[Path]:
    """Return every recorded handoff the reader must keep reading."""

    documents = _cases("valid")
    corpus = os.environ.get("ASAGO_REPLAY_CORPUS")
    if corpus:
        documents = documents + sorted(Path(corpus).rglob("handoff.json"))
    return documents


def test_the_mirrored_spec_is_the_scenario_handoff_v5() -> None:
    spec = _spec()
    body = _json(_VERSION_DIR / "spec.json")

    assert (spec.contract, spec.version, spec.status, spec.envelope) == (
        CONTRACT,
        VERSION,
        "published",
        "optional",
    )
    assert spec.supports == ()
    assert spec.extensions == ()
    assert body["cases"] == "cases"
    assert body["docs"] == "README.md"
    assert spec.spec_digest == spec_digest(body, _SCHEMA.read_bytes())


@pytest.mark.parametrize("case", _cases("valid"), ids=lambda case: case.stem)
def test_the_reader_accepts_every_valid_case_as_a_legacy_document(case: Path) -> None:
    """A v5 document without the envelope still reads; the recorded handoffs never had one."""

    assert read_handoff(_json(case)).version == VERSION


@pytest.mark.parametrize("case", _cases("valid"), ids=lambda case: case.stem)
def test_the_reader_accepts_every_valid_case_with_the_envelope(case: Path) -> None:
    written = write_envelope(
        _json(case),
        _spec(),
        writer="tests.handoff_contract",
        writer_version="0.1.0",
    )

    assert read_handoff(written).version == VERSION


@pytest.mark.parametrize("case", _recorded_documents(), ids=lambda case: case.name)
def test_every_recorded_handoff_still_reads(case: Path) -> None:
    document = _json(case)

    assert document.get("schema_version") == VERSION
    assert read_handoff(document).version == VERSION


def test_the_adapter_accepts_an_enveloped_handoff(tmp_path: Path) -> None:
    case = _json(_cases("valid")[0])
    written = write_envelope(
        case, _spec(), writer="tests.handoff_contract", writer_version="0.1.0"
    )
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(written), encoding="utf-8")

    view = load_input(path)

    assert view.kind is InputKind.SCENARIO_HANDOFF_V5
    assert view.payload[ENVELOPE_FIELD]["version"] == VERSION


def test_an_unsupported_version_names_the_supported_versions() -> None:
    case = _json(_cases("valid")[0])

    with pytest.raises(UnsupportedVersionError) as raised:
        _specs().read(_enveloped(_spec(), case, version="scenario-handoff-v9"))

    assert "scenario-handoff-v9" in str(raised.value)
    assert VERSION in str(raised.value)


def test_an_unknown_contract_names_the_installed_one() -> None:
    case = _json(_cases("valid")[0])

    with pytest.raises(UnknownContractError) as raised:
        _specs().read(_enveloped(_spec(), case, id="not-a-contract"))

    assert CONTRACT in str(raised.value)


def test_an_unknown_requires_entry_is_rejected() -> None:
    case = _json(_cases("valid")[0])

    with pytest.raises(UnknownRequirementError) as raised:
        _specs().read(_enveloped(_spec(), case, requires=["scenario-handoff-v2"]))

    assert "scenario-handoff-v2" in str(raised.value)


def test_an_unknown_field_outside_an_extension_is_rejected() -> None:
    case = _json(_cases("valid")[0])
    document = _enveloped(_spec(), case)
    document["probe_key"] = "a field v5 never declared"

    with pytest.raises(UnknownFieldError) as raised:
        _specs().read(document)

    assert "probe_key" in str(raised.value)
    assert "declared extensions: none" in str(raised.value)


def test_a_draft_with_a_mismatched_spec_digest_is_rejected() -> None:
    case = _json(_cases("valid")[0])
    draft = replace(_spec(), status="draft")
    other = "sha256:" + "0" * 64

    with pytest.raises(SpecDigestMismatchError) as raised:
        SpecSet((draft,)).read(_enveloped(draft, case, spec_digest=other))

    assert "draft" in str(raised.value)
    assert other in str(raised.value)
