"""Findings for the keys of a closed object: unexpected keys and missing required keys."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .core import Finding


def unexpected_field_findings(
    value: Mapping[Any, Any],
    allowed: Iterable[Any],
    detail: str,
    prefix: str,
    *,
    code: str = "unexpected_field",
) -> list[Finding]:
    """Return one finding per key of ``value`` outside ``allowed``, in sorted key order.

    Each finding reads ``"<detail>: <key>"`` at path ``"<prefix><key>"``.
    """

    return [
        Finding(code, f"{detail}: {name}", f"{prefix}{name}")
        for name in sorted(set(value) - set(allowed), key=str)
    ]


def missing_field_findings(
    value: Mapping[Any, Any],
    required: Iterable[str],
    detail: str,
    prefix: str,
    *,
    code: str = "missing_field",
) -> list[Finding]:
    """Return one finding per name in ``required`` absent from ``value``, in ``required`` order.

    Each finding reads ``"<detail>: <name>"`` at path ``"<prefix><name>"``.
    """

    return [
        Finding(code, f"{detail}: {name}", f"{prefix}{name}")
        for name in required
        if name not in value
    ]
