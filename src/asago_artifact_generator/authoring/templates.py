"""Fixed prompt prose, kept as Jinja templates under ``authoring/prompts``.

Each ``.j2`` file holds one text exactly as the model receives it, one line per
paragraph. Jinja drops the file's single final newline, and ``{% raw %}`` keeps a
literal ``{{binding_name}}`` slot as written. Assembly of the user message stays in
Python; a template supplies only the prose.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined


@cache
def _environment() -> Environment:
    # Built on first use: PackageLoader requires the prompts directory to exist.
    return Environment(
        loader=PackageLoader("asago_artifact_generator.authoring", "prompts"),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=False,
    )


def prose(name: str, **values: Any) -> str:
    """Return ``prompts/<name>.j2`` rendered with ``values``."""

    return _environment().get_template(f"{name}.j2").render(**values)


def prose_lines(name: str) -> tuple[str, ...]:
    """Return the lines of ``prompts/<name>.j2``: one entry per line of the file."""

    return tuple(prose(name).split("\n"))
