"""The tool-bundle kit: both runtime specs, the lock, the cases and the reader rules.

This repository owns ``contracts/tool-bundle/``: both tool adapters write ``bundle.json``
documents that validate against it, and the orchestrator keeps a byte-identical copy.
``tool-bundle-v1/`` and ``tool-bundle-v2/`` hold the runtime specs (``spec.json`` and
``schema.json``), the cases under ``cases/`` and the docs; a spec digest covers its own
runtime spec alone. v2 renames the manifest's ``delivery`` to ``conversation_mode`` and its
top-level ``requires`` to ``values_required``; every reader accepts both versions.
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
from jsonschema import Draft202012Validator

from asago_artifact_generator.contract_kit import verify_contract_lock

_ROOT = Path(__file__).resolve().parents[1] / "contracts" / "tool-bundle"
_CONTRACTS = _ROOT.parent
_VERSIONS = ("tool-bundle-v1", "tool-bundle-v2")
_VERSION = "tool-bundle-v2"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _specs() -> SpecSet:
    return load_specs(_CONTRACTS)


def _spec(version: str = _VERSION) -> ContractSpec:
    return _specs().spec("tool-bundle", version)


def _validator(version: str = _VERSION) -> Draft202012Validator:
    return Draft202012Validator(_json(_ROOT / version / "schema.json"))


def _cases(version: str, kind: str) -> list[Path]:
    return sorted((_ROOT / version / "cases" / kind).glob("*.json"))


def _enveloped(spec: ContractSpec, document: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return a case document with the envelope, overriding envelope fields."""

    envelope = {
        "id": spec.contract,
        "version": spec.version,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.tool_bundle",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
        **overrides,
    }
    return {**document, ENVELOPE_FIELD: envelope}


def test_the_lock_holds_every_file() -> None:
    lock = _json(_ROOT / "CONTRACT.lock")

    verify_contract_lock(
        _ROOT,
        AssertionError,
        lock_label="lock",
        member_label="tool bundle",
        metadata={"contract": "tool-bundle", "schema_version": _VERSION},
        metadata_message="lock metadata mismatch",
    )
    assert set(lock["files"]) == {
        path.relative_to(_ROOT).as_posix()
        for path in _ROOT.rglob("*")
        if path.is_file() and path.name != "CONTRACT.lock"
    }


@pytest.mark.parametrize("version", _VERSIONS)
def test_the_spec_identifies_the_runtime_spec_alone(version: str) -> None:
    spec = _spec(version)
    body = _json(_ROOT / version / "spec.json")

    assert (spec.contract, spec.version, spec.status) == ("tool-bundle", version, "published")
    assert spec.envelope == ("required" if version == _VERSION else "optional")
    assert spec.supports == ()
    assert spec.extensions == ()
    assert (spec.schema, spec.cases, spec.docs) == (
        _json(_ROOT / version / "schema.json"),
        "cases",
        "README.md",
    )
    assert body["cases"] == "cases"
    assert body["docs"] == "README.md"
    assert spec.spec_digest == spec_digest(body, (_ROOT / version / "schema.json").read_bytes())


def test_v2_renames_the_delivery_and_requires_fields() -> None:
    v1 = _json(_ROOT / "tool-bundle-v1" / "schema.json")["properties"]
    v2 = _json(_ROOT / "tool-bundle-v2" / "schema.json")["properties"]

    assert {"delivery", "requires"} <= v1.keys()
    assert {"conversation_mode", "values_required"} <= v2.keys()
    assert {"delivery", "requires"}.isdisjoint(v2)


@pytest.mark.parametrize(
    ("version", "case"),
    [(version, case) for version in _VERSIONS for case in _cases(version, "valid")],
    ids=lambda value: value.name if isinstance(value, Path) else value,
)
def test_every_valid_case_validates(version: str, case: Path) -> None:
    assert list(_validator(version).iter_errors(_json(case))) == []


@pytest.mark.parametrize(
    ("version", "case"),
    [(version, case) for version in _VERSIONS for case in _cases(version, "valid")],
    ids=lambda value: value.name if isinstance(value, Path) else value,
)
def test_the_reader_accepts_every_valid_case_with_its_version(version: str, case: Path) -> None:
    spec = _spec(version)
    document = _json(case)
    written = write_envelope(document, spec, writer="tests.tool_bundle", writer_version="0.1.0")

    assert _specs().read(written).version == version
    if spec.envelope == "optional":
        assert _specs().read(document).version == version
    else:
        with pytest.raises(MissingEnvelopeError):
            _specs().read(document)


def test_the_reader_accepts_both_versions() -> None:
    legacy = _spec("tool-bundle-v1")
    current = _spec(_VERSION)
    v1_case = _json(_cases("tool-bundle-v1", "valid")[0])
    v2_case = _json(_cases(_VERSION, "valid")[0])

    assert _specs().read(v1_case) == legacy
    assert _specs().read(_enveloped(legacy, v1_case)) == legacy
    assert _specs().read(_enveloped(current, v2_case)) == current
    with pytest.raises(MissingEnvelopeError):
        _specs().read(v2_case)


@pytest.mark.parametrize("version", _VERSIONS)
def test_every_invalid_case_names_its_violation(version: str) -> None:
    expected = _json(_ROOT / version / "cases" / "expected-violations.json")

    assert set(expected) == {f"invalid/{case.name}" for case in _cases(version, "invalid")}


@pytest.mark.parametrize(
    ("version", "case"),
    [(version, case) for version in _VERSIONS for case in _cases(version, "invalid")],
    ids=lambda value: value.name if isinstance(value, Path) else value,
)
def test_each_invalid_case_fails_first_where_it_says(version: str, case: Path) -> None:
    expected = _json(_ROOT / version / "cases" / "expected-violations.json")[
        f"invalid/{case.name}"
    ]

    errors = sorted(
        _validator(version).iter_errors(_json(case)),
        key=lambda e: list(e.absolute_path),
    )

    assert errors
    first = errors[0]
    path = "/" + "/".join(str(part) for part in first.absolute_path)
    assert {"path": path, "keyword": first.validator} == expected


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unsupported_version_names_the_supported_versions(version: str) -> None:
    case = _json(_cases(version, "valid")[0])

    with pytest.raises(UnsupportedVersionError) as raised:
        _specs().read(_enveloped(_spec(version), case, version="tool-bundle-v9"))

    assert "tool-bundle-v9" in str(raised.value)
    assert "tool-bundle-v1" in str(raised.value)
    assert "tool-bundle-v2" in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_contract_names_the_installed_one(version: str) -> None:
    case = _json(_cases(version, "valid")[0])

    with pytest.raises(UnknownContractError) as raised:
        _specs().read(_enveloped(_spec(version), case, id="not-a-contract"))

    assert "tool-bundle" in str(raised.value)


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
        _specs().read(document)

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
