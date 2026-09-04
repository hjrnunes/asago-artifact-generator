"""Closed semantic-condition variants shared by the execution projection.

The producer publishes one discriminated condition union.  Keeping the same
variants at the consumer boundary prevents a value placeholder from changing
the condition family, subject, property, or operator during binding.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, model_validator

from ._base import ImmutableModel

# Keep this leaf deliberately independent from projection/adapter code.  The
# producer owns the proposition's meaning; the consumer only checks that the
# persisted value is bounded plain text before preserving it byte-for-byte.
_SEMANTIC_PROPOSITION_MAX_LENGTH = 600
_SEMANTIC_PROPOSITION_ID = re.compile(
    r"\b(?:PM|FB|CA|CM|CL|CP|RESP|H|L|SC|CF|SEM|REQ|OUTCOME|EXEC|SCN)-[A-Za-z0-9._-]+\b"
)
_SEMANTIC_PROPOSITION_URL = re.compile(r"\b(?:https?|ftp)://|\bwww\.", re.IGNORECASE)
_SEMANTIC_PROPOSITION_SECRET = re.compile(
    r"\b(?:api[_ -]?key|credential|password|secret|token)\b", re.IGNORECASE
)

_STRUCTURAL_REFERENCE = re.compile(r"^(?:PM|FB|CA|CM)-\d+(?:-\d+)?$|^S-\d+$")
_ACTION_REFERENCE = re.compile(r"^(?:CA|CM)-\d+(?:-\d+)?$")
CONDITION_TYPES = (
    "ordering",
    "delay",
    "duration",
    "window",
    "absence",
    "action_presence",
    "action_value",
    "state_value",
)
OPERATORS = (
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
)


class SemanticBindingPlaceholder(ImmutableModel):
    """A typed value hole retained from producer semantic evidence."""

    binding_ref: StrictStr = Field(min_length=1, pattern=r"^SEM-[A-Za-z0-9._-]+$")
    value_type: Literal["integer", "number", "string", "boolean"]
    description: StrictStr = Field(min_length=1)
    minimum: StrictInt | StrictFloat | None = None
    maximum: StrictInt | StrictFloat | None = None

    @model_validator(mode="after")
    def _validate_bounds(self) -> SemanticBindingPlaceholder:
        _validate_bound_kind(self)
        _validate_bound_values(self)
        _validate_bound_order(self)
        return self


def _validate_bound_kind(value: SemanticBindingPlaceholder) -> None:
    bounds = (("minimum", value.minimum), ("maximum", value.maximum))
    validators = {
        "integer": _validate_integer_bounds,
        "number": _validate_number_bounds,
        "string": _validate_no_numeric_bounds,
        "boolean": _validate_no_numeric_bounds,
    }
    validators[value.value_type](bounds)


def _validate_integer_bounds(bounds: tuple[tuple[str, Any], ...]) -> None:
    for name, bound in bounds:
        _validate_integer_bound(name, bound)


def _validate_integer_bound(name: str, bound: Any) -> None:
    if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int)):
        raise ValueError(f"{name} must be an integer for an integer binding")


def _validate_number_bounds(bounds: tuple[tuple[str, Any], ...]) -> None:
    for name, bound in bounds:
        _validate_number_bound(name, bound)


def _validate_number_bound(name: str, bound: Any) -> None:
    if bound is not None and isinstance(bound, bool):
        raise ValueError(f"{name} must be numeric for a number binding")
    if isinstance(bound, float) and not math.isfinite(bound):
        raise ValueError(f"{name} must be finite")


def _validate_no_numeric_bounds(bounds: tuple[tuple[str, Any], ...]) -> None:
    if any(bound is not None for _, bound in bounds):
        raise ValueError("string and boolean bindings cannot declare numeric bounds")


def _validate_bound_values(value: SemanticBindingPlaceholder) -> None:
    for name, bound in (("minimum", value.minimum), ("maximum", value.maximum)):
        if bound is not None and bound < 0:
            raise ValueError(f"{name} must be non-negative")


def _validate_bound_order(value: SemanticBindingPlaceholder) -> None:
    if value.minimum is not None and value.maximum is not None and value.minimum > value.maximum:
        raise ValueError("minimum must not exceed maximum")


SemanticLiteral = StrictStr | StrictInt | StrictFloat | StrictBool
SemanticValue = SemanticBindingPlaceholder | SemanticLiteral
SemanticOperator = Literal[
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
]


class SemanticConditionModel(ImmutableModel):
    """Common immutable behavior for each discriminated condition variant."""

    def placeholders(self) -> tuple[SemanticBindingPlaceholder, ...]:
        """Return placeholders in deterministic field order."""

        values = tuple(
            getattr(self, name, None)
            for name in ("delay_ms", "duration_ms", "window_from_ms", "window_to_ms", "expected")
        )
        return tuple(item for item in values if isinstance(item, SemanticBindingPlaceholder))

    def references(self) -> tuple[str, ...]:
        """Return structural/step references used by this condition."""

        values = tuple(
            getattr(self, name, None)
            for name in (
                "reference_step_id",
                "reference_ref",
                "until_step_id",
                "control_action_id",
                "subject_ref",
            )
        )
        return tuple(item for item in values if item)


class OrderingCondition(SemanticConditionModel):
    """A relation between an action and an exported projected step."""

    type: Literal["ordering"] = "ordering"
    reference_step_id: StrictStr = Field(min_length=1, pattern=r"^S-\d+$")
    relation: Literal["before", "after"]


class DelayCondition(SemanticConditionModel):
    """A non-negative feedback delay in integer milliseconds."""

    type: Literal["delay"] = "delay"
    reference_ref: StrictStr = Field(min_length=1)
    delay_ms: SemanticValue

    @model_validator(mode="after")
    def _validate_condition(self) -> DelayCondition:
        _validate_structural_reference(self.reference_ref, "reference_ref")
        _validate_time_value(self.delay_ms, "delay_ms")
        return self


class DurationCondition(SemanticConditionModel):
    """A non-negative action duration in integer milliseconds."""

    type: Literal["duration"] = "duration"
    reference_ref: StrictStr = Field(min_length=1)
    duration_ms: SemanticValue

    @model_validator(mode="after")
    def _validate_condition(self) -> DurationCondition:
        _validate_structural_reference(self.reference_ref, "reference_ref")
        _validate_time_value(self.duration_ms, "duration_ms")
        return self


class WindowCondition(SemanticConditionModel):
    """An ordered timing window in integer milliseconds."""

    type: Literal["window"] = "window"
    reference_ref: StrictStr = Field(min_length=1)
    window_from_ms: SemanticValue
    window_to_ms: SemanticValue

    @model_validator(mode="after")
    def _validate_condition(self) -> WindowCondition:
        _validate_structural_reference(self.reference_ref, "reference_ref")
        _validate_time_value(self.window_from_ms, "window_from_ms")
        _validate_time_value(self.window_to_ms, "window_to_ms")
        if not isinstance(self.window_from_ms, SemanticBindingPlaceholder) and not isinstance(
            self.window_to_ms, SemanticBindingPlaceholder
        ):
            if self.window_from_ms > self.window_to_ms:  # type: ignore[operator]
                raise ValueError("window_from_ms must not exceed window_to_ms")
        return self


class AbsenceCondition(SemanticConditionModel):
    """A structural signal remains absent until an exported step."""

    type: Literal["absence"] = "absence"
    reference_ref: StrictStr = Field(min_length=1)
    until_step_id: StrictStr = Field(min_length=1, pattern=r"^S-\d+$")

    @model_validator(mode="after")
    def _validate_condition(self) -> AbsenceCondition:
        _validate_structural_reference(self.reference_ref, "reference_ref")
        return self


class ActionPresenceCondition(SemanticConditionModel):
    """The selected action is absent when required."""

    type: Literal["action_presence"] = "action_presence"
    control_action_id: StrictStr = Field(min_length=1)
    expected: Literal["not_provided"] = "not_provided"

    @model_validator(mode="after")
    def _validate_condition(self) -> ActionPresenceCondition:
        _validate_action_reference(self.control_action_id)
        return self


class ActionValueCondition(SemanticConditionModel):
    """The action's semantic property has an expected value."""

    type: Literal["action_value"] = "action_value"
    control_action_id: StrictStr = Field(min_length=1)
    property: StrictStr = Field(min_length=1)
    operator: SemanticOperator
    expected: SemanticValue

    @model_validator(mode="after")
    def _validate_condition(self) -> ActionValueCondition:
        _validate_action_reference(self.control_action_id)
        _validate_scalar(self.expected, "expected")
        return self


class StateValueCondition(SemanticConditionModel):
    """A structural state subject has an expected semantic value."""

    type: Literal["state_value"] = "state_value"
    subject_ref: StrictStr = Field(min_length=1)
    property: StrictStr = Field(min_length=1)
    operator: SemanticOperator
    expected: SemanticValue

    @model_validator(mode="after")
    def _validate_condition(self) -> StateValueCondition:
        _validate_structural_reference(self.subject_ref, "subject_ref")
        _validate_scalar(self.expected, "expected")
        return self


SemanticCondition = Annotated[
    (
        OrderingCondition
        | DelayCondition
        | DurationCondition
        | WindowCondition
        | AbsenceCondition
        | ActionPresenceCondition
        | ActionValueCondition
        | StateValueCondition
    ),
    Field(discriminator="type"),
]


def _validate_structural_reference(value: str, field_name: str) -> None:
    if not _STRUCTURAL_REFERENCE.fullmatch(value):
        raise ValueError(f"{field_name} must resolve to a PM/FB/CA/CM/S structural ID")


def _validate_action_reference(value: str) -> None:
    if not _ACTION_REFERENCE.fullmatch(value):
        raise ValueError("control_action_id must be a canonical CA-* or CM-* ID")


def _validate_scalar(value: SemanticValue, field_name: str) -> None:
    if _is_valid_placeholder(value):
        return
    _validate_scalar_literal(value, field_name)


def _is_valid_placeholder(value: Any) -> bool:
    return isinstance(value, SemanticBindingPlaceholder)


def _validate_scalar_literal(value: Any, field_name: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if not isinstance(value, (str, int, float, bool)) or value is None:
        raise ValueError(f"{field_name} must be a literal scalar or typed placeholder")


def _validate_time_value(value: SemanticValue, field_name: str) -> None:
    _validate_scalar(value, field_name)
    if isinstance(value, SemanticBindingPlaceholder):
        if value.value_type not in ("integer", "number"):
            raise ValueError(f"{field_name} placeholder must be numeric")
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a non-negative integer number of milliseconds")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")


def normalize_semantic_proposition(
    value: str | None,
    *,
    required: bool = False,
) -> str | None:
    """Validate one producer-authored proposition without assigning meaning.

    The proposition is copied into the execution oracle exactly as supplied by
    the producer.  This function therefore validates the closed, bounded text
    contract but does not rewrite, paraphrase, or infer a replacement value.
    Producer-side materialization performs the one permitted outer-whitespace
    normalization before publication.
    """

    if value is None:
        _require_proposition(value, required)
        return None
    if not isinstance(value, str):
        raise ValueError("semantic_proposition must be a string or null")
    if not value:
        _require_proposition(value, required)
        return None
    _validate_proposition_text(value)
    return value


def _require_proposition(value: str | None, required: bool) -> None:
    if not required:
        return
    message = (
        "semantic_proposition is required for output-text observation"
        if value is None
        else "semantic_proposition must be non-empty"
    )
    raise ValueError(message)


def _validate_proposition_text(value: str) -> None:
    _validate_proposition_whitespace(value)
    _validate_proposition_length(value)
    _validate_proposition_line(value)
    _validate_proposition_references(value)


def _validate_proposition_whitespace(value: str) -> None:
    if value != value.strip():
        raise ValueError("semantic_proposition must be outer-whitespace normalized")


def _validate_proposition_length(value: str) -> None:
    if len(value) > _SEMANTIC_PROPOSITION_MAX_LENGTH:
        limit = _SEMANTIC_PROPOSITION_MAX_LENGTH
        raise ValueError(f"semantic_proposition exceeds maximum length of {limit} characters")


def _validate_proposition_line(value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError("semantic_proposition must be one plain line")


def _validate_proposition_references(value: str) -> None:
    if _SEMANTIC_PROPOSITION_URL.search(value):
        raise ValueError("semantic_proposition must not contain a runtime URL")
    if _SEMANTIC_PROPOSITION_SECRET.search(value):
        raise ValueError("semantic_proposition must not contain credential material")
    if _SEMANTIC_PROPOSITION_ID.search(value):
        raise ValueError("semantic_proposition must not contain structural identifiers")


__all__ = [
    "AbsenceCondition",
    "ActionPresenceCondition",
    "ActionValueCondition",
    "CONDITION_TYPES",
    "DelayCondition",
    "DurationCondition",
    "OrderingCondition",
    "OPERATORS",
    "SemanticBindingPlaceholder",
    "SemanticCondition",
    "SemanticConditionModel",
    "SemanticLiteral",
    "SemanticOperator",
    "SemanticValue",
    "StateValueCondition",
    "WindowCondition",
    "normalize_semantic_proposition",
]
