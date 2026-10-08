"""The reader enforces the producer's ownership rules from the mirrored kit.

``contracts/scenario-handoff/ownership-rules.json`` is the producer's file,
copied byte for byte and pinned by the mirrored ``CONTRACT.lock``; the reader
keeps no list of its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator import input_adapter
from asago_artifact_generator.input_adapter import InputSourceError, load_input

_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff"
_RULES = _CONTRACT / "ownership-rules.json"
_KIT = _CONTRACT / "handoff-v4"


def test_the_mirrored_lock_pins_the_ownership_rules() -> None:
    lock = json.loads((_CONTRACT / "CONTRACT.lock").read_text(encoding="utf-8"))

    assert "ownership-rules.json" in lock["files"]


def test_the_reader_uses_the_mirrored_rules_in_file_order() -> None:
    rules = json.loads(_RULES.read_text(encoding="utf-8"))

    keys, patterns = input_adapter._ownership_rules()

    assert keys == frozenset(rules["forbidden_keys"])
    assert [(code, pattern.pattern) for code, pattern in patterns] == [
        (entry["code"], entry["pattern"]) for entry in rules["forbidden_value_patterns"]
    ]


def test_the_reader_keeps_no_ownership_list_of_its_own() -> None:
    assert not hasattr(input_adapter, "_FORBIDDEN_KEYS")
    assert not hasattr(input_adapter, "_FORBIDDEN_VALUE_PATTERNS")


def test_a_rules_file_with_an_unknown_flag_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ownership-rules.json"
    path.write_text(
        json.dumps(
            {
                "forbidden_keys": [],
                "forbidden_value_patterns": [
                    {"code": "beta", "pattern": "beta", "flags": ["DOTALL"]}
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(InputSourceError, match="DOTALL"):
        input_adapter._read_ownership_rules(path)


def test_an_edited_rules_file_fails_the_kit_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "scenario-handoff"
    for path in _CONTRACT.rglob("*"):
        if path.is_file():
            target = mirror / path.relative_to(_CONTRACT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    rules = json.loads(_RULES.read_text(encoding="utf-8"))
    rules["forbidden_keys"].remove("turns")
    (mirror / "ownership-rules.json").write_text(json.dumps(rules), encoding="utf-8")
    monkeypatch.setattr(input_adapter, "_HANDOFF_ROOT", mirror)

    with pytest.raises(InputSourceError, match="digest mismatch: ownership-rules.json"):
        load_input(_KIT / "invalid" / "shape-turns-array-key.json")
