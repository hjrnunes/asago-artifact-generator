"""The installed runtime-contract spec: identity, cases, and the reader refusals.

``asago_package`` carries the authority copy of ``runtime-contract`` as package data, so the
reader here is also the installed copy: the whole version directory ships byte-identically to
the repository's kit. The tests recompute the spec digest, read every case, read every
recorded contract, and assert the four refusals a reader owes.
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
    ContractEnvelopeError,
    ContractSpec,
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
)

REPO = Path(__file__).resolve().parents[3]
_ROOT = contract_dir(CONTRACT)
_SPEC_ROOT = _ROOT / VERSION
_CASES = _SPEC_ROOT / "cases"
_SCHEMA = _SPEC_ROOT / "schema.json"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _spec() -> ContractSpec:
    return installed_specs().spec(CONTRACT, VERSION)


def _valid_cases() -> list[Path]:
    return sorted((_CASES / "valid").glob("*.json"))


def _invalid_cases() -> list[Path]:
    return sorted((_CASES / "invalid").glob("*.json"))


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


def test_the_version_directory_holds_the_spec_the_cases_and_the_docs() -> None:
    assert CONTRACTS_DIR / CONTRACT == _ROOT
    assert (CONTRACT, VERSION) == ("runtime-contract", "artifact-runtime-contract-v2")
    for relative in ("spec.json", "schema.json", "README.md", "cases/expected-violations.json"):
        assert (_SPEC_ROOT / relative).is_file(), relative
    assert _valid_cases()
    assert _invalid_cases()


def test_the_installed_spec_is_the_runtime_contract_version() -> None:
    spec = _spec()
    body = _json(_SPEC_ROOT / "spec.json")

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


@pytest.mark.parametrize("case", _valid_cases(), ids=lambda case: case.stem)
def test_the_reader_accepts_every_valid_case_as_a_legacy_document(case: Path) -> None:
    assert read_runtime_contract(_json(case)).version == VERSION


@pytest.mark.parametrize("case", _valid_cases(), ids=lambda case: case.stem)
def test_the_reader_accepts_every_valid_case_with_the_envelope(case: Path) -> None:
    written = write_envelope(
        _json(case), _spec(), writer="tests.runtime_contract_spec", writer_version="0.1.0"
    )

    assert read_runtime_contract(written).version == VERSION


@pytest.mark.parametrize("case", _invalid_cases(), ids=lambda case: case.stem)
def test_the_reader_refuses_every_invalid_case(case: Path) -> None:
    with pytest.raises(ContractEnvelopeError):
        read_runtime_contract(_json(case))


def test_every_recorded_contract_still_reads() -> None:
    """Every contract recorded outside the cases reads as a v2 document.

    The replay corpus holds the runtime contracts the recorded runs captured. A checkout
    without the corpus still reads the cases above.
    """

    corpus = Path(os.environ.get("ASAGO_REPLAY_CORPUS", REPO.parent / "replay-corpus"))
    recorded = sorted(corpus.rglob("runtime-contract.json")) if corpus.is_dir() else []
    if not recorded:
        pytest.skip(f"no recorded runtime contracts under {corpus}")

    for path in recorded:
        assert read_runtime_contract(_json(path)).version == VERSION, path


def test_an_unsupported_version_names_the_supported_versions() -> None:
    case = _json(_valid_cases()[0])

    with pytest.raises(UnsupportedVersionError) as raised:
        installed_specs().read(_enveloped(_spec(), case, version="artifact-runtime-contract-v9"))

    assert "artifact-runtime-contract-v9" in str(raised.value)
    assert VERSION in str(raised.value)


def test_an_unknown_requires_entry_is_rejected() -> None:
    case = _json(_valid_cases()[0])

    with pytest.raises(UnknownRequirementError) as raised:
        installed_specs().read(_enveloped(_spec(), case, requires=["runtime-contract-v3"]))

    assert "runtime-contract-v3" in str(raised.value)


def test_an_unknown_field_outside_an_extension_is_rejected() -> None:
    case = _json(_valid_cases()[0])
    document = _enveloped(_spec(), case)
    document["probe_key"] = "a field v2 never declared"

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
