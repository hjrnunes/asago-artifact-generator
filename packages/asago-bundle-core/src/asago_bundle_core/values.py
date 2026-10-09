"""The values orch supplies at instantiate time: checks and entrypoint rendering."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any


def check_values(
    requires: list[str],
    values: Any,
    check_for: Callable[[str], Callable[[Any], bool] | None],
    error: type[Exception],
) -> None:
    """Refuse values that lack a required key or carry a malformed one.

    ``check_for`` is the adapter's table: it returns the check of a key, or None for a key
    that needs none. ``error`` is the adapter's exception class.
    """

    if not isinstance(values, dict):
        raise error("values must be a JSON object")
    for key in requires:
        if key not in values:
            raise error(f"values lack {key}")
        check = check_for(key)
        if check is not None and not check(values[key]):
            raise error(f"values carry a malformed {key}")


def render_entrypoint(
    entrypoint: list[str], out: Path, values: dict[str, Any], keys: Iterable[str]
) -> list[str]:
    """Render ``{bundle}`` and ``{<key>}`` for each key; ``{tool_python}`` stays for orch."""

    rendered = []
    for arg in entrypoint:
        arg = arg.replace("{bundle}", str(out))
        for key in keys:
            arg = arg.replace("{" + key + "}", str(values[key]))
        rendered.append(arg)
    return rendered
