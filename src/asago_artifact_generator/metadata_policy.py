"""Closed secret policies for authoring metadata and prompt views.

The metadata policy lives in the ``asago-package`` workspace member; this module re-exports
it for the authoring code and keeps the prompt-view policy, which no package value reads.
"""

from __future__ import annotations

from typing import Any

from asago_package.metadata_policy import (
    SECRET_KEY_BASE as SECRET_KEY_BASE,
)
from asago_package.metadata_policy import (
    SECRET_KEY_MARKERS as SECRET_KEY_MARKERS,
)
from asago_package.metadata_policy import (
    secret_metadata_paths as secret_metadata_paths,
)

_PROMPT_SECRET_KEY_MARKERS = SECRET_KEY_MARKERS + ("bearer",)
_PROMPT_SECRET_KEY_PREFIXES = ("auth", "access", "bearer")
_PROMPT_STRUCTURAL_SUFFIXES = frozenset(
    {
        "handle",
        "handles",
        "id",
        "locator",
        "locators",
        "path",
        "paths",
        "ref",
        "refs",
        "selector",
        "selectors",
    }
)


def prompt_secret_metadata_paths(value: Any, path: str = "") -> list[str]:
    """Return secret-bearing key paths in a model-facing prompt view.

    Prompt views contain semantic structure, so ``session_path`` and related
    source-navigation vocabulary are ordinary data.  The prompt policy still
    rejects high-confidence secret names and deceptive spellings.  Package
    metadata must continue to use :func:`secret_metadata_paths`.
    """

    return _walk_prompt(value, path)


def _walk_prompt(value: Any, path: str) -> list[str]:
    if isinstance(value, dict):
        violations: list[str] = []
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if not isinstance(key, str) or _looks_prompt_secret_key(key):
                violations.append(child_path)
            violations.extend(_walk_prompt(item, child_path))
        return violations
    if isinstance(value, list):
        violations = []
        for index, item in enumerate(value):
            violations.extend(_walk_prompt(item, f"{path}[{index}]"))
        return violations
    return []


def _looks_prompt_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    if normalized == "session" or normalized.startswith("session"):
        suffix = normalized.removeprefix("session_")
        if normalized.startswith("session_") and suffix in _PROMPT_STRUCTURAL_SUFFIXES:
            return False
        return True
    if normalized in _PROMPT_SECRET_KEY_PREFIXES or any(
        normalized.startswith(f"{prefix}_") for prefix in _PROMPT_SECRET_KEY_PREFIXES
    ):
        return True
    return any(marker in normalized for marker in _PROMPT_SECRET_KEY_MARKERS)


__all__ = [
    "SECRET_KEY_BASE",
    "SECRET_KEY_MARKERS",
    "prompt_secret_metadata_paths",
    "secret_metadata_paths",
]
