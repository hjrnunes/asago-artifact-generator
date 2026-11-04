"""The runtime-contract kit: its schema, cases, and lock, and the loader that applies it.

This repository owns ``contracts/runtime-contract/``; the orchestrator writes one
contract per target and keeps a byte-identical copy of the kit. The version directory
holds the runtime spec, the cases and the docs, and the loader reads it through the
installed spec set before the hand check that words a fault for the CLI.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from asago_contracts_core import ENVELOPE_FIELD, ContractSpec, spec_digest, write_envelope
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from asago_artifact_generator import runtime_contract
from asago_artifact_generator.runtime_contract import RuntimeContractError, load_runtime_contract
from asago_package.contract_kit import contract_dir
from asago_package.runtime_contract_spec import installed_specs

_ROOT = contract_dir("runtime-contract")
_VERSION = "artifact-runtime-contract-v2"
_SPEC_ROOT = _ROOT / _VERSION
_CASES = _SPEC_ROOT / "cases"
_SCHEMA = _SPEC_ROOT / "schema.json"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema() -> dict[str, Any]:
    return _json(_SCHEMA)


def _spec() -> ContractSpec:
    return installed_specs().spec("runtime-contract", _VERSION)


def _enveloped(spec: ContractSpec, document: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return a case document with the envelope, overriding envelope fields."""

    envelope = {
        "id": spec.contract,
        "version": spec.version,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.runtime_contract_cases",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
        **overrides,
    }
    return {**document, ENVELOPE_FIELD: envelope}


def _cases(kind: str) -> list[Path]:
    return sorted((_CASES / kind).glob("*.json"))


def _first_error(document: Any) -> ValidationError | None:
    errors = sorted(
        Draft202012Validator(_schema()).iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.path),
    )
    return errors[0] if errors else None


def test_the_schema_is_a_valid_draft_2020_12_schema() -> None:
    Draft202012Validator.check_schema(_schema())


@pytest.mark.parametrize("case", _cases("valid"), ids=lambda case: case.stem)
def test_every_valid_case_passes_the_schema(case: Path) -> None:
    assert _first_error(_json(case)) is None


def test_every_invalid_case_names_its_violation() -> None:
    expected = _json(_CASES / "expected-violations.json")

    assert set(expected) == {f"invalid/{case.name}" for case in _cases("invalid")}


@pytest.mark.parametrize("case", _cases("invalid"), ids=lambda case: case.stem)
def test_each_invalid_case_fails_first_at_the_expected_location(case: Path) -> None:
    expected = _json(_CASES / "expected-violations.json")[f"invalid/{case.name}"]

    error = _first_error(_json(case))

    assert error is not None
    assert "/" + "/".join(str(part) for part in error.path) == expected["path"]
    assert error.validator == expected["keyword"]


def test_the_lock_names_the_authority_and_every_file_digest() -> None:
    lock = _json(_ROOT / "CONTRACT.lock")
    files = {
        path.relative_to(_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(_ROOT.rglob("*"))
        if path.is_file() and path.name != "CONTRACT.lock"
    }

    assert lock["authority"] == "asago-artifact-generator"
    assert lock["contract"] == "runtime-contract"
    assert lock["schema_version"] == _VERSION
    assert lock["files"] == files


def test_the_spec_identifies_the_runtime_spec_alone() -> None:
    spec = _spec()
    body = _json(_SPEC_ROOT / "spec.json")

    assert (spec.contract, spec.version, spec.status, spec.envelope) == (
        "runtime-contract",
        _VERSION,
        "published",
        "optional",
    )
    assert spec.supports == ()
    assert spec.extensions == ()
    assert body["cases"] == "cases"
    assert body["docs"] == "README.md"
    assert spec.spec_digest == spec_digest(body, _SCHEMA.read_bytes())


@pytest.mark.parametrize("case", _cases("valid"), ids=lambda case: case.stem)
def test_the_loader_returns_every_valid_case_as_written(case: Path) -> None:
    assert load_runtime_contract(case) == _json(case)


@pytest.mark.parametrize("case", _cases("valid"), ids=lambda case: case.stem)
def test_the_loader_accepts_every_valid_case_with_the_envelope(case: Path, tmp_path: Path) -> None:
    written = write_envelope(
        _json(case), _spec(), writer="tests.runtime_contract_cases", writer_version="0.1.0"
    )
    path = tmp_path / "runtime-contract.json"
    path.write_text(json.dumps(written), encoding="utf-8")

    assert load_runtime_contract(path) == written


def test_the_loader_reads_a_yaml_contract(tmp_path: Path) -> None:
    document = _json(_CASES / "valid" / "writer-complete.json")
    path = tmp_path / "runtime-contract.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    assert load_runtime_contract(path) == document


def test_the_loader_names_the_first_schema_fault() -> None:
    case = _CASES / "invalid" / "slot-missing-max-chars.json"

    with pytest.raises(RuntimeContractError) as raised:
        load_runtime_contract(case)

    assert str(raised.value) == (
        "runtime contract schema invalid at seed_slots.0: 'max_chars' is a required property"
    )


@pytest.mark.parametrize("content", [None, "{unclosed", "key: [unclosed\n"])
def test_the_loader_rejects_a_file_it_cannot_read(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / ("runtime-contract.json" if content != "key: [unclosed\n" else "rc.yaml")
    if content is not None:
        path.write_text(content, encoding="utf-8")

    with pytest.raises(RuntimeContractError, match="^cannot read runtime contract: "):
        load_runtime_contract(path)


def test_the_loader_refuses_a_kit_whose_files_differ_from_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit = tmp_path / "runtime-contract"
    shutil.copytree(_ROOT, kit)
    schema = kit / _VERSION / "schema.json"
    schema.write_text(schema.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    monkeypatch.setattr(runtime_contract, "_CONTRACT_ROOT", kit)

    with pytest.raises(RuntimeContractError, match="digest mismatch"):
        load_runtime_contract(_CASES / "valid" / "minimal.json")


def test_the_loader_refuses_an_unsupported_version_naming_the_supported_ones(
    tmp_path: Path,
) -> None:
    document = _json(_CASES / "valid" / "minimal.json")
    document["schema_version"] = "artifact-runtime-contract-v9"
    path = tmp_path / "runtime-contract.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RuntimeContractError) as raised:
        load_runtime_contract(path)

    assert "artifact-runtime-contract-v9" in str(raised.value)
    assert _VERSION in str(raised.value)


def test_the_loader_refuses_an_unknown_requires_entry(tmp_path: Path) -> None:
    written = _enveloped(_spec(), _json(_CASES / "valid" / "minimal.json"), requires=["v3"])
    path = tmp_path / "runtime-contract.json"
    path.write_text(json.dumps(written), encoding="utf-8")

    with pytest.raises(RuntimeContractError) as raised:
        load_runtime_contract(path)

    assert "'v3'" in str(raised.value)
    assert "the spec supports: none" in str(raised.value)


def test_the_loader_refuses_an_unknown_field_outside_an_extension(tmp_path: Path) -> None:
    document = _json(_CASES / "valid" / "minimal.json")
    document["probe_key"] = "a field v2 never declared"
    path = tmp_path / "runtime-contract.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RuntimeContractError) as raised:
        load_runtime_contract(path)

    assert "probe_key" in str(raised.value)


def test_the_loader_refuses_a_mismatched_spec_digest(tmp_path: Path) -> None:
    written = _enveloped(
        _spec(), _json(_CASES / "valid" / "minimal.json"), spec_digest="sha256:" + "0" * 64
    )
    path = tmp_path / "runtime-contract.json"
    path.write_text(json.dumps(written), encoding="utf-8")

    with pytest.raises(RuntimeContractError) as raised:
        load_runtime_contract(path)

    assert "does not match the installed spec digest" in str(raised.value)
    assert "published" in str(raised.value)
