"""The execution-receipt kit: its runtime specs, lock, cases and reader rules.

This repository owns ``contracts/execution-receipt/``: the tool adapters write receipts that
validate against it, and the orchestrator keeps a byte-identical copy. Both versions of the
runtime spec live side by side: ``execution-receipt-v1/`` is the legacy shape and
``execution-receipt-v2/`` names the evaluation, its driver and the slot key. Each version
holds ``spec.json``, ``schema.json``, its ``cases/`` and its docs; a spec digest covers the
runtime spec alone.
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

_ROOT = Path(__file__).resolve().parents[1] / "contracts" / "execution-receipt"
_CONTRACTS = _ROOT.parent
_VERSIONS = ("execution-receipt-v1", "execution-receipt-v2")
_LATEST = _VERSIONS[-1]


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _specs() -> SpecSet:
    return load_specs(_CONTRACTS)


def _spec(version: str = _LATEST) -> ContractSpec:
    return _specs().spec("execution-receipt", version)


def _spec_root(version: str) -> Path:
    return _ROOT / version


def _validator(version: str) -> Draft202012Validator:
    return Draft202012Validator(_json(_spec_root(version) / "schema.json"))


def _cases(version: str, kind: str) -> list[Path]:
    return sorted((_spec_root(version) / "cases" / kind).glob("*.json"))


def _case(version: str, kind: str = "valid") -> dict[str, Any]:
    return _json(_cases(version, kind)[0])


def _enveloped(spec: ContractSpec, document: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return a case document with the envelope, overriding envelope fields."""

    envelope = {
        "id": spec.contract,
        "version": spec.version,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.execution_receipt",
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
        member_label="execution receipt",
        metadata={"contract": "execution-receipt", "schema_version": _LATEST},
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
    body = _json(_spec_root(version) / "spec.json")
    expected = "optional" if version == _VERSIONS[0] else "required"

    assert (spec.contract, spec.version, spec.status, spec.envelope) == (
        "execution-receipt",
        version,
        "published",
        expected,
    )
    assert spec.supports == ()
    assert spec.extensions == ()
    assert body["cases"] == "cases"
    assert body["docs"] == "README.md"
    assert spec.spec_digest == spec_digest(
        body, (_spec_root(version) / "schema.json").read_bytes()
    )


def _versioned_cases(kind: str) -> list[tuple[str, Path]]:
    return [(version, case) for version in _VERSIONS for case in _cases(version, kind)]


def _ids(value: Any) -> str:
    return value.stem if isinstance(value, Path) else str(value)


@pytest.mark.parametrize("version,case", _versioned_cases("valid"), ids=_ids)
def test_every_valid_case_validates(version: str, case: Path) -> None:
    assert list(_validator(version).iter_errors(_json(case))) == []


@pytest.mark.parametrize("case", _cases(_VERSIONS[0], "valid"), ids=lambda case: case.stem)
def test_every_recorded_document_still_reads_as_a_legacy_document(case: Path) -> None:
    """The v1 documents on disk were recorded before the spec existed; all still read."""

    assert _specs().read(_json(case)).version == _VERSIONS[0]


@pytest.mark.parametrize("version,case", _versioned_cases("valid"), ids=_ids)
def test_the_reader_accepts_every_valid_case_with_the_envelope(version: str, case: Path) -> None:
    spec = _spec(version)
    written = write_envelope(
        _json(case),
        spec,
        writer="tests.execution_receipt",
        writer_version="0.1.0",
    )

    assert _specs().read(written).version == version


@pytest.mark.parametrize("case", _cases(_LATEST, "valid"), ids=lambda case: case.stem)
def test_a_v2_document_without_the_envelope_is_refused(case: Path) -> None:
    with pytest.raises(MissingEnvelopeError, match="execution-receipt"):
        _specs().read(_json(case))


@pytest.mark.parametrize("version", _VERSIONS)
def test_every_invalid_case_names_its_violation(version: str) -> None:
    expected = _json(_spec_root(version) / "cases" / "expected-violations.json")

    assert set(expected) == {f"invalid/{case.name}" for case in _cases(version, "invalid")}


@pytest.mark.parametrize("version,case", _versioned_cases("invalid"), ids=_ids)
def test_each_invalid_case_fails_first_where_it_says(version: str, case: Path) -> None:
    expected = _json(_spec_root(version) / "cases" / "expected-violations.json")[
        f"invalid/{case.name}"
    ]

    errors = sorted(
        _validator(version).iter_errors(_json(case)), key=lambda e: list(e.absolute_path)
    )

    assert errors
    first = errors[0]
    path = "/" + "/".join(str(part) for part in first.absolute_path)
    assert {"path": path, "keyword": first.validator} == expected


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unsupported_version_names_the_supported_versions(version: str) -> None:
    spec = _spec(version)

    with pytest.raises(UnsupportedVersionError) as raised:
        _specs().read(_enveloped(spec, _case(version), version="execution-receipt-v9"))

    assert "execution-receipt-v9" in str(raised.value)
    for supported in _VERSIONS:
        assert supported in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_contract_names_the_installed_one(version: str) -> None:
    spec = _spec(version)

    with pytest.raises(UnknownContractError) as raised:
        _specs().read(_enveloped(spec, _case(version), id="not-a-contract"))

    assert "execution-receipt" in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_requires_entry_is_rejected(version: str) -> None:
    spec = _spec(version)

    with pytest.raises(UnknownRequirementError) as raised:
        _specs().read(_enveloped(spec, _case(version), requires=["execution-receipt-v9"]))

    assert "execution-receipt-v9" in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_field_outside_an_extension_is_rejected(version: str) -> None:
    old_key = "episode" if version == _LATEST else "evaluation"
    document = _enveloped(_spec(version), _case(version))
    document[old_key] = "a field this version does not declare"

    with pytest.raises(UnknownFieldError) as raised:
        _specs().read(document)

    assert old_key in str(raised.value)
    assert "declared extensions: none" in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_a_draft_with_a_mismatched_spec_digest_is_rejected(version: str) -> None:
    draft = replace(_spec(version), status="draft")
    other = "sha256:" + "0" * 64

    with pytest.raises(SpecDigestMismatchError) as raised:
        SpecSet((draft,)).read(_enveloped(draft, _case(version), spec_digest=other))

    assert "draft" in str(raised.value)
    assert other in str(raised.value)
