"""The capability-gap contract: schema, cases, lock, and the records adapters write.

This repository owns ``capability-gap-v1``; the orchestrator mirrors the
directory byte-identically and refuses a gap record that fails it. Every valid
case validates, every invalid case fails at the instance path and keyword
``expected-violations.json`` names, the lock lists every file with its digest,
and what ``capability_gap_record`` writes for a Garak and a MiDojo refusal
validates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from asago_artifact_generator.package_io import load_package
from asago_bundle_core.gap import capability_gap_record
from asago_bundle_core.testing import sequential_stimulus, write_test_package

_ROOT = Path(__file__).resolve().parents[3] / "contracts" / "capability-gap"
_VERSION = "capability-gap-v1"
_CASES = _ROOT / _VERSION


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_json(_ROOT / f"{_VERSION}.schema.json"))


def _cases(kind: str) -> list[Path]:
    return sorted((_CASES / kind).glob("*.json"))


def test_the_schema_is_a_valid_draft_2020_12_schema() -> None:
    schema = _json(_ROOT / f"{_VERSION}.schema.json")

    Draft202012Validator.check_schema(schema)
    assert schema["properties"]["kind"] == {"const": "capability_gap"}


def test_the_contract_has_valid_and_invalid_cases() -> None:
    assert _cases("valid")
    assert _cases("invalid")


@pytest.mark.parametrize("case", _cases("valid"), ids=lambda case: case.stem)
def test_the_schema_accepts_every_valid_case(case: Path) -> None:
    assert list(_validator().iter_errors(_json(case))) == []


def test_every_invalid_case_names_its_violation() -> None:
    expected = _json(_CASES / "expected-violations.json")

    assert set(expected) == {f"invalid/{case.name}" for case in _cases("invalid")}


@pytest.mark.parametrize("case", _cases("invalid"), ids=lambda case: case.stem)
def test_the_schema_rejects_every_invalid_case_where_it_names(case: Path) -> None:
    expected = _json(_CASES / "expected-violations.json")[f"invalid/{case.name}"]

    error = best_match(_validator().iter_errors(_json(case)))

    assert error is not None
    assert {
        "path": "/" + "/".join(str(part) for part in error.absolute_path),
        "keyword": error.validator,
    } == expected


@pytest.mark.parametrize(
    ("tool", "delivery", "reason"),
    [
        (
            "garak",
            "sequential",
            "injection.SequentialInjection sends user turns only; this "
            "sequential history holds another message",
        ),
        (
            "midojo",
            "sequential",
            "MiDojo sends one prompt per evaluation; this package holds several user turns",
        ),
        ("midojo", "single", "the state_effect claim is not graded by this adapter"),
    ],
)
def test_what_capability_gap_record_writes_validates(
    tmp_path: Path, tool: str, delivery: str, reason: str
) -> None:
    package = load_package(write_test_package(tmp_path, stimulus=sequential_stimulus()))

    record = capability_gap_record(tool, package, delivery, reason)

    assert list(_validator().iter_errors(record)) == []
    assert record["package_digest"] == package.manifest.manifest_digest


def test_the_lock_names_the_authority_and_every_file_digest() -> None:
    lock = _json(_ROOT / "CONTRACT.lock")
    files = {
        path.relative_to(_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(_ROOT.rglob("*"))
        if path.is_file() and path.name != "CONTRACT.lock"
    }

    assert lock["authority"] == "asago-artifact-generator"
    assert lock["contract"] == "capability-gap"
    assert lock["schema_version"] == _VERSION
    assert lock["files"] == files
