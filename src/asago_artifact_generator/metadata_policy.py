"""Closed secret policies for authoring metadata and prompt views."""

from __future__ import annotations

from typing import Any

_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "endpoint",
    "password",
    "secret",
    "base_url",
    "baseurl",
    "token",
)
_SECRET_KEY_PREFIXES = ("auth", "session", "access", "bearer")
_PROMPT_SECRET_KEY_MARKERS = _SECRET_KEY_MARKERS + ("bearer",)
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
_USAGE_COUNTER_KEYS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }
)
_USAGE_DETAIL_KEYS = frozenset(
    {
        "prompt_tokens_details",
        "completion_tokens_details",
    }
)
_USAGE_KEYS = _USAGE_COUNTER_KEYS | _USAGE_DETAIL_KEYS


def secret_metadata_paths(value: Any, path: str = "") -> list[str]:
    """Return paths that violate the closed metadata policy.

    A ``usage`` field is the sole exception to broad token-key protection. It
    must use only the three non-negative integer counter names plus optional
    token-detail maps. A record may contain the subset reported by a provider,
    but it must contain at least one counter. The exception never applies to
    any other metadata field or to secret-bearing names inside a detail map.
    """

    return _walk(value, path)


def prompt_secret_metadata_paths(value: Any, path: str = "") -> list[str]:
    """Return secret-bearing key paths in a model-facing prompt view.

    Prompt views contain semantic structure, so ``session_path`` and related
    source-navigation vocabulary are ordinary data.  The prompt policy still
    rejects high-confidence secret names and deceptive spellings.  Package
    metadata must continue to use :func:`secret_metadata_paths`.
    """

    return _walk_prompt(value, path)


def _walk(value: Any, path: str) -> list[str]:
    if isinstance(value, dict):
        violations: list[str] = []
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if key == "usage":
                violations.extend(_validate_usage(item, child_path))
                continue
            if not isinstance(key, str) or _looks_secret_key(key):
                violations.append(child_path)
            violations.extend(_walk(item, child_path))
        return violations
    if isinstance(value, list):
        violations = []
        for index, item in enumerate(value):
            violations.extend(_walk(item, f"{path}[{index}]"))
        return violations
    return []


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


def _validate_usage(value: Any, path: str) -> list[str]:
    if isinstance(value, dict):
        if "availability" in value:
            return _validate_usage_entry(value, path)
        return _validate_usage_value(value, path)
    if not isinstance(value, list):
        return [path]
    violations: list[str] = []
    for index, entry in enumerate(value):
        violations.extend(_validate_usage_entry(entry, f"{path}[{index}]"))
    return violations


def _validate_usage_entry(value: Any, path: str) -> list[str]:
    if not isinstance(value, dict):
        return [path]
    availability = value.get("availability")
    if availability == "unavailable":
        if (
            set(value) != {"availability", "reason"}
            or not isinstance(value.get("reason"), str)
            or not value["reason"]
        ):
            return [path]
        return []
    if availability != "available" or set(value) != {"availability", "value"}:
        return [path]
    return _validate_usage_value(value["value"], f"{path}.value")


def _validate_usage_value(value: Any, path: str) -> list[str]:
    if not isinstance(value, dict):
        return [path]
    violations: list[str] = []
    if not value:
        return [path]
    unknown = [key for key in value if key not in _USAGE_KEYS]
    if unknown:
        violations.extend(f"{path}.{key}" for key in sorted(unknown, key=str))
    if not _USAGE_COUNTER_KEYS.intersection(value):
        violations.append(path)
    for key in _USAGE_COUNTER_KEYS & set(value):
        if not _is_non_negative_integer(value[key]):
            violations.append(f"{path}.{key}")
    for key in _USAGE_DETAIL_KEYS & set(value):
        violations.extend(_validate_detail_map(value[key], f"{path}.{key}"))
    return violations


def _validate_detail_map(value: Any, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        return [path]
    violations: list[str] = []
    for key, item in value.items():
        child_path = f"{path}.{key}" if path else str(key)
        if not isinstance(key, str) or not key or _looks_secret_key(key, detail_map=True):
            violations.append(child_path)
            continue
        if isinstance(item, dict):
            violations.extend(_validate_detail_map(item, child_path))
        elif not _is_non_negative_integer(item):
            violations.append(child_path)
    return violations


def _looks_secret_key(key: str, *, detail_map: bool = False) -> bool:
    lowered = key.lower().replace("-", "_")
    if lowered in _SECRET_KEY_PREFIXES or any(
        lowered.startswith(f"{prefix}_") for prefix in _SECRET_KEY_PREFIXES
    ):
        return True
    if detail_map and "token" in lowered:
        if lowered.startswith("api_"):
            return True
        lowered = lowered.replace("token", "")
    return any(marker in lowered for marker in _SECRET_KEY_MARKERS)


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


def _is_non_negative_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


__all__ = ["prompt_secret_metadata_paths", "secret_metadata_paths"]
