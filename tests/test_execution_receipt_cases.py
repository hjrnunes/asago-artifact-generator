"""The execution-receipt-v1 kit: its lock, and each case against the schema.

This repository owns ``contracts/execution-receipt/``; the tool adapters write receipts
that validate against it, and the orchestrator keeps a byte-identical copy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from asago_artifact_generator.contract_kit import verify_contract_lock

_ROOT = Path(__file__).resolve().parents[1] / "contracts" / "execution-receipt"
_VERSION = "execution-receipt-v1"
_CASES = _ROOT / _VERSION


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    return Draft202012Validator(_json(_ROOT / f"{_VERSION}.schema.json"))


def _cases(kind: str) -> list[Path]:
    return sorted((_CASES / kind).glob("*.json"))


def test_the_lock_holds_every_file() -> None:
    lock = _json(_ROOT / "CONTRACT.lock")

    verify_contract_lock(
        _ROOT,
        AssertionError,
        lock_label="lock",
        member_label="execution receipt",
        metadata={"contract": "execution-receipt", "schema_version": _VERSION},
        metadata_message="lock metadata mismatch",
    )
    assert set(lock["files"]) == {
        path.relative_to(_ROOT).as_posix() for path in _ROOT.rglob("*.json")
    }


@pytest.mark.parametrize("case", _cases("valid"), ids=lambda case: case.stem)
def test_every_valid_case_validates(case: Path) -> None:
    assert list(_validator().iter_errors(_json(case))) == []


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
