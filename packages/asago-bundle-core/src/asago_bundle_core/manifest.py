"""Read the installed ``tool-bundle`` manifest's conversation and value keys."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CONVERSATION_MODE = "conversation_mode"
VALUES_REQUIRED = "values_required"


def conversation_mode(manifest: Mapping[str, Any] | Any) -> Any:
    """Return the manifest's conversation mode, or ``None`` when absent."""

    if not isinstance(manifest, Mapping):
        return None
    return manifest.get(CONVERSATION_MODE)


def values_field() -> str:
    """Return the field name for required value keys."""

    return VALUES_REQUIRED


def values_required(manifest: Mapping[str, Any] | Any) -> list[str]:
    """Return the manifest's required value keys, or an empty list."""

    if not isinstance(manifest, Mapping):
        return []
    keys = manifest.get(VALUES_REQUIRED, [])
    return list(keys) if isinstance(keys, list) else []
