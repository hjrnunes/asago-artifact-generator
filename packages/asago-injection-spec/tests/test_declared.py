"""``declared.py`` reads the runtime-spec slot declarations the mini-agents writer emits.

Each seed slot states the parties that may write it as a ``writable_by`` list. The
declared-target reader only needs each slot's ``id`` and ``record_key``, so it reads
the slots the writer declares.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from asago_bundle_core.target import load_target_config
from asago_injection_spec.declared import DeclarationFault, declared_tools

REPO = Path(__file__).resolve().parents[3]
TARGET = REPO / "tests" / "fixtures" / "targets" / "piggybank"


def _declared(target: Path) -> Any:
    return declared_tools(load_target_config(target, DeclarationFault))


def test_the_reader_reads_the_slots_the_writer_declares() -> None:
    tools = _declared(TARGET)

    assert tools.record_keys == {
        "piggybank.note.body": "note_id",
        "piggybank.note.read": "note_id",
    }
    assert tools.mutating == frozenset({"withdraw"})
