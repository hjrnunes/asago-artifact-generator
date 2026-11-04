"""The midojo-delivery kit: its runtime spec, lock, cases and reader rules.

This repository owns ``contracts/midojo-delivery/``: the runner of a MiDojo episode writes
``delivery.json``, and the MiDojo adapter's parse reads it. ``midojo-delivery-v1/`` holds
the runtime spec (``spec.json`` and ``schema.json``), the cases under ``cases/`` and the
docs; the spec digest covers the runtime spec alone.
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

_ROOT = Path(__file__).resolve().parents[1] / "contracts" / "midojo-delivery"
_CONTRACTS = _ROOT.parent
_VERSION = "midojo-delivery-v1"
_SPEC_ROOT = _ROOT / _VERSION
_CASES = _SPEC_ROOT / "cases"
_SCHEMA = _SPEC_ROOT / "schema.json"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _specs() -> SpecSet:
    return load_specs(_CONTRACTS)


def _spec() -> ContractSpec:
    return _specs().spec("midojo-delivery", _VERSION)


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_json(_SCHEMA))


def _cases(kind: str) -> list[Path]:
    return sorted((_CASES / kind).glob("*.json"))


def _enveloped(spec: ContractSpec, document: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return a case document with the envelope, overriding envelope fields."""

    envelope = {
        "id": spec.contract,
        "version": spec.version,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.midojo_delivery",
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
        member_label="midojo delivery",
        metadata={"contract": "midojo-delivery", "schema_version": _VERSION},
        metadata_message="lock metadata mismatch",
    )
    assert set(lock["files"]) == {
        path.relative_to(_ROOT).as_posix()
        for path in _ROOT.rglob("*")
        if path.is_file() and path.name != "CONTRACT.lock"
    }


def test_the_spec_identifies_the_runtime_spec_alone() -> None:
    spec = _spec()
    body = _json(_SPEC_ROOT / "spec.json")

    assert (spec.contract, spec.version, spec.status, spec.envelope) == (
        "midojo-delivery",
        _VERSION,
        "published",
        "optional",
    )
    assert spec.supports == ()
    assert spec.extensions == ()
    assert body["cases"] == "cases"
    assert spec.spec_digest == spec_digest(body, _SCHEMA.read_bytes())


@pytest.mark.parametrize("case", _cases("valid"), ids=lambda case: case.stem)
def test_every_valid_case_validates(case: Path) -> None:
    assert list(_validator().iter_errors(_json(case))) == []


@pytest.mark.parametrize("case", _cases("valid"), ids=lambda case: case.stem)
def test_the_reader_accepts_every_valid_case_as_a_legacy_document(case: Path) -> None:
    assert _specs().read(_json(case)).version == _VERSION


@pytest.mark.parametrize("case", _cases("valid"), ids=lambda case: case.stem)
def test_the_reader_accepts_every_valid_case_with_the_envelope(case: Path) -> None:
    written = write_envelope(
        _json(case),
        _spec(),
        writer="tests.midojo_delivery",
        writer_version="0.1.0",
    )

    assert _specs().read(written).version == _VERSION


def test_every_invalid_case_names_its_violation() -> None:
    expected = _json(_CASES / "expected-violations.json")

    assert set(expected) == {f"invalid/{case.name}" for case in _cases("invalid")}


@pytest.mark.parametrize("case", _cases("invalid"), ids=lambda case: case.stem)
def test_each_invalid_case_fails_first_where_it_says(case: Path) -> None:
    expected = _json(_CASES / "expected-violations.json")[f"invalid/{case.name}"]

    errors = sorted(_validator().iter_errors(_json(case)), key=lambda e: list(e.absolute_path))

    assert errors
    first = errors[0]
    path = "/" + "/".join(str(part) for part in first.absolute_path)
    assert {"path": path, "keyword": first.validator} == expected


def test_an_unsupported_version_names_the_supported_versions() -> None:
    case = _json(_CASES / "valid" / "emitted.json")

    with pytest.raises(UnsupportedVersionError) as raised:
        _specs().read(_enveloped(_spec(), case, version="midojo-delivery-v9"))

    assert "midojo-delivery-v9" in str(raised.value)
    assert "midojo-delivery-v1" in str(raised.value)


def test_an_unknown_contract_names_the_installed_one() -> None:
    case = _json(_CASES / "valid" / "emitted.json")

    with pytest.raises(UnknownContractError) as raised:
        _specs().read(_enveloped(_spec(), case, id="not-a-contract"))

    assert "midojo-delivery" in str(raised.value)


def test_an_unknown_requires_entry_is_rejected() -> None:
    case = _json(_CASES / "valid" / "emitted.json")

    with pytest.raises(UnknownRequirementError) as raised:
        _specs().read(_enveloped(_spec(), case, requires=["midojo-delivery-v2"]))

    assert "midojo-delivery-v2" in str(raised.value)


def test_an_unknown_field_outside_an_extension_is_rejected() -> None:
    case = _json(_CASES / "valid" / "emitted.json")
    document = _enveloped(_spec(), case)
    document["probe_key"] = "a field v1 never declared"

    with pytest.raises(UnknownFieldError) as raised:
        _specs().read(document)

    assert "probe_key" in str(raised.value)
    assert "declared extensions: none" in str(raised.value)


def test_a_draft_with_a_mismatched_spec_digest_is_rejected() -> None:
    case = _json(_CASES / "valid" / "emitted.json")
    draft = replace(_spec(), status="draft")
    other = "sha256:" + "0" * 64

    with pytest.raises(SpecDigestMismatchError) as raised:
        SpecSet((draft,)).read(_enveloped(draft, case, spec_digest=other))

    assert "draft" in str(raised.value)
    assert other in str(raised.value)
