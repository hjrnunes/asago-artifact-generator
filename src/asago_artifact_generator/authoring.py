"""Constrained presentation-only authoring seam.

Authoring may produce text for named content slots after deterministic
readiness.  It cannot choose execution order, surfaces, tools, values,
conditions, observers, or detector criteria.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models._base import canonical_json_bytes, freeze_value

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _require_nonempty(value: str, label: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")


def _validate_slot(slot: PresentationSlot) -> None:
    _require_nonempty(slot.slot_id, "presentation slot_id")
    _require_nonempty(slot.purpose, "presentation slot purpose")
    if slot.max_chars < 1:
        raise ValueError("presentation slot max_chars must be positive")


@dataclass(frozen=True, slots=True)
class PresentationSlot:
    """One compiler-owned text slot an author may fill."""

    slot_id: str
    purpose: str
    allowed_role: str
    max_chars: int = 2_000

    def __post_init__(self) -> None:
        _validate_slot(self)


def _validate_slots(slots: tuple[PresentationSlot, ...]) -> None:
    if any(not isinstance(slot, PresentationSlot) for slot in slots):
        raise TypeError("presentation request slots must be PresentationSlot values")


def _validate_strings(values: tuple[Any, ...], label: str) -> None:
    if any(not isinstance(value, str) for value in values):
        raise TypeError(f"{label} must be strings")


def _validate_unique_slots(slot_ids: tuple[str, ...]) -> None:
    if len(slot_ids) != len(set(slot_ids)):
        raise ValueError("presentation request slot IDs must be unique")


@dataclass(frozen=True, slots=True)
class PresentationRequest:
    """Closed author request assembled from a ready plan."""

    slots: tuple[PresentationSlot, ...]
    scenario_narrative: str = ""
    loss_context: str = ""
    allowed_tools: tuple[str, ...] = ()
    allowed_values: tuple[Any, ...] = ()
    constraints: tuple[str, ...] = (
        "Do not add, remove, or reorder execution steps.",
        "Do not invent tools, schemas, values, surfaces, observers, or success criteria.",
    )

    def __post_init__(self) -> None:
        self._normalize()
        self._validate()

    def _normalize(self) -> None:
        object.__setattr__(self, "slots", tuple(self.slots))
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        object.__setattr__(self, "allowed_values", tuple(freeze_value(self.allowed_values)))
        object.__setattr__(self, "constraints", tuple(self.constraints))

    def _validate(self) -> None:
        _validate_slots(self.slots)
        _validate_strings(self.allowed_tools, "presentation request tool names")
        _validate_strings(self.constraints, "presentation request constraints")
        _validate_unique_slots(self.slot_ids)

    @property
    def slot_ids(self) -> tuple[str, ...]:
        return tuple(slot.slot_id for slot in self.slots)


def _normalize_texts(texts: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(texts, Mapping):
        raise TypeError("presentation texts must be an object")
    normalized = dict(texts)
    if any(not isinstance(key, str) for key in normalized):
        raise TypeError("presentation text keys must be strings")
    if any(not isinstance(value, str) for value in normalized.values()):
        raise TypeError("presentation text values must be strings")
    return normalized


def _author_digest(author_digest: str, texts: Mapping[str, str]) -> str:
    if not author_digest:
        return hashlib.sha256(canonical_json_bytes(texts)).hexdigest()
    if not _DIGEST_RE.fullmatch(author_digest):
        raise ValueError("author_digest must be a SHA-256 digest")
    return author_digest


def _validate_complete_slot_set(texts: Mapping[str, str], slot_ids: tuple[str, ...]) -> None:
    expected = set(slot_ids)
    actual = set(texts)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise ValueError(f"presentation author omitted slots: {missing}")
    if extra:
        raise ValueError(f"presentation author returned unknown slots: {extra}")


def _validate_text_lengths(texts: Mapping[str, str], slots: tuple[PresentationSlot, ...]) -> None:
    limits = {slot.slot_id: slot.max_chars for slot in slots}
    too_long = sorted(slot_id for slot_id, text in texts.items() if len(text) > limits[slot_id])
    if too_long:
        raise ValueError(f"presentation text exceeds slot limit: {too_long}")


@dataclass(frozen=True, slots=True)
class PresentationResult:
    """Closed text map returned by a presentation author."""

    texts: Mapping[str, str] = field(default_factory=dict)
    author_digest: str = ""

    def __post_init__(self) -> None:
        normalized = _normalize_texts(self.texts)
        object.__setattr__(self, "texts", freeze_value(normalized))
        object.__setattr__(self, "author_digest", _author_digest(self.author_digest, normalized))

    @property
    def slot_texts(self) -> Mapping[str, str]:
        """Compatibility alias for callers that use the wire terminology."""

        return self.texts

    def validate_for(self, request: PresentationRequest) -> None:
        _validate_complete_slot_set(self.texts, request.slot_ids)
        _validate_text_lengths(self.texts, request.slots)

    @classmethod
    def from_mapping(
        cls,
        texts: Mapping[str, str],
        request: PresentationRequest,
    ) -> PresentationResult:
        result = cls(texts=texts)
        result.validate_for(request)
        return result


class PresentationAuthor(Protocol):
    """Injected optional author; implementations must not receive raw plans."""

    def author(self, request: PresentationRequest) -> PresentationResult: ...


class DeterministicPresentationAuthor:
    """Author that supplies pre-bound text without constructing an LLM client."""

    def __init__(self, texts: Mapping[str, str]) -> None:
        if not isinstance(texts, Mapping):
            raise TypeError("deterministic presentation texts must be an object")
        self._texts = freeze_value(dict(texts))

    def author(self, request: PresentationRequest) -> PresentationResult:
        return PresentationResult.from_mapping(self._texts, request)


__all__ = [
    "DeterministicPresentationAuthor",
    "PresentationAuthor",
    "PresentationRequest",
    "PresentationResult",
    "PresentationSlot",
]
