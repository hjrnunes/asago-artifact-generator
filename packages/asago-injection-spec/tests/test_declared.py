"""``declared.py`` reads both runtime-spec slot forms the mini-agents writer emits.

The writer stamps ``seed_slots[].writable_by`` as a list from v3 on and wrote the scalar
``controller`` before it. The declared-target reader only needs each slot's ``id`` and
``record_key``, so it reads either form of the same declaration.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from asago_bundle_core.target import load_target_config
from asago_injection_spec.declared import DeclarationFault, declared_tools

REPO = Path(__file__).resolve().parents[3]
TARGET = REPO / "tests" / "fixtures" / "targets" / "piggybank"


def _declared(target: Path) -> Any:
    return declared_tools(load_target_config(target, DeclarationFault))


def _with_writable_by(tmp_path: Path, parties: list[str]) -> Path:
    """Return a copy of the fixture whose slots state ``writable_by`` instead of ``controller``."""

    target = tmp_path / "target"
    shutil.copytree(TARGET, target)
    spec_path = target / "runtime-spec.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    for slot in spec["seed_slots"]:
        slot.pop("controller", None)
        slot["writable_by"] = parties
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return target


def test_the_reader_reads_the_v2_slots_scalar_controller_form() -> None:
    tools = _declared(TARGET)

    assert tools.record_keys == {
        "piggybank.note.body": "note_id",
        "piggybank.note.read": "note_id",
    }
    assert tools.mutating == frozenset({"withdraw"})


def test_the_reader_reads_the_v3_slots_writable_by_list_form(tmp_path: Path) -> None:
    target = _with_writable_by(tmp_path, ["third_party", "counterparty"])

    tools = _declared(target)

    assert tools.record_keys == _declared(TARGET).record_keys
    assert tools.mutating == frozenset({"withdraw"})
