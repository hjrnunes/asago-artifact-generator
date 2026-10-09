"""Slot markers in bundle templates.

A template is plain JSON in which a one-key object marks a hole:
``{"$value": "<key>"}`` takes ``values.json[<key>]`` (orch knows the value),
and ``{"$bundle": "<relative path>"}`` takes that path inside the concrete
bundle (only ``instantiate`` knows where the bundle lands).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

VALUE = "$value"
BUNDLE = "$bundle"
MARKERS = (VALUE, BUNDLE)


def value_slot(key: str) -> dict[str, str]:
    return {VALUE: key}


def bundle_slot(relative: str) -> dict[str, str]:
    return {BUNDLE: relative}


def marker(node: Any) -> tuple[str, Any] | None:
    """Return ``(marker, argument)`` when ``node`` is a slot, else None."""

    if isinstance(node, dict) and len(node) == 1:
        ((key, argument),) = node.items()
        if key in MARKERS:
            return key, argument
    return None


def contains_marker(node: Any) -> bool:
    if marker(node) is not None:
        return True
    if isinstance(node, dict):
        return any(contains_marker(child) for child in node.values())
    if isinstance(node, list):
        return any(contains_marker(child) for child in node)
    return False


def template_slots(templates: Iterable[Any]) -> set[str]:
    """Return every ``$value`` key the templates need."""

    found: set[str] = set()

    def visit(kind: str, argument: Any) -> Any:
        if kind == VALUE:
            found.add(argument)
        return None

    for template in templates:
        fill(template, visit)
    return found


def fill(node: Any, resolve: Callable[[str, Any], Any]) -> Any:
    """Return ``node`` with every slot replaced by ``resolve(marker, argument)``."""

    slot = marker(node)
    if slot is not None:
        return resolve(*slot)
    if isinstance(node, dict):
        return {key: fill(child, resolve) for key, child in node.items()}
    if isinstance(node, list):
        return [fill(child, resolve) for child in node]
    return node
