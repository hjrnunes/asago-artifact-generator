"""Fail-closed connection loading for target-free private authoring."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REQUIRED_PROFILE_FIELDS: tuple[str, ...] = ("base_url", "api_key", "model")
ENVIRONMENT_API_KEY_NAMES: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "HF_TOKEN",
    "OPENROUTER_API_KEY",
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


class EnvironmentConfigurationError(ProfileLoadError):
    """Environment-only authoring configuration is incomplete."""

    code = "environment_configuration_error"


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
    if "profiles" in raw:
        lookup = raw["profiles"]
        if not isinstance(lookup, dict):
            raise ProfileFileError(
                f"profiles entry is not a mapping: {path}",
                path=path,
                profile_name=profile_name,
            )
    else:
        lookup = raw
    if profile_name not in lookup:
        raise ProfileNotFoundError(
            f"profile {profile_name!r} not found in {path}",
            path=path,
            profile_name=profile_name,
        )
    entry = lookup[profile_name]
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
    optional = {
        "reasoning_effort": _optional_nonblank_string(
            entry, "reasoning_effort", path=path, profile_name=profile_name
        ),
        "service_tier": _optional_nonblank_string(
            entry, "service_tier", path=path, profile_name=profile_name
        ),
        "service_tier_fallback": _optional_nonblank_string(
            entry, "service_tier_fallback", path=path, profile_name=profile_name
        ),
        "sampling_controls": _optional_boolean(
            entry,
            "sampling_controls",
            default=True,
            path=path,
            profile_name=profile_name,
        ),
        "strict_json_schema": _optional_boolean(
            entry,
            "strict_json_schema",
            default=None,
            path=path,
            profile_name=profile_name,
        ),
        "context_window": _optional_positive_integer(
            entry, "context_window", path=path, profile_name=profile_name
        ),
        "max_completion_tokens": _optional_positive_integer(
            entry, "max_completion_tokens", path=path, profile_name=profile_name
        ),
        "timeout": _optional_positive_number(
            entry, "timeout", path=path, profile_name=profile_name
        ),
    }
    return AuthoringProfile(name=profile_name, **values, **optional)


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


def authoring_profile_from_environment(
    *,
    base_url: Any,
    model: Any,
) -> AuthoringProfile:
    """Resolve legacy environment-only settings without a placeholder key."""

    if not isinstance(base_url, str) or not base_url.strip():
        raise EnvironmentConfigurationError(
            "environment authoring configuration is missing base_url",
            field="base_url",
        )
    if not isinstance(model, str) or not model.strip():
        raise EnvironmentConfigurationError(
            "environment authoring configuration is missing model",
            field="model",
        )
    api_key = next(
        (
            value
            for name in ENVIRONMENT_API_KEY_NAMES
            if isinstance(value := os.environ.get(name), str) and value.strip()
        ),
        None,
    )
    if api_key is None:
        raise EnvironmentConfigurationError(
            "environment authoring configuration is missing an API key",
            field="api_key",
        )
    return AuthoringProfile(
        name="environment",
        base_url=base_url,
        api_key=api_key,
        model=model,
    )


__all__ = [
    "AuthoringProfile",
    "ENVIRONMENT_API_KEY_NAMES",
    "EnvironmentConfigurationError",
    "ProfileFieldError",
    "ProfileFileError",
    "ProfileLoadError",
    "ProfileNotFoundError",
    "REQUIRED_PROFILE_FIELDS",
    "authoring_profile_from_environment",
    "load_authoring_profile",
]
