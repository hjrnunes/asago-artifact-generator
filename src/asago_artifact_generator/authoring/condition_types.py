"""Structural operand-type checks for the handoff's bound tool-call condition.

The tool-call-condition contract gives a value comparison a fixed result for
operand types that cannot match: ``eq`` of two disjoint types is false, ``in``
with a right side that is not a list and ``gt`` with a non-numeric operand are
unknown on every capture. A condition that cannot decide cannot score a
command_attempt claim. These checks read only declared types (a literal's JSON
type, an argument's inventory schema type) and leave what a value means to the
model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .core import Finding, _json_value_type, staged_findings

CODE = "condition_operand_type_mismatch"
_ORDERING_OPS = frozenset({"gt", "ge", "lt", "le"})
_MEMBERSHIP_OPS = frozenset({"in", "not_in"})
_NUMERIC_TYPES = frozenset({"integer", "number"})


def operand_type_findings(condition: Any, inventory: Mapping[str, Any]) -> list[Finding]:
    """Return a plan-stage finding for each value comparison that cannot decide."""

    comparisons = condition.get("comparisons") if isinstance(condition, Mapping) else None
    if not isinstance(comparisons, list):
        return []
    arguments = _argument_types(inventory)
    findings: list[Finding] = []
    for index, comparison in enumerate(comparisons):
        problem = _comparison_problem(comparison, arguments)
        if problem is not None:
            findings.append(
                Finding(CODE, problem, f"tool_call_condition.comparisons[{index}]"),
            )
    return staged_findings(findings, "plan")


def _comparison_problem(
    comparison: Any, arguments: Mapping[tuple[str, str], set[str]]
) -> str | None:
    if not isinstance(comparison, Mapping) or comparison.get("kind") != "value":
        return None
    op = comparison.get("op")
    left = comparison.get("left")
    right = comparison.get("right")
    if op in _MEMBERSHIP_OPS:
        return _membership_problem(op, left, right, arguments)
    left_types = _operand_types(left, arguments)
    right_types = _operand_types(right, arguments)
    if op in _ORDERING_OPS:
        return _ordering_problem(op, left, left_types) or _ordering_problem(op, right, right_types)
    if op in {"eq", "ne"} and _disjoint(left_types, right_types) and not _is_null(left, right):
        return (
            f"{op} compares {_describe(left_types)} with {_describe(right_types)}, "
            "which are never equal"
        )
    return None


def _membership_problem(
    op: str, left: Any, right: Any, arguments: Mapping[tuple[str, str], set[str]]
) -> str | None:
    left_types = _operand_types(left, arguments)
    right_types = _operand_types(right, arguments)
    if right_types is not None and "array" not in right_types:
        return f"{op} needs a list on the right but it is {_describe(right_types)}"
    items = right.get("value") if isinstance(right, Mapping) else None
    if left_types is None or "array" in left_types or not isinstance(items, list):
        return None
    return _unmatched_items_problem(op, left_types, items)


def _unmatched_items_problem(op: str, left_types: set[str], items: list[Any]) -> str | None:
    item_types = {_json_value_type(item) for item in items}
    if not items or not all(_disjoint(left_types, {kind}) for kind in item_types):
        return None
    return (
        f"{op} tests {_describe(left_types)} against a list of "
        f"{_describe(item_types)} items, so no item can equal it"
    )


def _ordering_problem(op: str, operand: Any, types: set[str] | None) -> str | None:
    if types is None or types & _NUMERIC_TYPES:
        return None
    if "string" in types and not _non_numeric_literal(operand):
        return None
    return f"{op} converts both sides to numbers but an operand is {_describe(types)}"


def _non_numeric_literal(operand: Any) -> bool:
    """Return whether the operand is a literal string that ``float()`` rejects."""

    if not _is_literal(operand) or not isinstance(operand.get("value"), str):
        return False
    try:
        float(operand["value"])
    except ValueError:
        return True
    return False


def _is_literal(operand: Any) -> bool:
    return isinstance(operand, Mapping) and operand.get("source") == "literal"


def _is_null(left: Any, right: Any) -> bool:
    return any(_is_literal(side) and side.get("value") is None for side in (left, right))


def _operand_types(operand: Any, arguments: Mapping[tuple[str, str], set[str]]) -> set[str] | None:
    if not isinstance(operand, Mapping):
        return None
    if operand.get("source") == "literal" and "value" in operand:
        return {_json_value_type(operand["value"])}
    if operand.get("source") == "argument":
        return arguments.get((operand.get("operation"), operand.get("argument")))
    return None


def _disjoint(left: set[str] | None, right: set[str] | None) -> bool:
    if left is None or right is None:
        return False
    return not _widen(left) & _widen(right)


def _widen(types: set[str]) -> set[str]:
    return types | {"number", "integer"} if types & _NUMERIC_TYPES else set(types)


def _describe(types: set[str] | None) -> str:
    return "/".join(sorted(types or ()))


def _argument_types(inventory: Mapping[str, Any]) -> dict[tuple[str, str], set[str]]:
    result: dict[tuple[str, str], set[str]] = {}
    operations = inventory.get("operations")
    for operation in operations if isinstance(operations, list) else []:
        if not isinstance(operation, Mapping) or not isinstance(operation.get("name"), str):
            continue
        schema = operation.get("arguments")
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        for name, spec in properties.items() if isinstance(properties, Mapping) else ():
            types = _declared_types(spec)
            if types:
                result[(operation["name"], name)] = types
    return result


def _declared_types(spec: Any) -> set[str]:
    declared = spec.get("type") if isinstance(spec, Mapping) else None
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list) and all(isinstance(item, str) for item in declared):
        return set(declared)
    return set()


__all__ = ["CODE", "operand_type_findings"]
