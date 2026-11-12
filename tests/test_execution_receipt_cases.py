"""The execution-receipt kit: its runtime specs, lock, cases and reader rules.

This repository owns ``contracts/execution-receipt/``: the tool adapters write receipts that
validate against it, and the orchestrator keeps a byte-identical copy. The runtime spec
names the evaluation, its driver and the slot key. Its spec digest covers the runtime
spec alone.
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
_VERSIONS = ("execution-receipt-v2",)
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
    expected = "required"

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


_UTILITY_CASES = {
    "evaluation-utility-not-applicable.json": ("not_applicable", None, "no_utility_check"),
    "evaluation-utility-baseline-security.json": ("not_applicable", None, "baseline_security"),
    "evaluation-utility-intermediate-turn.json": ("not_applicable", None, "intermediate_turn"),
    "evaluation-utility-applicable.json": ("applicable", False, None),
}


def _utility_case(**utility: Any) -> dict[str, Any]:
    case = _json(
        _spec_root(_LATEST) / "cases" / "valid" / "evaluation-utility-not-applicable.json"
    )
    case["evaluation"]["utility"].update(utility)
    return case


def _utility_errors(case: dict[str, Any]) -> list[tuple[list[Any], str]]:
    errors = sorted(_validator(_LATEST).iter_errors(case), key=lambda e: list(e.absolute_path))
    return [(list(error.absolute_path), error.validator) for error in errors]


@pytest.mark.parametrize("name", _UTILITY_CASES, ids=list(_UTILITY_CASES))
def test_a_receipt_may_record_whether_its_utility_applies(name: str) -> None:
    case = _json(_spec_root(_LATEST) / "cases" / "valid" / name)
    applicability, value, reason = _UTILITY_CASES[name]

    utility = case["evaluation"]["utility"]
    assert (utility["applicability"], utility["value"], utility["reason"]) == (
        applicability,
        value,
        reason,
    )
    assert set(utility) == {"value", "applicability", "reason", "snapshot_id", "pairing_id"}
    assert _utility_errors(case) == []


def test_a_receipt_without_a_utility_block_records_no_utility() -> None:
    """The block is optional, and its absence is not the not-applicable record."""

    case = _json(_spec_root(_LATEST) / "cases" / "valid" / "midojo-episode-unknown.json")

    assert "utility" not in case["evaluation"]
    assert _utility_errors(case) == []


@pytest.mark.parametrize("value", [True, False])
def test_a_value_set_while_not_applicable_is_invalid(value: bool) -> None:
    """MiDojo writes ``utility: false`` beside ``not_applicable``; the receipt never states it."""

    assert _utility_errors(_utility_case(value=value)) == [
        (["evaluation", "utility", "value"], "type")
    ]


@pytest.mark.parametrize("reason", ["no_utility_check", "baseline_security", "intermediate_turn"])
@pytest.mark.parametrize("value", [True, False, None])
def test_an_applicable_utility_states_its_value_or_none_and_no_reason(
    value: bool | None, reason: str
) -> None:
    case = _utility_case(value=value, applicability="applicable", reason=None)
    assert _utility_errors(case) == []

    case["evaluation"]["utility"]["reason"] = reason
    assert _utility_errors(case) == [(["evaluation", "utility", "reason"], "type")]


@pytest.mark.parametrize("reason", ["no_utility_check", "baseline_security", "intermediate_turn"])
def test_each_not_applicable_reason_validates(reason: str) -> None:
    assert _utility_errors(_utility_case(reason=reason)) == []


@pytest.mark.parametrize(
    "member", ["value", "applicability", "reason", "snapshot_id", "pairing_id"]
)
def test_every_utility_member_is_written(member: str) -> None:
    case = _utility_case()
    del case["evaluation"]["utility"][member]

    first, *_ = _utility_errors(case)
    assert first == (["evaluation", "utility"], "required")


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
    old_key = "episode"
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
