"""Deprecated alias for :mod:`asago_injection_spec.sealed`, kept until M9.

The sealed-result reader lives in the ``asago-injection-spec`` workspace member, where both
adapters read it without depending on each other. This module keeps the MiDojo adapter's old
import path; every name is that module's, and this module goes away at M9.
"""

from __future__ import annotations

from asago_injection_spec import sealed as _moved

__all__ = [name for name in dir(_moved) if not name.startswith("_")]


def __getattr__(name: str) -> object:
    """Return the moved module's attribute, so every old path keeps its object."""

    return getattr(_moved, name)


def __dir__() -> list[str]:
    return dir(_moved)
