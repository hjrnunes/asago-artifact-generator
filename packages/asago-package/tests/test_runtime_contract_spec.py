"""The installed runtime-contract specs: identity, cases, and the reader refusals.

``asago_package`` carries the authority copy of ``runtime-contract`` as package data, so the
reader tests the installed spec, its cases, and its envelope.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from asago_contracts_core import (
    ENVELOPE_FIELD,
    ContractEnvelopeError,
    ContractSpec,
    MissingEnvelopeError,
    SpecDigestMismatchError,
    SpecSet,
    UnknownFieldError,
    UnknownRequirementError,
    UnsupportedVersionError,
    spec_digest,
    write_envelope,
)

from asago_package.contract_kit import contract_dir
from asago_package.runtime_contract_spec import (
    CONTRACT,
    CONTRACTS_DIR,
    VERSION,
    installed_specs,
    read_runtime_contract,
    write_runtime_contract,
)

_ROOT = contract_dir(CONTRACT)
_STATUS = ("published", "required")
_DATA = Path(__file__).resolve().parent / "data"
#: Contracts the declared-target builder wrote for a target with no seed slot and with one.
_WRITTEN_CONTRACTS = (
    "runtime-contract-v1-no-slots.json",
    "runtime-contract-v1-planted-item.json",
)


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _spec(version: str = VERSION) -> ContractSpec:
    return installed_specs().spec(CONTRACT, version)


def _valid_cases(version: str = VERSION) -> list[Path]:
    return sorted((_ROOT / version / "cases" / "valid").glob("*.json"))


def _invalid_cases(version: str = VERSION) -> list[Path]:
    return sorted((_ROOT / version / "cases" / "invalid").glob("*.json"))


def _enveloped(spec: ContractSpec, document: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return a case document with the envelope, overriding envelope fields."""

    envelope = {
        "id": spec.contract,
        "version": spec.version,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.runtime_contract_spec",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
        **overrides,
    }
    return {**document, ENVELOPE_FIELD: envelope}


_VALID_CASES = _valid_cases()
_INVALID_CASES = _invalid_cases()


def test_the_version_directory_holds_the_spec_the_cases_and_the_docs() -> None:
    root = _ROOT / VERSION
    assert CONTRACTS_DIR / CONTRACT == _ROOT
    assert (CONTRACT, VERSION) == ("runtime-contract", "artifact-runtime-contract-v1")
    for relative in ("spec.json", "schema.json", "README.md", "cases/expected-violations.json"):
        assert (root / relative).is_file(), relative
    assert _valid_cases()
    assert _invalid_cases()


def test_the_installed_spec_is_the_runtime_contract_version() -> None:
    spec = _spec()
    root = _ROOT / VERSION
    body = _json(root / "spec.json")

    assert (spec.contract, spec.version, spec.status, spec.envelope) == (
        CONTRACT,
        VERSION,
        *_STATUS,
    )
    assert spec.supports == ()
    assert spec.extensions == ()
    assert body["cases"] == "cases"
    assert body["docs"] == "README.md"
    assert spec.spec_digest == spec_digest(body, (root / "schema.json").read_bytes())


def test_the_reader_refuses_a_document_without_the_envelope() -> None:
    with pytest.raises(MissingEnvelopeError, match="requires the self-identifying envelope"):
        read_runtime_contract(_json(_valid_cases()[0]))


@pytest.mark.parametrize("case", _VALID_CASES, ids=lambda case: case.stem)
def test_the_reader_accepts_every_valid_case_with_the_envelope(case: Path) -> None:
    written = write_envelope(
        _json(case), _spec(), writer="tests.runtime_contract_spec", writer_version="0.1.0"
    )

    assert read_runtime_contract(written).version == VERSION


def test_the_writer_stamps_the_installed_envelope() -> None:
    case = _json(_valid_cases()[0])
    spec = _spec()

    written = write_runtime_contract(
        case, writer="tests.runtime_contract_spec", writer_version="0.1.0"
    )

    assert written[ENVELOPE_FIELD] == {
        "id": CONTRACT,
        "version": VERSION,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.runtime_contract_spec",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
    }
    assert read_runtime_contract(written).version == VERSION


@pytest.mark.parametrize("case", _INVALID_CASES, ids=lambda case: case.stem)
def test_the_reader_refuses_every_invalid_case(case: Path) -> None:
    document = _json(case)
    document = _enveloped(_spec(), document)

    with pytest.raises(ContractEnvelopeError):
        read_runtime_contract(document)


@pytest.mark.parametrize("name", _WRITTEN_CONTRACTS)
def test_the_reader_accepts_a_contract_the_builder_wrote(name: str) -> None:
    """A contract the declared-target builder wrote reads at the current version."""

    assert read_runtime_contract(_json(_DATA / name)).version == VERSION


def test_the_lock_names_every_file_of_the_version_directory() -> None:
    """The lock names all installed files."""

    lock = _json(_ROOT / "CONTRACT.lock")
    files = {
        path.relative_to(_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(_ROOT.rglob("*"))
        if path.is_file() and path.name != "CONTRACT.lock"
    }

    assert lock["authority"] == "asago-artifact-generator"
    assert lock["contract"] == CONTRACT
    assert lock["schema_version"] == VERSION
    assert lock["files"] == files


def test_an_unsupported_version_names_the_supported_versions() -> None:
    case = _json(_valid_cases()[0])

    with pytest.raises(UnsupportedVersionError) as raised:
        installed_specs().read(_enveloped(_spec(), case, version="artifact-runtime-contract-v9"))

    assert "artifact-runtime-contract-v9" in str(raised.value)
    assert VERSION in str(raised.value)


def test_an_unknown_requires_entry_is_rejected() -> None:
    case = _json(_valid_cases()[0])

    with pytest.raises(UnknownRequirementError) as raised:
        installed_specs().read(_enveloped(_spec(), case, requires=["runtime-contract-v1"]))

    assert "runtime-contract-v1" in str(raised.value)


def test_an_unknown_field_outside_an_extension_is_rejected() -> None:
    case = _json(_valid_cases()[0])
    document = _enveloped(_spec(), case)
    document["probe_key"] = "a field the contract never declared"

    with pytest.raises(UnknownFieldError) as raised:
        read_runtime_contract(document)

    assert "probe_key" in str(raised.value)
    assert "declared extensions: none" in str(raised.value)


def test_a_draft_with_a_mismatched_spec_digest_is_rejected() -> None:
    case = _json(_valid_cases()[0])
    draft = replace(_spec(), status="draft")
    other = "sha256:" + "0" * 64

    with pytest.raises(SpecDigestMismatchError) as raised:
        SpecSet((draft,)).read(_enveloped(draft, case, spec_digest=other))

    assert "draft" in str(raised.value)
    assert other in str(raised.value)
