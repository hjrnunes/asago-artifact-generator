"""Fail-closed connection loading for target-free private authoring."""

from __future__ import annotations

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
    readers: tuple[tuple[str, Any, dict[str, Any]], ...] = (
        ("reasoning_effort", _optional_nonblank_string, {}),
        ("service_tier", _optional_nonblank_string, {}),
        ("service_tier_fallback", _optional_nonblank_string, {}),
        ("sampling_controls", _optional_boolean, {"default": True}),
        ("strict_json_schema", _optional_boolean, {"default": None}),
        ("context_window", _optional_positive_integer, {}),
        ("max_completion_tokens", _optional_positive_integer, {}),
        ("timeout", _optional_positive_number, {}),
    )
    optional = {
        name: read(entry, name, path=path, profile_name=profile_name, **extra)
        for name, read, extra in readers
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


def _optional_nonblank_string(
    entry: dict[str, Any],
    field_name: str,
    *,
    path: Path,
    profile_name: str,
) -> str | None:
    value = entry.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProfileFieldError(
            f"profile {profile_name!r} has invalid field {field_name!r}",
            path=path,
            profile_name=profile_name,
            field=field_name,
        )
    return value


def _optional_boolean(
    entry: dict[str, Any],
    field_name: str,
    *,
    default: bool | None,
    path: Path,
    profile_name: str,
) -> bool | None:
    value = entry.get(field_name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ProfileFieldError(
            f"profile {profile_name!r} has invalid field {field_name!r}",
            path=path,
            profile_name=profile_name,
            field=field_name,
        )
    return value


def _optional_positive_integer(
    entry: dict[str, Any],
    field_name: str,
    *,
    path: Path,
    profile_name: str,
) -> int | None:
    value = entry.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProfileFieldError(
            f"profile {profile_name!r} has invalid field {field_name!r}",
            path=path,
            profile_name=profile_name,
            field=field_name,
        )
    return value


def _optional_positive_number(
    entry: dict[str, Any],
    field_name: str,
    *,
    path: Path,
    profile_name: str,
) -> float | int | None:
    value = entry.get(field_name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ProfileFieldError(
            f"profile {profile_name!r} has invalid field {field_name!r}",
            path=path,
            profile_name=profile_name,
            field=field_name,
        )
    return value


__all__ = [
    "AuthoringProfile",
    "ProfileFieldError",
    "ProfileFileError",
    "ProfileLoadError",
    "ProfileNotFoundError",
    "REQUIRED_PROFILE_FIELDS",
    "load_authoring_profile",
]
