"""Fixed-phrase tables for the authoring prompt templates.

Each template has one YAML file in ``tests/phrases/``. A file names rendered cases, and each
case lists, per prompt part (``system`` or ``user``), the phrases that part must carry:

* ``required``: each phrase appears at least once.
* ``once``: each phrase appears exactly once.
* ``forbidden``: no phrase appears.
* ``ordered``: each list of phrases appears in that order, by first occurrence.

A phrase belongs here when it is fixed text that does not depend on the input. A phrase that
echoes the input, such as a statement or an identifier, stays an assertion in the test that
builds that input.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from asago_artifact_generator.authoring.core import PromptPacket
from asago_artifact_generator.input_adapter import load_input

from .prompt_support import STAGES, render_stage_packets
from .support import (
    NO_CONDITION_HANDOFF,
    NOT_CALLED_HANDOFF,
    OBSERVED_HANDOFF,
    signed_omission_view,
)

PHRASES_DIR = Path(__file__).resolve().parent / "phrases"
PARTS = ("system", "user")
KINDS = ("required", "once", "forbidden", "ordered")


def _from_handoff(path: Path) -> Callable[[Path], Any]:
    return lambda _scratch: load_input(path)


# Each case renders the six stages from the ``ehr`` world with another scenario handoff.
CASES: dict[str, Callable[[Path], Any]] = {
    "ehr": lambda _scratch: None,
    "observed-condition": _from_handoff(OBSERVED_HANDOFF),
    "not-called": _from_handoff(NOT_CALLED_HANDOFF),
    "no-condition": _from_handoff(NO_CONDITION_HANDOFF),
    "omission": signed_omission_view,
}


def table_path(template: str) -> Path:
    return PHRASES_DIR / f"{template}.yaml"


def load_table(template: str) -> dict[str, dict[str, dict[str, list]]]:
    """Return ``{case: {part: {kind: phrases}}}`` for ``template``."""

    document = yaml.safe_load(table_path(template).read_text(encoding="utf-8"))
    return document["cases"]


def entries() -> list[tuple[str, str, str, str]]:
    """Return one ``(template, case, part, kind)`` row per list in every table."""

    rows = []
    for template in STAGES:
        for case, parts in load_table(template).items():
            for part, kinds in parts.items():
                rows.extend((template, case, part, kind) for kind in kinds)
    return rows


@cache
def _render(case: str, scratch: Path) -> dict[str, PromptPacket]:
    return render_stage_packets("ehr", view=CASES[case](scratch))


def rendered_part(template: str, case: str, part: str, scratch: Path) -> str:
    return getattr(_render(case, scratch)[template], part)


def violations(kind: str, phrases: list, text: str) -> list[str]:
    """Return one message per phrase that breaks the ``kind`` rule on ``text``."""

    if kind == "required":
        return [f"missing: {phrase!r}" for phrase in phrases if phrase not in text]
    if kind == "once":
        return [
            f"appears {text.count(phrase)} times, expected once: {phrase!r}"
            for phrase in phrases
            if text.count(phrase) != 1
        ]
    if kind == "forbidden":
        return [f"present but forbidden: {phrase!r}" for phrase in phrases if phrase in text]
    if kind == "ordered":
        found = []
        for sequence in phrases:
            positions = [text.find(phrase) for phrase in sequence]
            if -1 in positions:
                found += [
                    f"missing from the ordered list: {phrase!r}"
                    for phrase, at in zip(sequence, positions, strict=True)
                    if at == -1
                ]
            elif positions != sorted(positions):
                found.append(f"out of order: {sequence!r}")
        return found
    raise KeyError(kind)
