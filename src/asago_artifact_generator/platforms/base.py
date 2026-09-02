"""Small typed seams shared by platform planners and compilers.

The semantic inputs to these protocols are the frozen inward models exported
by :mod:`asago_artifact_generator.models`.  This module intentionally does not
accept raw dictionaries or create provider clients; platform adapters receive
only a validated ready plan and return serializable values.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from ..models._base import freeze_value

if TYPE_CHECKING:
    from ..authoring import PresentationAuthor
    from ..models import PlatformCapabilities, ReadyExecutionPlan


class PlatformPlanError(ValueError):
    """Raised when a typed ready plan cannot be represented by an adapter."""


class ArtifactValidationError(ValueError):
    """Raised when a compiled platform artifact violates adapter invariants."""


PlanT = TypeVar("PlanT")


def _validate_compiled_digest(digest: str) -> None:
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("compiled artifact artifact_digest must be a SHA-256 digest")


def _validate_compiled_mappings(
    artifact: Mapping[str, Any],
    trace: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> None:
    if not isinstance(artifact, Mapping):
        raise TypeError("compiled artifact artifact must be an object")
    if not isinstance(trace, Mapping):
        raise TypeError("compiled artifact trace must be an object")
    if not isinstance(validation, Mapping):
        raise TypeError("compiled artifact validation must be an object")


@dataclass(frozen=True, slots=True)
class CompiledArtifact:
    """A serializable artifact and its closed trace/validation sidecars."""

    platform: str
    artifact: Mapping[str, Any]
    artifact_digest: str
    trace: Mapping[str, Any]
    validation: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.platform.strip():
            raise ValueError("compiled artifact platform must not be empty")
        _validate_compiled_digest(self.artifact_digest)
        _validate_compiled_mappings(self.artifact, self.trace, self.validation)
        self._freeze()

    def _freeze(self) -> None:
        object.__setattr__(self, "artifact", freeze_value(self.artifact))
        object.__setattr__(self, "trace", freeze_value(self.trace))
        object.__setattr__(self, "validation", freeze_value(self.validation))

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "artifact": dict(self.artifact),
            "artifact_digest": self.artifact_digest,
            "trace": dict(self.trace),
            "validation": dict(self.validation),
        }


class PlatformAdapter(Protocol[PlanT]):
    """Protocol implemented by a concrete platform adapter."""

    def capabilities(self) -> PlatformCapabilities: ...

    def plan(self, ready: ReadyExecutionPlan) -> PlanT: ...

    def compile(
        self, plan: PlanT, author: PresentationAuthor | None = None
    ) -> CompiledArtifact: ...


__all__ = [
    "ArtifactValidationError",
    "CompiledArtifact",
    "PlatformAdapter",
    "PlatformPlanError",
]
