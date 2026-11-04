"""The two names ``tool-bundle`` versions give the same manifest facts.

``tool-bundle-v2`` renamed ``delivery`` to ``conversation_mode`` and the top-level
``requires`` to ``values_required``. Every reader accepts both versions, so it reads the new
name first and falls back to the old one; every writer states the new name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

CONVERSATION_MODE = "conversation_mode"
LEGACY_DELIVERY = "delivery"
VALUES_REQUIRED = "values_required"
LEGACY_REQUIRES = "requires"


def conversation_mode(manifest: Mapping[str, Any] | Any) -> Any:
    """Return the mode a v1 or v2 manifest states, or ``None`` when it states neither."""

    if not isinstance(manifest, Mapping):
        return None
    return manifest.get(CONVERSATION_MODE, manifest.get(LEGACY_DELIVERY))


def values_field(manifest: Mapping[str, Any] | Any) -> str:
    """Return the field name a manifest states its value keys under."""

    if isinstance(manifest, Mapping) and VALUES_REQUIRED in manifest:
        return VALUES_REQUIRED
    return LEGACY_REQUIRES


def values_required(manifest: Mapping[str, Any] | Any) -> list[str]:
    """Return the value keys a v1 or v2 manifest states, or an empty list."""

    if not isinstance(manifest, Mapping):
        return []
    keys = manifest.get(values_field(manifest), [])
    return list(keys) if isinstance(keys, list) else []
