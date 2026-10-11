"""The prompt template loader renders each .j2 file to the exact text the model receives."""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import UndefinedError

import asago_artifact_generator.authoring as authoring
from asago_artifact_generator.authoring.templates import _environment, prose, prose_lines

PROMPTS = Path(authoring.__file__).resolve().parent / "prompts"
FILES = sorted(PROMPTS.rglob("*.j2"))


def test_the_prompts_directory_holds_only_templates() -> None:
    assert FILES
    assert {path for path in PROMPTS.rglob("*") if path.is_file()} == set(FILES)


@pytest.mark.parametrize("path", FILES, ids=lambda path: str(path.relative_to(PROMPTS)))
def test_a_template_file_ends_in_one_newline_and_no_line_ends_in_whitespace(path: Path) -> None:
    source = path.read_text(encoding="utf-8")

    assert source.endswith("\n")
    assert all(line == line.rstrip() for line in source.split("\n"))


def test_rendering_drops_the_final_newline_of_the_file() -> None:
    assert _environment().from_string("one paragraph\n").render() == "one paragraph"


def test_a_text_that_ends_in_a_newline_needs_a_second_one_in_its_file() -> None:
    assert _environment().from_string("one paragraph\n\n").render() == "one paragraph\n"


def test_a_raw_block_keeps_a_literal_slot() -> None:
    template = "a {% raw %}{{binding_name}}{% endraw %} slot\n"

    assert _environment().from_string(template).render() == "a {{binding_name}} slot"


def test_a_missing_value_raises_instead_of_rendering_empty() -> None:
    with pytest.raises(UndefinedError):
        _environment().from_string("value {{ missing }}\n").render()


def test_values_render_without_escaping() -> None:
    rendered = _environment().from_string("{{ text }}\n").render(text="<a> & 'b'")

    assert rendered == "<a> & 'b'"


def test_prose_renders_a_named_file() -> None:
    assert prose("review/plan_review_binding_rules") == (
        "Apply BINDING AND SETUP RULES when interpreting runtime_bindings, setup_recipe, "
        "and setup_permissions."
    )


def test_prose_lines_returns_one_entry_per_line() -> None:
    assert prose_lines("review/plan_review_binding_rules") == (
        prose("review/plan_review_binding_rules"),
    )
