"""The mirrored runtime-context-v1 kit: cases, lock, and the loader's error text.

Orch owns ``contracts/runtime-context/``; this repository holds a byte-identical
mirror that ``load_target_inputs`` validates a runtime-context file against.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from asago_artifact_generator.target_inputs import _first_schema_fault

_ROOT = Path(__file__).resolve().parents[1] / "contracts" / "runtime-context"
_VERSION = "runtime-context-v1"
_CASES = _ROOT / _VERSION


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema() -> dict[str, Any]:
    return _json(_ROOT / f"{_VERSION}.schema.json")


def _cases(kind: str) -> list[Path]:
    return sorted((_CASES / kind).glob("*.json"))


def test_the_schema_is_a_valid_draft_2020_12_schema() -> None:
    Draft202012Validator.check_schema(_schema())


@pytest.mark.parametrize("case", _cases("valid"), ids=lambda case: case.stem)
def test_the_loader_accepts_every_valid_case(case: Path) -> None:
    assert _first_schema_fault(_schema(), _json(case)) is None


def test_every_invalid_case_names_its_violation() -> None:
    expected = _json(_CASES / "expected-violations.json")

    assert set(expected) == {f"invalid/{case.name}" for case in _cases("invalid")}


@pytest.mark.parametrize("case", _cases("invalid"), ids=lambda case: case.stem)
def test_the_loader_reports_each_invalid_case_at_the_expected_location(case: Path) -> None:
    expected = _json(_CASES / "expected-violations.json")[f"invalid/{case.name}"]
    location = expected["path"].strip("/").replace("/", ".") or "<root>"

    fault = _first_schema_fault(_schema(), _json(case))

    assert fault is not None
    assert fault.startswith(f"at {location}: {expected['keyword']}")


def test_the_fault_text_never_echoes_the_instance() -> None:
    secret = "needle-" + "x" * 5000
    read = {
        "profile_digest": "a" * 64,
        "tool_name": "lookup_record",
        "arguments": None,
        "result": {"content": [{"type": "text", "text": secret}]},
        "status": {"transport": "unverified", "content": "untrusted"},
    }
    document = {
        "state": {"note": secret},
        "target_profile_digest": "a" * 64,
        "read_observations": [read] * 15,
    }

    fault = _first_schema_fault(_schema(), document)

    assert fault == "at read_observations.0.status.transport: const"
    assert len(fault) < 500


def test_unknown_keys_are_named_within_a_bounded_message() -> None:
    document = {"state": {}, "target_profile_digest": "a" * 64}
    document.update({f"{n:04d}-" + "x" * 5000: "y" * 5000 for n in range(1000)})

    fault = _first_schema_fault(_schema(), document)

    assert fault is not None
    assert fault.endswith(" (+990 more))")
    assert len(fault) < 10 * (64 + 3 + 2) + 100


def test_a_missing_key_is_named_from_the_schema_not_the_instance() -> None:
    fault = _first_schema_fault(
        _schema(), {"state": {}, "target_profile_digest": "a" * 64, "read_observations": [{}]}
    )

    assert fault == (
        "at read_observations.0: required (profile_digest, tool_name, status, result)"
    )


def test_the_lock_names_the_authority_and_every_file_digest() -> None:
    lock = _json(_ROOT / "CONTRACT.lock")
    files = {
        path.relative_to(_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(_ROOT.rglob("*"))
        if path.is_file() and path.name != "CONTRACT.lock"
    }

    assert lock["authority"] == "asago-orch"
    assert lock["contract"] == "runtime-context"
    assert lock["schema_version"] == _VERSION
    assert lock["files"] == files
