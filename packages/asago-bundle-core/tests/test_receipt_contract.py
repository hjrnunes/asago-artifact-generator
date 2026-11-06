"""The installed ``execution-receipt`` contract: copy, identity, reader rules and cases.

Both bundle adapters write ``execution-receipt.json`` and read it through
``asago_bundle_core.receipt_contract``. This library ships both versions the reader accepts,
so the tests compare each copy with the consumer's authority, recompute the spec digests,
read every recorded document, and assert the refusals a reader owes for each version.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from asago_contracts_core import (
    ENVELOPE_FIELD,
    ContractSpec,
    InvalidDocumentError,
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

from asago_bundle_core.receipt_contract import (
    CONTRACT,
    LEGACY_VERSION,
    VERSION,
    installed_specs,
    read_receipt,
    write_receipt,
)

REPO = Path(__file__).resolve().parents[3]
_AUTHORITY = REPO / "contracts" / CONTRACT
_INSTALLED = Path(__file__).resolve().parents[1] / "src/asago_bundle_core/contracts" / CONTRACT
_VERSIONS = (LEGACY_VERSION, VERSION)
#: The workspace checkout holds the replay corpus beside this repository.
_REPLAY_CORPUS = REPO.parent / "replay-corpus"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _specs() -> SpecSet:
    return load_specs(_INSTALLED.parent)


def _spec(version: str) -> ContractSpec:
    return installed_specs().spec(CONTRACT, version)


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _cases(version: str) -> list[Path]:
    return sorted((_AUTHORITY / version / "cases" / "valid").glob("*.json"))


def _case(version: str, name: str = "") -> dict[str, Any]:
    return _json(_cases(version)[0] if not name else _AUTHORITY / version / "cases/valid" / name)


def _enveloped(spec: ContractSpec, document: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return a case document with the envelope, overriding envelope fields."""

    envelope = {
        "id": spec.contract,
        "version": spec.version,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.receipt_contract",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
        **overrides,
    }
    return {**document, ENVELOPE_FIELD: envelope}


@pytest.mark.parametrize("version", _VERSIONS)
def test_the_installed_copy_is_the_authority_byte_for_byte(version: str) -> None:
    assert (_INSTALLED / version).is_dir()
    assert _files(_INSTALLED / version) == _files(_AUTHORITY / version)


@pytest.mark.parametrize("version", _VERSIONS)
def test_the_installed_spec_is_the_execution_receipt_version(version: str) -> None:
    spec = _spec(version)
    root = _INSTALLED / version
    body = _json(root / "spec.json")
    expected = "optional" if version == LEGACY_VERSION else "required"

    assert (spec.contract, spec.version, spec.status, spec.envelope) == (
        CONTRACT,
        version,
        "published",
        expected,
    )
    assert spec.supports == ()
    assert spec.extensions == ()
    assert body["cases"] == "cases"
    assert body["docs"] == "README.md"
    assert spec.spec_digest == spec_digest(body, (root / "schema.json").read_bytes())


@pytest.mark.parametrize("case", _cases(LEGACY_VERSION), ids=lambda case: case.stem)
def test_the_reader_accepts_every_v1_valid_case_as_a_legacy_document(case: Path) -> None:
    assert read_receipt(_json(case)).version == LEGACY_VERSION


@pytest.mark.parametrize("case", _cases(LEGACY_VERSION), ids=lambda case: case.stem)
def test_the_reader_accepts_every_v1_valid_case_with_the_envelope(case: Path) -> None:
    written = write_envelope(
        _json(case), _spec(LEGACY_VERSION), writer="tests.receipt_contract", writer_version="0.1.0"
    )

    assert read_receipt(written).version == LEGACY_VERSION


@pytest.mark.parametrize("case", _cases(VERSION), ids=lambda case: case.stem)
def test_the_reader_accepts_every_v2_valid_case_with_the_envelope(case: Path) -> None:
    written = write_envelope(
        _json(case), _spec(VERSION), writer="tests.receipt_contract", writer_version="0.1.0"
    )

    assert read_receipt(written).version == VERSION


@pytest.mark.parametrize("case", _cases(VERSION), ids=lambda case: case.stem)
def test_a_v2_document_without_the_envelope_is_refused(case: Path) -> None:
    with pytest.raises(MissingEnvelopeError, match=CONTRACT):
        read_receipt(_json(case))


def test_the_writer_stamps_the_v2_envelope() -> None:
    spec = _spec(VERSION)

    written = write_receipt(
        _case(VERSION), writer="tests.receipt_contract", writer_version="0.1.0"
    )

    assert written[ENVELOPE_FIELD] == {
        "id": CONTRACT,
        "version": VERSION,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.receipt_contract",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
    }
    assert read_receipt(written).version == VERSION


_RUNTIMES = (
    "midojo-runtime-unmanaged.json",
    "midojo-runtime-openshell.json",
    "midojo-runtime-openshell-without-evidence.json",
)


@pytest.mark.parametrize("name", _RUNTIMES)
def test_the_writer_keeps_the_runtime_and_its_evidence_order(name: str) -> None:
    """The optional runtime reach the reader unchanged, evidence in the caller's order."""

    document = _case(VERSION, name)

    written = write_receipt(document, writer="tests.receipt_contract", writer_version="0.1.0")

    assert read_receipt(written).version == VERSION
    assert written["evaluation"]["runtime"] == document["evaluation"]["runtime"]


def test_an_unknown_runtime_type_is_refused() -> None:
    document = _case(VERSION, "midojo-runtime-unmanaged.json")
    document["evaluation"]["runtime"]["type"] = "sandboxed"

    with pytest.raises(InvalidDocumentError, match="sandboxed"):
        write_receipt(document, writer="tests.receipt_contract", writer_version="0.1.0")


def test_the_reader_accepts_both_versions() -> None:
    v1 = _case(LEGACY_VERSION)
    v2 = write_receipt(_case(VERSION), writer="tests.receipt_contract", writer_version="0.1.0")

    assert read_receipt(v1).version == LEGACY_VERSION
    assert read_receipt(v2).version == VERSION


def _replay_corpus() -> Path:
    """Return the replay corpus the recorded receipts live in.

    VAL-SAFETY-006: a test that reads the corpus fails loudly when the
    configured directory is missing; it must not read nothing and pass.
    """

    corpus = Path(os.environ.get("ASAGO_REPLAY_CORPUS", _REPLAY_CORPUS))
    if not corpus.is_dir():
        pytest.fail(f"the replay corpus directory does not exist: {corpus}")
    return corpus


def test_a_missing_replay_corpus_fails_and_names_the_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "no-such-corpus"
    monkeypatch.setenv("ASAGO_REPLAY_CORPUS", str(missing))

    with pytest.raises(pytest.fail.Exception) as raised:
        _replay_corpus()

    assert str(missing) in str(raised.value)


@pytest.mark.replay_corpus
def test_every_recorded_receipt_still_reads() -> None:
    """Every receipt recorded outside the contract cases reads as a v1 document.

    The replay corpus holds the receipts the M4 and M5 runs recorded; RECEIPT-REPLAY
    checks the same runs. A missing corpus, or one with no receipts, fails: a test
    that reads nothing must not pass.
    """

    corpus = _replay_corpus() / "receipts"
    recorded = sorted(corpus.rglob("execution-receipt.json"))
    if not recorded:
        pytest.fail(f"no recorded receipts under {corpus}")

    for path in recorded:
        assert read_receipt(_json(path)).version == LEGACY_VERSION, path


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unsupported_version_names_the_supported_versions(version: str) -> None:
    spec = _spec(version)

    with pytest.raises(UnsupportedVersionError) as raised:
        _specs().read(_enveloped(spec, _case(version), version="execution-receipt-v9"))

    assert "execution-receipt-v9" in str(raised.value)
    assert VERSION in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_contract_names_the_installed_one(version: str) -> None:
    spec = _spec(version)

    with pytest.raises(UnknownContractError) as raised:
        _specs().read(_enveloped(spec, _case(version), id="not-a-contract"))

    assert CONTRACT in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_requires_entry_is_rejected(version: str) -> None:
    spec = _spec(version)

    with pytest.raises(UnknownRequirementError) as raised:
        _specs().read(_enveloped(spec, _case(version), requires=["execution-receipt-v9"]))

    assert "execution-receipt-v9" in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_field_outside_an_extension_is_rejected(version: str) -> None:
    spec = _spec(version)
    document = _enveloped(spec, _case(version))
    document["undeclared"] = "a field neither version declares"

    with pytest.raises(UnknownFieldError) as raised:
        read_receipt(document)

    assert "undeclared" in str(raised.value)
    assert "declared extensions: none" in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_a_draft_with_a_mismatched_spec_digest_is_rejected(version: str) -> None:
    draft = replace(_spec(version), status="draft")
    other = "sha256:" + "0" * 64

    with pytest.raises(SpecDigestMismatchError) as raised:
        SpecSet((draft,)).read(_enveloped(draft, _case(version), spec_digest=other))

    assert "draft" in str(raised.value)
    assert other in str(raised.value)
