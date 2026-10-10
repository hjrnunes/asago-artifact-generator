"""Fail-closed connection loading for target-free private authoring."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REQUIRED_PROFILE_FIELDS: tuple[str, ...] = ("base_url", "api_key", "model")


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
    qualification helpers. The three connection fields and recognized optional
    request controls are returned. Unknown profile fields remain ignored for
    compatibility with the producer's shared profile file.
    """

    path = Path(profiles_file)
    entry = _profile_lookup(path, profile_name)[profile_name]
    if not isinstance(entry, dict):
        raise ProfileFieldError(
            f"profile {profile_name!r} is not a mapping",
            path=path,
            profile_name=profile_name,
        )
    values: dict[str, Any] = {}
    for field_name in REQUIRED_PROFILE_FIELDS:
        value = entry.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ProfileFieldError(
                f"profile {profile_name!r} is missing required field {field_name!r}",
                path=path,
                profile_name=profile_name,
                field=field_name,
            )
        values[field_name] = value
    optional_fields: tuple[tuple[str, Callable[[Any], bool], bool | None], ...] = (
        ("reasoning_effort", _is_nonblank_string, None),
        ("service_tier", _is_nonblank_string, None),
        ("service_tier_fallback", _is_nonblank_string, None),
        ("sampling_controls", _is_boolean, True),
        ("strict_json_schema", _is_boolean, None),
        ("context_window", _is_positive_integer, None),
        ("max_completion_tokens", _is_positive_integer, None),
        ("timeout", _is_positive_number, None),
        ("repetition_penalty", _is_positive_finite_number, None),
    )
    optional = {
        name: _optional(entry, name, valid, default, path=path, profile_name=profile_name)
        for name, valid, default in optional_fields
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


def _optional(
    entry: dict[str, Any],
    field_name: str,
    valid: Callable[[Any], bool],
    default: bool | None,
    *,
    path: Path,
    profile_name: str,
) -> Any:
    value = entry.get(field_name)
    if value is None:
        return default
    if not valid(value):
        raise ProfileFieldError(
            f"profile {profile_name!r} has invalid field {field_name!r}",
            path=path,
            profile_name=profile_name,
            field=field_name,
        )
    return value


def _is_nonblank_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_boolean(value: Any) -> bool:
    return isinstance(value, bool)


def _is_positive_integer(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _is_positive_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0


def _is_positive_finite_number(value: Any) -> bool:
    return _is_positive_number(value) and math.isfinite(value)


__all__ = [
    "AuthoringProfile",
    "ProfileFieldError",
    "ProfileFileError",
    "ProfileLoadError",
    "ProfileNotFoundError",
    "REQUIRED_PROFILE_FIELDS",
    "load_authoring_profile",
]
