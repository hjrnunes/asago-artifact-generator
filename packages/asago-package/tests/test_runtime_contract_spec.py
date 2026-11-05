"""The installed runtime-contract specs: identity, cases, and the reader refusals.

``asago_package`` carries the authority copy of ``runtime-contract`` as package data, so the
reader here is also the installed copy: every version directory ships byte-identically to the
repository's kit. The tests recompute each spec digest, read every case, read every recorded
contract, and assert the four refusals a reader owes for both versions.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from asago_contracts_core import (
    ENVELOPE_FIELD,
    ContractEnvelopeError,
    ContractSpec,
    InvalidDocumentError,
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
    LEGACY_VERSION,
    VERSION,
    VERSIONS,
    installed_specs,
    read_runtime_contract,
    write_runtime_contract,
)

REPO = Path(__file__).resolve().parents[3]
_ROOT = contract_dir(CONTRACT)
_STATUS = {
    LEGACY_VERSION: ("published", "optional"),
    VERSION: ("published", "required"),
}


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


_VALID_CASES = [
    pytest.param(version, case, id=f"{version.rsplit('-', 1)[-1]}-{case.stem}")
    for version in VERSIONS
    for case in _valid_cases(version)
]
_INVALID_CASES = [
    pytest.param(version, case, id=f"{version.rsplit('-', 1)[-1]}-{case.stem}")
    for version in VERSIONS
    for case in _invalid_cases(version)
]


@pytest.mark.parametrize("version", VERSIONS, ids=lambda value: value.rsplit("-", 1)[-1])
def test_the_version_directory_holds_the_spec_the_cases_and_the_docs(version: str) -> None:
    root = _ROOT / version
    assert CONTRACTS_DIR / CONTRACT == _ROOT
    assert VERSIONS == (LEGACY_VERSION, VERSION)
    assert (CONTRACT, VERSION) == ("runtime-contract", "artifact-runtime-contract-v3")
    for relative in ("spec.json", "schema.json", "README.md", "cases/expected-violations.json"):
        assert (root / relative).is_file(), relative
    assert _valid_cases(version)
    assert _invalid_cases(version)


@pytest.mark.parametrize("version", VERSIONS, ids=lambda value: value.rsplit("-", 1)[-1])
def test_the_installed_spec_is_the_runtime_contract_version(version: str) -> None:
    spec = _spec(version)
    root = _ROOT / version
    body = _json(root / "spec.json")

    assert (spec.contract, spec.version, spec.status, spec.envelope) == (
        CONTRACT,
        version,
        *_STATUS[version],
    )
    assert spec.supports == ()
    assert spec.extensions == ()
    assert body["cases"] == "cases"
    assert body["docs"] == "README.md"
    assert spec.spec_digest == spec_digest(body, (root / "schema.json").read_bytes())


@pytest.mark.parametrize("case", _valid_cases(LEGACY_VERSION), ids=lambda case: case.stem)
def test_the_reader_accepts_every_v2_case_as_a_legacy_document(case: Path) -> None:
    assert read_runtime_contract(_json(case)).version == LEGACY_VERSION


def test_the_reader_refuses_a_v3_document_without_the_envelope() -> None:
    with pytest.raises(MissingEnvelopeError, match="requires the self-identifying envelope"):
        read_runtime_contract(_json(_valid_cases()[0]))


@pytest.mark.parametrize(("version", "case"), _VALID_CASES)
def test_the_reader_accepts_every_valid_case_with_the_envelope(case: Path, version: str) -> None:
    written = write_envelope(
        _json(case), _spec(version), writer="tests.runtime_contract_spec", writer_version="0.1.0"
    )

    assert read_runtime_contract(written).version == version


def test_the_writer_stamps_the_installed_v3_envelope() -> None:
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


def test_the_reader_accepts_both_versions() -> None:
    v2 = _json(_valid_cases(LEGACY_VERSION)[0])
    v3 = write_runtime_contract(
        _json(_valid_cases()[0]),
        writer="tests.runtime_contract_spec",
        writer_version="0.1.0",
    )

    assert read_runtime_contract(v2).version == LEGACY_VERSION
    assert read_runtime_contract(v3).version == VERSION


def test_the_writer_refuses_a_document_that_still_names_v2() -> None:
    case = _json(_valid_cases(LEGACY_VERSION)[0])

    with pytest.raises(InvalidDocumentError, match="schema_version"):
        write_runtime_contract(case, writer="tests.runtime_contract_spec", writer_version="0.1.0")


@pytest.mark.parametrize(("version", "case"), _INVALID_CASES)
def test_the_reader_refuses_every_invalid_case(case: Path, version: str) -> None:
    document = _json(case)
    if _spec(version).envelope == "required":
        document = _enveloped(_spec(version), document)

    with pytest.raises(ContractEnvelopeError):
        read_runtime_contract(document)


def test_every_recorded_contract_still_reads() -> None:
    """Every contract recorded outside the cases reads as a v2 or v3 document.

    The replay corpus holds the runtime contracts the recorded runs captured. A checkout
    without the corpus still reads the cases above.
    """

    corpus = Path(os.environ.get("ASAGO_REPLAY_CORPUS", REPO.parent / "replay-corpus"))
    recorded = sorted(corpus.rglob("runtime-contract.json")) if corpus.is_dir() else []
    if not recorded:
        pytest.skip(f"no recorded runtime contracts under {corpus}")

    for path in recorded:
        assert read_runtime_contract(_json(path)).version in VERSIONS, path


def test_the_lock_names_every_file_of_both_version_directories() -> None:
    """The installed copy is every version directory, so the lock names all of it."""

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


@pytest.mark.parametrize("version", VERSIONS, ids=lambda value: value.rsplit("-", 1)[-1])
def test_an_unsupported_version_names_the_supported_versions(version: str) -> None:
    case = _json(_valid_cases(version)[0])

    with pytest.raises(UnsupportedVersionError) as raised:
        installed_specs().read(
            _enveloped(_spec(version), case, version="artifact-runtime-contract-v9")
        )

    assert "artifact-runtime-contract-v9" in str(raised.value)
    assert VERSION in str(raised.value)


@pytest.mark.parametrize("version", VERSIONS, ids=lambda value: value.rsplit("-", 1)[-1])
def test_an_unknown_requires_entry_is_rejected(version: str) -> None:
    case = _json(_valid_cases(version)[0])

    with pytest.raises(UnknownRequirementError) as raised:
        installed_specs().read(_enveloped(_spec(version), case, requires=["runtime-contract-v3"]))

    assert "runtime-contract-v3" in str(raised.value)


@pytest.mark.parametrize("version", VERSIONS, ids=lambda value: value.rsplit("-", 1)[-1])
def test_an_unknown_field_outside_an_extension_is_rejected(version: str) -> None:
    case = _json(_valid_cases(version)[0])
    document = _enveloped(_spec(version), case)
    document["probe_key"] = "a field the contract never declared"

    with pytest.raises(UnknownFieldError) as raised:
        read_runtime_contract(document)

    assert "probe_key" in str(raised.value)
    assert "declared extensions: none" in str(raised.value)


@pytest.mark.parametrize("version", VERSIONS, ids=lambda value: value.rsplit("-", 1)[-1])
def test_a_draft_with_a_mismatched_spec_digest_is_rejected(version: str) -> None:
    case = _json(_valid_cases(version)[0])
    draft = replace(_spec(version), status="draft")
    other = "sha256:" + "0" * 64

    with pytest.raises(SpecDigestMismatchError) as raised:
        SpecSet((draft,)).read(_enveloped(draft, case, spec_digest=other))

    assert "draft" in str(raised.value)
    assert other in str(raised.value)
