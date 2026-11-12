"""Read the consumer's ``asago-midojo-package-v1`` envelope.

``injection-spec.json`` holds a MiDojo ``midojo-injection-spec-v1`` document under
``episode`` and the package identity, claim, delivery, placements, and tool revision
range beside it. Both version checks name unsupported versions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from midojo_spec.injection_spec_schema import INJECTION_SPEC_DOCUMENT_V1

from asago_bundle_core.errors import BundleError

ENVELOPE_SCHEMA = "asago-midojo-package-v1"
INJECTION_SPEC_SCHEMA = INJECTION_SPEC_DOCUMENT_V1
SINGLE = "single"
SEQUENTIAL = "sequential"
DELIVERIES = (SINGLE, SEQUENTIAL)
ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "package_id",
        "scenario_id",
        "package_digest",
        "claim_level",
        "delivery",
        "placements",
        "tool_revision_range",
        "episode",
    }
)


class EnvelopeError(BundleError):
    """The envelope cannot be read: a missing member, a wrong type, or an unknown key."""


class UnsupportedEnvelopeVersion(EnvelopeError):
    """A schema version this reader does not read. ``version`` names it, ``where`` its member."""

    def __init__(self, version: object, where: str = "envelope") -> None:
        self.version = version
        self.where = where
        self.supported = (INJECTION_SPEC_SCHEMA,) if where == "episode" else (ENVELOPE_SCHEMA,)
        super().__init__(
            f"{where}: schema_version {version!r} is not supported; supported: {self.supported[0]}"
        )


@dataclass(frozen=True)
class Envelope:
    """One compiled document: the version it was read as, the Asago fields, the spec."""

    source_version: str
    package_id: str
    scenario_id: str
    package_digest: str
    claim_level: str
    delivery: str
    placements: tuple[Mapping[str, Any], ...]
    tool_revision_range: str
    episode: Mapping[str, Any]


def build_envelope(
    episode: Mapping[str, Any],
    *,
    package_id: str,
    scenario_id: str,
    package_digest: str,
    claim_level: str,
    delivery: str,
    placements: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    tool_revision_range: str,
) -> dict[str, Any]:
    """Wrap the MiDojo document under ``episode`` without changing its contents."""

    return {
        "schema_version": ENVELOPE_SCHEMA,
        "package_id": package_id,
        "scenario_id": scenario_id,
        "package_digest": package_digest,
        "claim_level": claim_level,
        "delivery": delivery,
        "placements": [dict(entry) for entry in placements],
        "tool_revision_range": tool_revision_range,
        "episode": dict(episode),
    }


def read_envelope(document: Mapping[str, Any] | str | Path) -> Envelope:
    """Read the supported envelope from a mapping or ``injection-spec.json`` path.

    Unsupported envelope and member versions raise :class:`UnsupportedEnvelopeVersion`.
    """

    data = _object(_decode(document), "envelope")
    version = _version(data)
    if version != ENVELOPE_SCHEMA:
        raise UnsupportedEnvelopeVersion(version)
    _closed(data, ENVELOPE_FIELDS, "envelope")
    episode = _injection_spec_member(data.get("episode"))
    delivery = _text(data.get("delivery"), "delivery")
    if delivery not in DELIVERIES:
        raise EnvelopeError(f"delivery is one of {list(DELIVERIES)}, not {delivery!r}")
    return Envelope(
        source_version=version,
        package_id=_text(data.get("package_id"), "package_id"),
        scenario_id=_text(data.get("scenario_id"), "scenario_id"),
        package_digest=_text(data.get("package_digest"), "package_digest"),
        claim_level=_text(data.get("claim_level"), "claim_level"),
        delivery=delivery,
        placements=_placements(data.get("placements")),
        tool_revision_range=_text(data.get("tool_revision_range"), "tool_revision_range"),
        episode=episode,
    )


def _injection_spec_member(value: object) -> Mapping[str, Any]:
    """Check and return the envelope's MiDojo document."""

    episode = _object(value, "episode")
    version = _version(episode, "episode")
    if version != INJECTION_SPEC_SCHEMA:
        raise UnsupportedEnvelopeVersion(version, "episode")
    _readable(episode, "episode")
    return episode


def _readable(episode: Mapping[str, Any], label: str) -> None:
    """Check what a reader needs of an injection-spec document: a runtime object, a plan, if any.

    MiDojo's document makes ``plan`` optional, and a receipt reads a spec without one as
    unread rather than refusing it.
    """

    if not isinstance(episode.get("runtime"), Mapping):
        raise EnvelopeError(f"{label}: runtime must be a JSON object")
    plan = episode.get("plan")
    if plan is not None and not isinstance(plan, Mapping):
        raise EnvelopeError(f"{label}: plan must be a JSON object or null")
    _turns(episode.get("turns"))


def _decode(document: Mapping[str, Any] | str | Path) -> Mapping[str, Any]:
    if isinstance(document, Mapping):
        return document
    path = Path(document)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EnvelopeError(f"{path}: cannot be read: {exc}") from exc
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"{path}: not JSON: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise EnvelopeError(f"{path}: envelope must be a JSON object")
    return loaded


def _version(data: Mapping[str, Any], where: str = "envelope") -> str:
    if "schema_version" not in data:
        raise EnvelopeError(f"{where}: schema_version is required")
    version = data["schema_version"]
    if type(version) is not str:
        raise EnvelopeError(
            f"{where}: schema_version must be a string, not {type(version).__name__}"
        )
    return version


def _object(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EnvelopeError(f"{where} must be a JSON object, not {type(value).__name__}")
    return value


def _closed(data: Mapping[str, object], allowed: frozenset[str], where: str) -> None:
    if unknown := [key for key in data if key not in allowed]:
        raise EnvelopeError(f"{where}: unknown key {unknown[0]!r}; the document is closed")


def _text(value: object, where: str) -> str:
    if type(value) is not str or not value:
        raise EnvelopeError(f"{where} must be a non-empty string")
    return value


def _turns(value: object) -> list[Any]:
    if type(value) is not list or not all(type(turn) is str and turn for turn in value):
        raise EnvelopeError("turns must be an array of non-empty strings")
    return value


def _placements(value: object) -> tuple[Mapping[str, Any], ...]:
    if type(value) is not list:
        raise EnvelopeError("placements must be an array")
    return tuple(_object(entry, "placements entry") for entry in value)
