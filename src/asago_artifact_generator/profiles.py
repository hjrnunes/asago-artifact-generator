"""Fail-closed connection loading for target-free private authoring."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import yaml

REQUIRED_PROFILE_FIELDS: tuple[str, ...] = ("base_url", "api_key", "model")
# The rest of the model-profile contract's field table. The loader validates
# every one of them; it carries only the fields authoring uses.
OPTIONAL_PROFILE_FIELDS: tuple[str, ...] = (
    "context_window",
    "safety_margin",
    "max_completion_tokens",
    "temperature",
    "top_p",
    "top_k",
    "repetition_penalty",
    "seed",
    "headers",
    "enable_thinking",
    "use_guided_decoding",
    "timeout",
    "reasoning_effort",
    "service_tier",
    "service_tier_fallback",
    "sampling_controls",
    "strict_json_schema",
    "json_schema_strict",
)


class ProfileLoadError(ValueError):
    """Base class for typed, non-secret profile resolution failures."""

    code = "profile_load_error"

    def __init__(
        self,
        message: str,
        *,
        path: Path | None = None,
        profile_name: str | None = None,
        field: str | None = None,
    ) -> None:
        self.path = path
        self.profile_name = profile_name
        self.field = field
        super().__init__(message)


class ProfileFileError(ProfileLoadError):
    """The profile file could not be read or decoded."""

    code = "profile_file_error"


class ProfileNotFoundError(ProfileLoadError):
    """The requested named profile does not exist."""

    code = "profile_not_found"


class ProfileFieldError(ProfileLoadError):
    """A named profile is missing or has an invalid field."""

    code = "profile_field_error"


@dataclass(frozen=True, slots=True)
class AuthoringProfile:
    """Resolved private authoring connection held only in process memory."""

    name: str
    base_url: str = field(repr=False)
    api_key: str = field(repr=False)
    model: str = field(repr=False)
    reasoning_effort: str | None = None
    service_tier: str | None = None
    service_tier_fallback: str | None = None
    sampling_controls: bool = True
    strict_json_schema: bool | None = None
    context_window: int | None = None
    max_completion_tokens: int | None = None
    timeout: float | int | None = None
    repetition_penalty: float | int | None = None

    def __repr__(self) -> str:
        """Avoid exposing connection values in test failures or diagnostics."""

        return (
            f"AuthoringProfile(name={self.name!r}, base_url='<redacted>', "
            "api_key='<redacted>', model='<redacted>')"
        )


def load_authoring_profile(
    profiles_file: str | Path,
    profile_name: str,
) -> AuthoringProfile:
    """Load one complete named profile without logging or persisting values.

    The approved project file uses a top-level mapping of profile names.  A
    ``profiles`` mapping is also accepted for compatibility with existing
    qualification helpers. The three connection fields and the optional request
    controls authoring uses are returned. Every field of the entry must satisfy
    the ``model-profile`` contract's field table, whether authoring uses it or
    not; an unknown or invalid field raises ``ProfileFieldError`` naming it.
    """

    path = Path(profiles_file)
    entry = _profile_lookup(path, profile_name)[profile_name]
    if not isinstance(entry, dict):
        raise ProfileFieldError(
            f"profile {profile_name!r} is not a mapping",
            path=path,
            profile_name=profile_name,
        )
    _check_entry(entry, path, profile_name)
    values = {name: entry[name] for name in REQUIRED_PROFILE_FIELDS}
    optional = {
        name: entry[name] if entry.get(name) is not None else default
        for name, default in _CARRIED_DEFAULTS.items()
    }
    return AuthoringProfile(name=profile_name, **values, **optional)


def _profile_lookup(path: Path, profile_name: str) -> dict[Any, Any]:
    if not isinstance(profile_name, str) or not profile_name.strip():
        raise ProfileNotFoundError(
            "profile name must be a nonblank string",
            path=path,
            profile_name=profile_name if isinstance(profile_name, str) else None,
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        del exc
        raise ProfileFileError(
            f"could not read model profiles file: {path}",
            path=path,
            profile_name=profile_name,
        ) from None
    if not isinstance(raw, dict):
        raise ProfileFileError(
            f"model profiles file is not a mapping: {path}",
            path=path,
            profile_name=profile_name,
        )
    lookup = raw["profiles"] if "profiles" in raw else raw
    if not isinstance(lookup, dict):
        raise ProfileFileError(
            f"profiles entry is not a mapping: {path}",
            path=path,
            profile_name=profile_name,
        )
    if profile_name not in lookup:
        raise ProfileNotFoundError(
            f"profile {profile_name!r} not found in {path}",
            path=path,
            profile_name=profile_name,
        )
    return lookup


def _check_entry(entry: dict[str, Any], path: Path, profile_name: str) -> None:
    """Reject an unknown, missing or invalid field, naming it and never its value."""

    unknown = sorted(str(key) for key in entry if key not in _FIELD_RULES)
    if unknown:
        _reject(f"has unknown field {unknown[0]!r}", unknown[0], path, profile_name)
    for name in REQUIRED_PROFILE_FIELDS:
        if not _is_nonblank_string(entry.get(name)):
            _reject(f"is missing required field {name!r}", name, path, profile_name)
    for name, valid in _FIELD_RULES.items():
        value = entry.get(name)
        if value is not None and not valid(value):
            _reject(f"has invalid field {name!r}", name, path, profile_name)


def _reject(reason: str, field_name: str, path: Path, profile_name: str) -> NoReturn:
    raise ProfileFieldError(
        f"profile {profile_name!r} {reason}",
        path=path,
        profile_name=profile_name,
        field=field_name,
    )


def _is_nonblank_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_boolean(value: Any) -> bool:
    return isinstance(value, bool)


def _is_false(value: Any) -> bool:
    return value is False


def _is_headers(value: Any) -> bool:
    return isinstance(value, dict) and all(
        _is_nonblank_string(name) and isinstance(item, str) for name, item in value.items()
    )


def _integer(minimum: int) -> Callable[[Any], bool]:
    def check(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= minimum

    return check


def _number(
    *, minimum: float = 0.0, maximum: float | None = None, exclusive: bool = False
) -> Callable[[Any], bool]:
    def check(value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not math.isfinite(value):
            return False
        if value < minimum or (exclusive and value == minimum):
            return False
        return maximum is None or value <= maximum

    return check


# One validity rule per field, as in the model-profile contract's field table.
_FIELD_RULES: dict[str, Callable[[Any], bool]] = {
    "base_url": _is_nonblank_string,
    "model": _is_nonblank_string,
    "api_key": _is_nonblank_string,
    "context_window": _integer(1),
    "safety_margin": _integer(0),
    "max_completion_tokens": _integer(1),
    "temperature": _number(),
    "top_p": _number(maximum=1.0),
    "top_k": _integer(1),
    "repetition_penalty": _number(exclusive=True),
    "seed": _integer(0),
    "headers": _is_headers,
    "enable_thinking": _is_boolean,
    "use_guided_decoding": _is_boolean,
    "timeout": _number(exclusive=True),
    "reasoning_effort": _is_nonblank_string,
    "service_tier": _is_nonblank_string,
    "service_tier_fallback": _is_nonblank_string,
    "sampling_controls": _is_boolean,
    "strict_json_schema": _is_false,
    "json_schema_strict": _is_boolean,
}

# The optional fields authoring carries, with the loaded value of an absent one.
_CARRIED_DEFAULTS: dict[str, Any] = {
    "reasoning_effort": None,
    "service_tier": None,
    "service_tier_fallback": None,
    "sampling_controls": True,
    "strict_json_schema": None,
    "context_window": None,
    "max_completion_tokens": None,
    "timeout": None,
    "repetition_penalty": None,
}


__all__ = [
    "AuthoringProfile",
    "ProfileFieldError",
    "ProfileFileError",
    "ProfileLoadError",
    "ProfileNotFoundError",
    "OPTIONAL_PROFILE_FIELDS",
    "REQUIRED_PROFILE_FIELDS",
    "load_authoring_profile",
]
