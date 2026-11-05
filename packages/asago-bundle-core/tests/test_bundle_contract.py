"""The installed ``tool-bundle`` contract: copy, identity, reader rules and cases.

Both tool adapters write ``bundle.json`` and read it back; this library ships the versions
their readers enforce. The tests compare that copy with the consumer's authority, recompute
each spec digest, read every recorded bundle, and assert the four refusals a reader owes
against both versions. v2 renames the manifest's ``delivery`` to ``conversation_mode`` and
its top-level ``requires`` to ``values_required``; v1 documents keep reading.
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

from asago_bundle_core.bundle_contract import (
    CONTRACT,
    LEGACY_VERSION,
    VERSION,
    installed_specs,
    read_bundle,
)

REPO = Path(__file__).resolve().parents[3]
_AUTHORITY_ROOT = REPO / "contracts" / "tool-bundle"
_INSTALLED_ROOT = (
    Path(__file__).resolve().parents[1] / "src/asago_bundle_core/contracts/tool-bundle"
)
_VERSIONS = (LEGACY_VERSION, VERSION)
#: The workspace checkout holds the replay corpus beside this repository.
_REPLAY_CORPUS = REPO.parent / "replay-corpus"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _specs() -> SpecSet:
    return load_specs(_INSTALLED_ROOT.parent)


def _spec(version: str = VERSION) -> ContractSpec:
    return installed_specs().spec(CONTRACT, version)


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _cases(version: str, kind: str) -> list[Path]:
    return sorted((_AUTHORITY_ROOT / version / "cases" / kind).glob("*.json"))


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
    for version in _VERSIONS:
        installed = _INSTALLED_ROOT / version
        assert installed.is_dir()
        assert _files(installed) == _files(_AUTHORITY_ROOT / version)


@pytest.mark.parametrize("version", _VERSIONS)
def test_each_installed_spec_is_the_tool_bundle_version(version: str) -> None:
    spec = _spec(version)
    body = _json(_INSTALLED_ROOT / version / "spec.json")

    assert (spec.contract, spec.version, spec.status) == (CONTRACT, version, "published")
    assert spec.envelope == ("required" if version == VERSION else "optional")
    assert spec.supports == ()
    assert spec.extensions == ()
    assert (spec.cases, spec.docs) == ("cases", "README.md")
    assert body["cases"] == "cases"
    assert body["docs"] == "README.md"
    assert spec.spec_digest == spec_digest(
        body, (_INSTALLED_ROOT / version / "schema.json").read_bytes()
    )


@pytest.mark.parametrize(
    ("version", "case"),
    [(version, case) for version in _VERSIONS for case in _cases(version, "valid")],
    ids=lambda value: value.name if isinstance(value, Path) else value,
)
def test_the_reader_accepts_every_valid_case(version: str, case: Path) -> None:
    spec = _spec(version)
    document = _json(case)
    written = write_envelope(
        document, spec, writer="tests.bundle_contract", writer_version="0.1.0"
    )

    assert read_bundle(written).version == version
    if spec.envelope == "optional":
        assert read_bundle(document).version == version
    else:
        with pytest.raises(MissingEnvelopeError):
            read_bundle(document)


def _replay_corpus() -> Path:
    """Return the replay corpus the recorded bundles live in.

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


def test_every_recorded_bundle_still_reads() -> None:
    """Every bundle recorded outside the contract cases reads as a v1 document.

    The replay corpus holds the bundle manifests the recorded runs captured, including the
    pre-M2 and M5 packages RECEIPT-REPLAY re-parses. A missing corpus, or one with no
    bundles, fails: a test that reads nothing must not pass.
    """

    corpus = _replay_corpus()
    recorded = sorted(corpus.rglob("bundle.json"))
    if not recorded:
        pytest.fail(f"no recorded bundles under {corpus}")

    for path in recorded:
        assert read_bundle(_json(path)).version == LEGACY_VERSION, path


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unsupported_version_names_the_supported_versions(version: str) -> None:
    case = _json(_cases(version, "valid")[0])

    with pytest.raises(UnsupportedVersionError) as raised:
        _specs().read(_enveloped(_spec(version), case, version="tool-bundle-v9"))

    assert "tool-bundle-v9" in str(raised.value)
    assert LEGACY_VERSION in str(raised.value)
    assert VERSION in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_contract_names_the_installed_one(version: str) -> None:
    case = _json(_cases(version, "valid")[0])

    with pytest.raises(UnknownContractError) as raised:
        _specs().read(_enveloped(_spec(version), case, id="not-a-contract"))

    assert CONTRACT in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_requires_entry_is_rejected(version: str) -> None:
    case = _json(_cases(version, "valid")[0])

    with pytest.raises(UnknownRequirementError) as raised:
        _specs().read(_enveloped(_spec(version), case, requires=["future-feature"]))

    assert "future-feature" in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_field_outside_an_extension_is_rejected(version: str) -> None:
    case = _json(_cases(version, "valid")[0])
    document = _enveloped(_spec(version), case)
    document["probe_key"] = f"a field {version} never declared"

    with pytest.raises(UnknownFieldError) as raised:
        read_bundle(document)

    assert "probe_key" in str(raised.value)
    assert "declared extensions: none" in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_a_draft_with_a_mismatched_spec_digest_is_rejected(version: str) -> None:
    case = _json(_cases(version, "valid")[0])
    draft = replace(_spec(version), status="draft")
    other = "sha256:" + "0" * 64

    with pytest.raises(SpecDigestMismatchError) as raised:
        SpecSet((draft,)).read(_enveloped(draft, case, spec_digest=other))

    assert "draft" in str(raised.value)
    assert other in str(raised.value)
