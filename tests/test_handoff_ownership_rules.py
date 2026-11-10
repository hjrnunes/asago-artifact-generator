"""The reader enforces the producer's ownership rules from the installed kit.

``scenario-handoff/ownership-rules.json`` is the producer's file, which the shared core
ships byte for byte in its package data and pins by the installed ``CONTRACT.lock``; the
reader keeps no list of its own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from asago_contracts_core import CONTRACT_DIR

from asago_artifact_generator import input_adapter
from asago_artifact_generator.input_adapter import InputSourceError, load_input

from .support import KIT

_CONTRACT = CONTRACT_DIR
_RULES = _CONTRACT / "ownership-rules.json"
_KIT = KIT


def test_the_reader_uses_the_mirrored_rules_in_file_order() -> None:
    rules = json.loads(_RULES.read_text(encoding="utf-8"))

    contract = input_adapter._handoff_contract()

    assert contract.forbidden_keys == frozenset(rules["forbidden_keys"])
    assert [(code, pattern.pattern) for code, pattern in contract.forbidden_patterns] == [
        (entry["code"], entry["pattern"]) for entry in rules["forbidden_value_patterns"]
    ]


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
