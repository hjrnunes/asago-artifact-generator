"""The binding-repair option field descriptions match their pinned text word for word.

A correction prompt sends these descriptions in its BINDING REPAIR OPTION FIELDS
section, and no recorded replay or golden prompt renders that section. Edit
``tests/fixtures/binding_repair_option_fields.json`` when you reword a template on
purpose.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring.binding_repair import (
    _BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS,
)
from asago_artifact_generator.authoring.templates import prose

_TEMPLATES = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "asago_artifact_generator"
    / "authoring"
    / "prompts"
    / "binding_repair"
    / "option_fields"
)
_PINNED = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "binding_repair_option_fields.json").read_text(
        encoding="utf-8"
    )
)


def test_every_option_field_template_is_pinned_and_described() -> None:
    names = {path.stem for path in _TEMPLATES.glob("*.j2")}

    assert names == set(_PINNED)
    assert set(_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS) == names


@pytest.mark.parametrize("name", sorted(_PINNED))
def test_each_option_field_template_renders_its_pinned_text(name: str) -> None:
    assert prose(f"binding_repair/option_fields/{name}") == _PINNED[name]
    assert _BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS[name] == _PINNED[name]
