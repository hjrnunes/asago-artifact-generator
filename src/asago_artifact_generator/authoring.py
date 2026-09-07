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

from .models._base import canonical_json_bytes, compute_framed_digest, freeze_value

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
RUNTIME_CONTEXT_SCHEMA_VERSION = "runtime-observation-context-v1"
AUTHOR_CONTEXT_SCHEMA_VERSION = "presentation-author-context-v1"


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


def _validate_tool_definitions(
    definitions: tuple[Mapping[str, Any], ...],
) -> None:
    """Validate the observed tool declarations exposed to an author.

    Tool names and schemas are runtime facts.  Keeping the complete wire
    declaration in the request prevents an author implementation from having
    to reconstruct (or silently generalise) the target interface.
    """

    names: list[str] = []
    for index, definition in enumerate(definitions):
        if not isinstance(definition, Mapping):
            raise TypeError(f"presentation tool definition {index} must be an object")
        function = definition.get("function")
        if not isinstance(function, Mapping):
            raise ValueError(f"presentation tool definition {index} must have a function")
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"presentation tool definition {index} must have a name")
        parameters = function.get("parameters")
        if not isinstance(parameters, Mapping):
            raise ValueError(f"presentation tool definition {index} parameters must be an object")
        description = function.get("description")
        if description is not None and not isinstance(description, str):
            raise TypeError(f"presentation tool definition {index} description must be a string")
        names.append(name)
    if len(names) != len(set(names)):
        raise ValueError("presentation tool definition names must be unique")


def _normalize_context(value: Mapping[str, Any] | None, label: str) -> Mapping[str, Any]:
    """Validate and freeze one caller-owned JSON observation/context object."""

    if value is None:
        return freeze_value({})
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    # Validate JSON compatibility before ``freeze_value`` stringifies mapping
    # keys.  Runtime observations are wire evidence, not a place to coerce
    # arbitrary Python values or invent a representation.
    canonical_json_bytes(value)
    return freeze_value(dict(value))


def _normalize_text_tuple(values: tuple[Any, ...], label: str) -> tuple[str, ...]:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise TypeError(f"{label} must contain non-empty strings")
    return tuple(value.strip() for value in values)


@dataclass(frozen=True, slots=True)
class AuthorContext:
    """Small semantic view exposed to a presentation author.

    The ready plan remains the authority for execution.  This view carries only
    the meaning needed to phrase a causal stimulus and the observer criterion;
    structural IDs, digests, and the raw observation document stay outside the
    provider prompt in ``provenance`` and ``PresentationRequest.runtime_context``.
    """

    action: str
    deviation: str
    causal_hypothesis: tuple[str, ...] = ()
    observable_criterion: str = ""
    loss_context: str = ""
    runtime_facts: Mapping[str, Any] = field(default_factory=dict)
    unresolved_facts: tuple[str, ...] = ()
    observation_limitations: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)
    source_constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label in ("action", "deviation", "observable_criterion"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"author context {label} must be non-empty")
            object.__setattr__(self, label, value.strip())
        for label in ("loss_context",):
            value = getattr(self, label)
            if not isinstance(value, str):
                raise TypeError(f"author context {label} must be a string")
            object.__setattr__(self, label, value.strip())
        object.__setattr__(
            self,
            "causal_hypothesis",
            _normalize_text_tuple(tuple(self.causal_hypothesis), "causal hypothesis"),
        )
        object.__setattr__(
            self,
            "source_constraints",
            _normalize_text_tuple(tuple(self.source_constraints), "source constraints"),
        )
        object.__setattr__(
            self,
            "unresolved_facts",
            _normalize_text_tuple(tuple(self.unresolved_facts), "unresolved facts"),
        )
        object.__setattr__(
            self,
            "observation_limitations",
            _normalize_text_tuple(tuple(self.observation_limitations), "observation limitations"),
        )
        object.__setattr__(
            self,
            "runtime_facts",
            _normalize_context(self.runtime_facts, "author context runtime facts"),
        )
        object.__setattr__(
            self,
            "provenance",
            _normalize_context(self.provenance, "author context provenance"),
        )

    def prompt_mapping(self) -> Mapping[str, Any]:
        """Return the compact, non-bookkeeping view sent to an author."""

        mapping: dict[str, Any] = {
            "action": self.action,
            "deviation": self.deviation,
            "causal_hypothesis": list(self.causal_hypothesis),
            "observable_criterion": self.observable_criterion,
            "source_constraints": [
                {
                    "description": description,
                    "label": "source constraint (not verified runtime policy)",
                }
                for description in self.source_constraints
            ],
            "runtime_facts": dict(self.runtime_facts),
            "unresolved_facts": list(self.unresolved_facts),
            "observation_limitations": list(self.observation_limitations),
        }
        if self.loss_context:
            mapping["loss_context"] = self.loss_context
        return freeze_value(mapping)

    def evidence_mapping(self) -> Mapping[str, Any]:
        """Return prompt-view digest inputs plus provenance for output evidence."""

        prompt = self.prompt_mapping()
        return freeze_value(
            {
                "schema_version": AUTHOR_CONTEXT_SCHEMA_VERSION,
                "digest": compute_framed_digest(AUTHOR_CONTEXT_SCHEMA_VERSION, prompt),
                "provenance": dict(self.provenance),
            }
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AuthorContext:
        if not isinstance(value, Mapping):
            raise TypeError("author context must be an object")
        causal = value.get("causal_hypothesis", ())
        source_constraints = value.get("source_constraints", ())
        unresolved = value.get("unresolved_facts", ())
        limitations = value.get("observation_limitations", ())
        for label, collection in (
            ("causal_hypothesis", causal),
            ("source_constraints", source_constraints),
            ("unresolved_facts", unresolved),
            ("observation_limitations", limitations),
        ):
            if not isinstance(collection, (list, tuple)):
                raise TypeError(f"author context {label} must be an array")
        return cls(
            action=value.get("action", ""),
            deviation=value.get("deviation", ""),
            causal_hypothesis=tuple(causal),
            observable_criterion=value.get("observable_criterion", ""),
            loss_context=value.get("loss_context", ""),
            source_constraints=tuple(
                item.get("description", "") if isinstance(item, Mapping) else item
                for item in source_constraints
            ),
            runtime_facts=value.get("runtime_facts", {}),
            unresolved_facts=tuple(unresolved),
            observation_limitations=tuple(limitations),
            provenance=value.get("provenance", {}),
        )


@dataclass(frozen=True, slots=True)
class PresentationRequest:
    """Closed author request assembled from a ready plan."""

    slots: tuple[PresentationSlot, ...]
    scenario_narrative: str = ""
    loss_context: str = ""
    allowed_tools: tuple[str, ...] = ()
    allowed_tool_definitions: tuple[Mapping[str, Any], ...] = ()
    allowed_values: tuple[Any, ...] = ()
    constraints: tuple[str, ...] = (
        "Do not add, remove, or reorder execution steps.",
        "Do not invent tools, schemas, values, surfaces, observers, or success criteria.",
    )
    runtime_context: Mapping[str, Any] | None = None
    runtime_context_provenance: Mapping[str, Any] | None = None
    execution_plan_steps: tuple[Mapping[str, Any], ...] = ()
    author_context: AuthorContext | None = None
    runtime_context_digest: str | None = field(init=False, default=None)
    author_context_digest: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._normalize()
        self._validate()

    def _normalize(self) -> None:
        object.__setattr__(self, "slots", tuple(self.slots))
        if self.author_context is not None and not isinstance(self.author_context, AuthorContext):
            if isinstance(self.author_context, Mapping):
                object.__setattr__(
                    self, "author_context", AuthorContext.from_mapping(self.author_context)
                )
            else:
                raise TypeError("author_context must be an AuthorContext value")
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        object.__setattr__(
            self,
            "allowed_tool_definitions",
            tuple(freeze_value(dict(item)) for item in self.allowed_tool_definitions),
        )
        object.__setattr__(self, "allowed_values", tuple(freeze_value(self.allowed_values)))
        object.__setattr__(self, "constraints", tuple(self.constraints))
        runtime_context = _normalize_context(self.runtime_context, "runtime context")
        runtime_provenance = _normalize_context(
            self.runtime_context_provenance,
            "runtime context provenance",
        )
        if not runtime_context and runtime_provenance:
            raise ValueError("runtime context provenance requires runtime context")
        object.__setattr__(self, "runtime_context", runtime_context)
        object.__setattr__(self, "runtime_context_provenance", runtime_provenance)
        execution_steps = tuple(self.execution_plan_steps)
        if any(not isinstance(item, Mapping) for item in execution_steps):
            raise TypeError("execution plan steps must be objects")
        canonical_json_bytes(execution_steps)
        object.__setattr__(
            self,
            "execution_plan_steps",
            tuple(freeze_value(dict(item)) for item in execution_steps),
        )
        if runtime_context:
            object.__setattr__(
                self,
                "runtime_context_digest",
                compute_framed_digest(RUNTIME_CONTEXT_SCHEMA_VERSION, runtime_context),
            )
        if self.author_context is not None:
            object.__setattr__(
                self,
                "author_context_digest",
                compute_framed_digest(
                    AUTHOR_CONTEXT_SCHEMA_VERSION,
                    self.author_context.prompt_mapping(),
                ),
            )

    def _validate(self) -> None:
        _validate_slots(self.slots)
        _validate_strings(self.allowed_tools, "presentation request tool names")
        _validate_tool_definitions(self.allowed_tool_definitions)
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
    "AuthorContext",
    "AUTHOR_CONTEXT_SCHEMA_VERSION",
    "PresentationAuthor",
    "PresentationRequest",
    "PresentationResult",
    "PresentationSlot",
    "RUNTIME_CONTEXT_SCHEMA_VERSION",
]
