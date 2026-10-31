"""The consumer envelope, ``asago-midojo-package-v1``: an episode document and the Asago fields.

The consumer writes one file, ``episode.json``, that holds an episode document v1
(``midojo-episode-v1``, owned by MiDojo) as its ``episode`` member, plus the fields only
Asago reads: the package it came from, the claim it grades, how the turns are delivered,
where the planted items sit, and the MiDojo revisions it needs. The envelope is closed:
an unknown key is refused, and so is an unknown version, named in the error.

The wrapper this adapter wrote before (``midojo-episode-v2``) is the v0 form. MiDojo and
every Asago reader read it for one release and then stop. A v0 wrapper keeps the episode's
own fields under ``episode`` and the interception, tools and verifier list beside them; the
v0 reader moves them into the episode document. A v0 wrapper's ``verifiers`` list is the
old Garak verifier registry: a v1 document names no verifier (MiDojo's rule, because a
registry is trusted code and not data), so the conversion drops it. This adapter never ran
those verifiers; the sealed reader reads the turns, the plan and the placements only.

Version strings and the episode schema come from ``midojo_sdk.episode_schema``, so this
module follows MiDojo without copying its constants.
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from midojo_sdk.episode_schema import EPISODE_DOCUMENT_V1

from asago_bundle_core.errors import BundleError

ENVELOPE_SCHEMA = "asago-midojo-package-v1"
LEGACY_SCHEMA = "midojo-episode-v2"
EPISODE_SCHEMA = EPISODE_DOCUMENT_V1
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
# The v0 wrapper: the envelope's fields, with the episode's interception, tools and verifier
# list beside the episode instead of inside it, and the tool the wrapper named.
LEGACY_FIELDS = (ENVELOPE_FIELDS - {"schema_version"}) | {
    "schema_version",
    "tool",
    "verifiers",
    "interception",
    "tools",
}
# The v0 wrapper's ``episode`` member: the episode document v1's own data members, in the
# names MiDojo's ``Episode`` uses.
LEGACY_EPISODE_FIELDS = frozenset(
    {
        "runtime",
        "turns",
        "plan",
        "security",
        "utility",
        "environment",
        "suite_name",
        "user_task_id",
        "injection_task_id",
    }
)
# The marker a v0 wrapper's interception carried for the target's endpoint, which the runner
# supplies at run time: MiDojo reads it as no upstream.
LATE_UPSTREAM = "endpoint.mcp_url"


class EnvelopeError(BundleError):
    """The envelope cannot be read: a missing member, a wrong type, or an unknown key."""


class UnsupportedEnvelopeVersion(EnvelopeError):
    """A schema version this reader does not read. ``version`` names it, ``where`` its member."""

    def __init__(self, version: object, where: str = "envelope") -> None:
        self.version = version
        self.where = where
        if where == "episode":
            self.supported = (EPISODE_SCHEMA,)
            reason = f"supported: {EPISODE_SCHEMA}"
        else:
            self.supported = (ENVELOPE_SCHEMA, LEGACY_SCHEMA)
            reason = f"supported: {ENVELOPE_SCHEMA}, or {LEGACY_SCHEMA} for the v0 wrapper"
        super().__init__(f"{where}: schema_version {version!r} is not supported; {reason}")


@dataclass(frozen=True)
class Envelope:
    """One ``episode.json``: the version it was read as, the Asago fields, the episode document."""

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
    """Return the envelope of ``episode``: a v1 document plus the fields only Asago reads.

    ``episode`` is written as given, so this function never edits MiDojo's document.
    """

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
    """Read ``episode.json``, given as its decoded contents or as its path.

    ``asago-midojo-package-v1`` is read as v1, ``midojo-episode-v2`` as the v0 wrapper with
    a :class:`DeprecationWarning`, and any other version raises
    :class:`UnsupportedEnvelopeVersion`, which names it.
    """

    data = _object(_decode(document), "envelope")
    version = _version(data)
    if version == ENVELOPE_SCHEMA:
        _closed(data, ENVELOPE_FIELDS, "envelope")
        episode = _episode_member(data.get("episode"))
    elif version == LEGACY_SCHEMA:
        warnings.warn(
            f"the envelope version {LEGACY_SCHEMA!r} is read as v0 and is deprecated; "
            f"write {ENVELOPE_SCHEMA!r} instead",
            DeprecationWarning,
            stacklevel=2,
        )
        _closed(data, LEGACY_FIELDS, "envelope (v0)")
        episode = _v0_episode(data)
    else:
        raise UnsupportedEnvelopeVersion(version)
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


def _episode_member(value: object) -> Mapping[str, Any]:
    """Return the v1 envelope's ``episode`` member: a document v1, and nothing else."""

    episode = _object(value, "episode")
    version = _version(episode, "episode")
    if version != EPISODE_SCHEMA:
        raise UnsupportedEnvelopeVersion(version, "episode")
    _readable(episode, "episode")
    return episode


def _readable(episode: Mapping[str, Any], label: str) -> None:
    """Check what a reader needs of an episode document: a runtime object and a plan, if any.

    MiDojo's document makes ``plan`` optional, and a receipt reads an episode without one as
    unread rather than refusing it.
    """

    if not isinstance(episode.get("runtime"), Mapping):
        raise EnvelopeError(f"{label}: runtime must be a JSON object")
    plan = episode.get("plan")
    if plan is not None and not isinstance(plan, Mapping):
        raise EnvelopeError(f"{label}: plan must be a JSON object or null")
    _turns(episode.get("turns"))


def _v0_episode(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return the episode document a v0 wrapper holds, its next to the episode moved inside."""

    inner = _object(data.get("episode"), "episode (v0)")
    _closed(inner, LEGACY_EPISODE_FIELDS, "episode (v0)")
    _readable(inner, "episode (v0)")
    episode: dict[str, Any] = {
        "schema_version": EPISODE_SCHEMA,
        "suite_name": inner.get("suite_name"),
        "user_task_id": inner.get("user_task_id"),
        "injection_task_id": inner.get("injection_task_id"),
        "environment_type": None,
        "environment": inner.get("environment"),
        "runtime": inner.get("runtime"),
        "turns": inner.get("turns"),
        "plan": inner.get("plan"),
        "security": inner.get("security"),
        "utility": inner.get("utility"),
        "verifiers": [],
        "interception": _v0_interception(data.get("interception")),
        "tools": data.get("tools"),
    }
    return episode


def _v0_interception(value: object) -> dict[str, Any]:
    """Return the v0 interception settings, the late-bound endpoint marker read as no upstream."""

    if value is None:
        return {}
    settings = dict(_object(value, "interception"))
    if settings.get("upstream") == LATE_UPSTREAM:
        settings["upstream"] = None
    return settings


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
