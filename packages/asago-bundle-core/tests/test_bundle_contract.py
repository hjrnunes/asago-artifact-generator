"""The installed ``tool-bundle`` contract: copy, identity, reader rules and cases.

Both tool adapters write ``bundle.json`` and read it back; this library ships the version
their readers enforce. The tests compare that copy with the consumer's authority, recompute
the spec digest, read every recorded bundle, and assert the four refusals a reader owes.
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

from asago_bundle_core.bundle_contract import (
    CONTRACT,
    VERSION,
    installed_specs,
    read_bundle,
)

REPO = Path(__file__).resolve().parents[3]
_AUTHORITY = REPO / "contracts" / "tool-bundle" / VERSION
_INSTALLED = (
    Path(__file__).resolve().parents[1] / "src/asago_bundle_core/contracts/tool-bundle" / VERSION
)
_CASES = _AUTHORITY / "cases"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _specs() -> SpecSet:
    return load_specs(_INSTALLED.parents[1])


def _spec() -> ContractSpec:
    return installed_specs().spec(CONTRACT, VERSION)


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _valid_cases() -> list[Path]:
    return sorted((_CASES / "valid").glob("*.json"))


def _enveloped(spec: ContractSpec, document: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return a case document with the envelope, overriding envelope fields."""

    envelope = {
        "id": spec.contract,
        "version": spec.version,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.bundle_contract",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
        **overrides,
    }
    return {**document, ENVELOPE_FIELD: envelope}


def test_the_installed_copy_is_the_authority_byte_for_byte() -> None:
    assert _INSTALLED.is_dir()
    assert _files(_INSTALLED) == _files(_AUTHORITY)


def test_the_installed_spec_is_the_tool_bundle_version() -> None:
    spec = _spec()
    body = _json(_INSTALLED / "spec.json")

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
    assert spec.spec_digest == spec_digest(body, (_INSTALLED / "schema.json").read_bytes())


@pytest.mark.parametrize("case", _valid_cases(), ids=lambda case: case.stem)
def test_the_reader_accepts_every_valid_case_as_a_legacy_document(case: Path) -> None:
    assert read_bundle(_json(case)).version == VERSION


@pytest.mark.parametrize("case", _valid_cases(), ids=lambda case: case.stem)
def test_the_reader_accepts_every_valid_case_with_the_envelope(case: Path) -> None:
    written = write_envelope(
        _json(case), _spec(), writer="tests.bundle_contract", writer_version="0.1.0"
    )

    assert read_bundle(written).version == VERSION


def test_every_recorded_bundle_still_reads() -> None:
    """Every bundle recorded outside the contract cases reads as a v1 document.

    The replay corpus holds the bundle manifests the recorded runs captured, including the
    pre-M2 and M5 packages RECEIPT-REPLAY re-parses. A checkout without the corpus still
    reads the cases above.
    """

    corpus = Path(os.environ.get("ASAGO_REPLAY_CORPUS", REPO.parent / "replay-corpus"))
    recorded = sorted(corpus.rglob("bundle.json")) if corpus.is_dir() else []
    if not recorded:
        pytest.skip(f"no recorded bundles under {corpus}")

    for path in recorded:
        assert read_bundle(_json(path)).version == VERSION, path


def test_an_unsupported_version_names_the_supported_versions() -> None:
    case = _json(_valid_cases()[0])

    with pytest.raises(UnsupportedVersionError) as raised:
        _specs().read(_enveloped(_spec(), case, version="tool-bundle-v9"))

    assert "tool-bundle-v9" in str(raised.value)
    assert VERSION in str(raised.value)


def test_an_unknown_contract_names_the_installed_one() -> None:
    case = _json(_valid_cases()[0])

    with pytest.raises(UnknownContractError) as raised:
        _specs().read(_enveloped(_spec(), case, id="not-a-contract"))

    assert CONTRACT in str(raised.value)


def test_an_unknown_requires_entry_is_rejected() -> None:
    case = _json(_valid_cases()[0])

    with pytest.raises(UnknownRequirementError) as raised:
        _specs().read(_enveloped(_spec(), case, requires=["tool-bundle-v2"]))

    assert "tool-bundle-v2" in str(raised.value)


def test_an_unknown_field_outside_an_extension_is_rejected() -> None:
    case = _json(_valid_cases()[0])
    document = _enveloped(_spec(), case)
    document["probe_key"] = "a field v1 never declared"

    with pytest.raises(UnknownFieldError) as raised:
        read_bundle(document)

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
