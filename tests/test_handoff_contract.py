"""The scenario-handoff kit: its installed specs, its documents and its refusals.

The producer owns ``data/contracts/scenario-handoff/`` and the shared core ships a byte for
byte copy as package data, which the adapter reads every input through before its own
checks. The installed handoff requires its self-identifying envelope.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from asago_contracts_core import CONTRACT_DIR
from asago_handoff import read_handoff
from asago_handoff.envelope import (
    ENVELOPE_FIELD,
    ContractSpec,
    MissingEnvelopeError,
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
from tests.support import HANDOFFS

CONTRACT = "scenario-handoff"
V6 = "scenario-handoff-v1"
VERSION = V6
VERSIONS = (V6,)
_ENVELOPE = {V6: "required"}
_KIND = {V6: InputKind.SCENARIO_HANDOFF_V1}
_CONTRACT_DIR = CONTRACT_DIR
#: The producer-written handoffs checked in beside the tests, as JSON or YAML.
_PRODUCER_HANDOFFS = sorted([*HANDOFFS.glob("*.json"), *HANDOFFS.glob("*.yaml")])


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _version_dir(version: str) -> Path:
    return _CONTRACT_DIR / version


def _specs() -> SpecSet:
    return load_specs(_CONTRACT_DIR.parent)


def _spec(version: str = V6) -> ContractSpec:
    return _specs().spec(CONTRACT, version)


def _valid_case(version: str) -> dict[str, Any]:
    return _json(sorted((_version_dir(version) / "cases" / "valid").glob("*.json"))[0])


def _cases(version: str, kind: str) -> list[Path]:
    return sorted((_version_dir(version) / "cases" / kind).glob("*.json"))


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


@pytest.mark.parametrize("version", VERSIONS)
def test_the_mirrored_spec_states_its_version(version: str) -> None:
    spec = _spec(version)
    body = _json(_version_dir(version) / "spec.json")

    assert (spec.contract, spec.version, spec.status, spec.envelope) == (
        CONTRACT,
        version,
        "published",
        _ENVELOPE[version],
    )
    assert spec.supports == ()
    assert spec.extensions == ()
    assert body["cases"] == "cases"
    assert body["docs"] == "README.md"
    schema = (_version_dir(version) / "schema.json").read_bytes()
    assert spec.spec_digest == spec_digest(body, schema)


def test_the_mirror_installs_the_current_version() -> None:
    installed = {spec.version for spec in _specs().specs if spec.contract == CONTRACT}

    assert VERSION in installed


def test_the_reader_accepts_the_current_version() -> None:
    written = write_envelope(
        _valid_case(V6), _spec(), writer="tests.handoff_contract", writer_version="0.1.0"
    )
    assert read_handoff(written).version == V6


_ENVELOPED_CASES = [
    pytest.param(version, case, id=f"{version}-{case.stem}")
    for version in VERSIONS
    for case in _cases(version, "valid")
]


@pytest.mark.parametrize(("version", "case"), _ENVELOPED_CASES)
def test_the_reader_accepts_every_valid_case_with_the_envelope(version: str, case: Path) -> None:
    written = write_envelope(
        _json(case),
        _spec(version),
        writer="tests.handoff_contract",
        writer_version="0.1.0",
    )

    assert read_handoff(written).version == version


def test_a_document_without_the_envelope_is_rejected() -> None:
    with pytest.raises(MissingEnvelopeError) as raised:
        _specs().read(_valid_case(V6))

    assert ENVELOPE_FIELD in str(raised.value)
    assert V6 in str(raised.value)


@pytest.mark.parametrize("path", _PRODUCER_HANDOFFS, ids=lambda path: path.name)
def test_every_checked_in_producer_handoff_reads(path: Path) -> None:
    """A handoff the producer wrote, kept as a fixture, reads as a v6 document."""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert document.get("schema_version") == VERSION
    assert read_handoff(document).version == VERSION


@pytest.mark.parametrize("version", VERSIONS)
def test_the_adapter_accepts_an_enveloped_handoff(tmp_path: Path, version: str) -> None:
    case = _valid_case(version)
    case["schema_version"] = version
    written = write_envelope(
        case, _spec(version), writer="tests.handoff_contract", writer_version="0.1.0"
    )
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(written), encoding="utf-8")

    view = load_input(path)

    assert view.kind is _KIND[version]
    assert view.payload[ENVELOPE_FIELD]["version"] == version


def test_an_unsupported_version_names_the_supported_versions() -> None:
    case = _valid_case(V6)

    with pytest.raises(UnsupportedVersionError) as raised:
        _specs().read(_enveloped(_spec(V6), case, version="scenario-handoff-v9"))

    assert "scenario-handoff-v9" in str(raised.value)
    for version in VERSIONS:
        assert version in str(raised.value)


@pytest.mark.parametrize("version", VERSIONS)
def test_an_unknown_contract_names_the_installed_one(version: str) -> None:
    case = _valid_case(version)

    with pytest.raises(UnknownContractError) as raised:
        _specs().read(_enveloped(_spec(version), case, id="not-a-contract"))

    assert CONTRACT in str(raised.value)


@pytest.mark.parametrize("version", VERSIONS)
def test_an_unknown_requires_entry_is_rejected(version: str) -> None:
    case = _valid_case(version)

    with pytest.raises(UnknownRequirementError) as raised:
        _specs().read(_enveloped(_spec(version), case, requires=["scenario-handoff-v9"]))

    assert "scenario-handoff-v9" in str(raised.value)


@pytest.mark.parametrize("version", VERSIONS)
def test_an_unknown_field_outside_an_extension_is_rejected(version: str) -> None:
    case = _valid_case(version)
    document = _enveloped(_spec(version), case)
    document["probe_key"] = "a field the version never declared"

    with pytest.raises(UnknownFieldError) as raised:
        _specs().read(document)

    assert "probe_key" in str(raised.value)
    assert "declared extensions: none" in str(raised.value)


@pytest.mark.parametrize("version", VERSIONS)
def test_a_draft_with_a_mismatched_spec_digest_is_rejected(version: str) -> None:
    case = _valid_case(version)
    draft = replace(_spec(version), status="draft")
    other = "sha256:" + "0" * 64

    with pytest.raises(SpecDigestMismatchError) as raised:
        SpecSet((draft,)).read(_enveloped(draft, case, spec_digest=other))

    assert "draft" in str(raised.value)
    assert other in str(raised.value)
