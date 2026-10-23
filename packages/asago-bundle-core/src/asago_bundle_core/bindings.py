"""Resolve a package's declared bindings to values and fill its slots with them.

The declarations and slot references are checked by the consumer's
``binding_rules``, the one validator authoring uses too; this module reads the
values a run supplies (setup results and supplied facts) at the declared
selectors. Every failure is a ``BindingError`` whose ``code`` is the incomplete
reason ``prepare_package`` reports.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from asago_artifact_generator.binding_rules import (
    SLOT_PATTERN,
    BindingError,
    RuntimeBinding,
    check_slot_reference,
    declared_types,
    parse_binding,
    setup_operation,
    validate_binding_declarations,
    validate_slot_references,
    validate_stimulus_declarations,
)


def resolve_bindings(
    declarations: tuple[RuntimeBinding, ...] | list[RuntimeBinding],
    *,
    setup_outputs: dict[str, Any],
    supplied_inputs: dict[str, Any] | None = None,
    allow_null_bindings: Collection[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return each binding's value and where it came from.

    A setup binding reads ``result`` of its operation's output and a supplied
    binding reads ``value`` of its fact. A binding named in
    ``allow_null_bindings`` may resolve to null whatever its expected type.
    """

    values: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    supplied_inputs = supplied_inputs or {}
    allow_null_bindings = allow_null_bindings or set()
    for binding in declarations:
        if binding.source_kind == "setup_output":
            operation = setup_operation(binding.source_ref)
            source = {"result": setup_outputs.get(operation)}
            provenance_source = f"setup:{operation}"
        else:
            fact = binding.source_ref.removeprefix("facts:")
            source = {"value": supplied_inputs.get(fact)}
            provenance_source = f"facts:{fact}"
        try:
            value = select_value(source, binding.selector)
        except BindingError as exc:
            raise BindingError(
                f"binding_missing:{binding.name}:{exc}", code="binding_missing"
            ) from exc
        if not value_matches_type(value, binding.expected_type) and not (
            value is None and binding.name in allow_null_bindings
        ):
            raise BindingError(f"binding_mistyped:{binding.name}", code="binding_mistyped")
        values[binding.name] = value
        provenance[binding.name] = {
            "source_kind": binding.source_kind,
            "source_ref": binding.source_ref,
            "selector": binding.selector,
            "provenance": provenance_source,
        }
    return values, provenance


def substitute_slots(
    template: str,
    values: dict[str, Any],
    declarations: tuple[RuntimeBinding, ...] | list[RuntimeBinding],
) -> str:
    """Fill every ``{{name}}`` slot of ``template`` with its binding's scalar value."""

    expected_types = declared_types(declarations)
    for match in SLOT_PATTERN.finditer(template):
        token = match.group(1)
        check_slot_reference(token, expected_types)
        if token not in values:
            raise BindingError(f"missing bound value: {token}", code="binding_missing")
        if not value_matches_type(values[token], expected_types[token]):
            raise BindingError(f"mistyped bound value: {token}", code="binding_mistyped")
        if isinstance(values[token], (dict, list)):
            raise BindingError(f"non-scalar slot value: {token}")
    return SLOT_PATTERN.sub(lambda match: str(values[match.group(1)]), template)


def select_value(source: Any, selector: str) -> Any:
    """Return the value a dot selector names in ``source``, reading object keys only."""

    parts = selector.split(".")
    if not parts or any(not part for part in parts):
        raise BindingError(f"invalid selector: {selector}", code="binding_missing")
    current = source
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise BindingError(f"selector not found: {selector}", code="binding_missing")
    return current


def value_matches_type(value: Any, expected: str) -> bool:
    """Return whether ``value`` is of the closed binding type ``expected``."""

    return {
        "boolean": lambda: isinstance(value, bool),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "string": lambda: isinstance(value, str),
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
    }.get(expected, lambda: False)()


__all__ = [
    "BindingError",
    "RuntimeBinding",
    "parse_binding",
    "resolve_bindings",
    "select_value",
    "substitute_slots",
    "validate_binding_declarations",
    "validate_slot_references",
    "validate_stimulus_declarations",
    "value_matches_type",
]
