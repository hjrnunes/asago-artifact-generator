"""The installed ``tool-bundle`` contract: copy, identity, reader rules and cases.

Both tool adapters write ``bundle.json`` and read it back. These tests compare the
installed copy with the authority, recompute its spec digest, read Garak's manifests,
and assert the reader's refusals.
"""

from __future__ import annotations

import json
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
    VERSION,
    installed_specs,
    read_bundle,
)

REPO = Path(__file__).resolve().parents[3]
_AUTHORITY_ROOT = REPO / "contracts" / "tool-bundle"
_INSTALLED_ROOT = (
    Path(__file__).resolve().parents[1] / "src/asago_bundle_core/contracts/tool-bundle"
)
_VERSIONS = (VERSION,)
_DATA = Path(__file__).resolve().parent / "data"
#: Manifests the Garak compiler wrote for a command-attempt and a reply package.
_WRITTEN_BUNDLES = ("tool-bundle-v1-command.json", "tool-bundle-v1-reply.json")


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
    assert spec.envelope == "required"
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
    with pytest.raises(MissingEnvelopeError):
        read_bundle(document)


@pytest.mark.parametrize("name", _WRITTEN_BUNDLES)
def test_the_reader_accepts_a_bundle_the_adapter_wrote(name: str) -> None:
    """A manifest the Garak compiler wrote reads at the current version, envelope included."""

    assert read_bundle(_json(_DATA / name)).version == VERSION


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unsupported_version_names_the_supported_versions(version: str) -> None:
    case = _json(_cases(version, "valid")[0])

    with pytest.raises(UnsupportedVersionError) as raised:
        _specs().read(_enveloped(_spec(version), case, version="tool-bundle-v9"))

    assert "tool-bundle-v9" in str(raised.value)
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
