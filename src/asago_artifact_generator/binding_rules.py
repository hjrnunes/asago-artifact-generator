"""The binding and slot rules of an artifact package, in one place.

Authoring checks the declarations and slots a plan and an artifact write with
these rules and words its own findings for the model. Package preparation, which
runs downstream, checks a frozen package with ``validate_binding_declarations``
and ``validate_slot_references`` before it resolves any value; each
``BindingError`` carries the incomplete reason preparation reports.

The closed vocabulary, the selector rules (root, schema walk, array step, type
compatibility) and the slot rules (pattern, identifier name, declared binding,
scalar value) are defined here once, for both.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ConsumerPrefix:
    """A consumer destination family: the prefix and what the rest of the path names."""

    prefix: str
    suffix: str

    @property
    def spelling(self) -> str:
        return f"{self.prefix}<{self.suffix}>"


@dataclass(frozen=True)
class BindingSpec:
    """The closed runtime-binding vocabulary; tuple order is the wire and schema order."""

    fields: tuple[str, ...]
    expected_types: tuple[str, ...]
    source_kinds: tuple[str, ...]
    missing_policies: tuple[str, ...]
    consumer_paths: tuple[str, ...]
    consumer_prefixes: tuple[ConsumerPrefix, ...]

    @property
    def string_fields(self) -> tuple[str, ...]:
        return tuple(name for name in self.fields if name != "consumers")

    @property
    def consumer_destinations(self) -> tuple[str, ...]:
        """Return each closed destination as a model writes it, in prompt order."""

        return (*self.consumer_paths, *(family.spelling for family in self.consumer_prefixes))

    def is_closed_consumer(self, value: str) -> bool:
        return value in self.consumer_paths or value.startswith(
            tuple(family.prefix for family in self.consumer_prefixes)
        )


PREREQUISITE_CONSUMERS = ConsumerPrefix("prerequisites.", "binding name")
JUDGE_CONSUMERS = ConsumerPrefix("judge.", "binding name")
SETUP_ARGUMENT_CONSUMERS = ConsumerPrefix("setup.arguments.", "argument name")


BINDING_SPEC = BindingSpec(
    fields=(
        "name",
        "expected_type",
        "source_kind",
        "source_ref",
        "selector",
        "consumers",
        "on_missing",
    ),
    expected_types=("array", "boolean", "integer", "number", "object", "string"),
    source_kinds=("supplied_input", "setup_output"),
    missing_policies=("inconclusive", "stop"),
    consumer_paths=("stimulus.user_text", "stimulus.history"),
    consumer_prefixes=(PREREQUISITE_CONSUMERS, JUDGE_CONSUMERS, SETUP_ARGUMENT_CONSUMERS),
)
CLOSED_TYPES = frozenset(BINDING_SPEC.expected_types)
SOURCE_KINDS = frozenset(BINDING_SPEC.source_kinds)
MISSING_POLICIES = frozenset(BINDING_SPEC.missing_policies)
_BINDING_FIELDS = frozenset(BINDING_SPEC.fields)
# A selector starts at the root its source kind's value is read under.
SELECTOR_ROOTS = {"supplied_input": "value", "setup_output": "result"}
_INDEX_SUFFIX = re.compile(r"\[\d+\]$")

SLOT_PATTERN = re.compile(r"\{\{([^{}]*)\}\}")
# A slot is filled only when its name is an identifier, whatever the bindings declare.
SLOT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A slot is filled with one scalar; a binding of these types cannot fill one.
NON_SCALAR_TYPES = frozenset({"array", "object"})
INVALID_SLOT_NAME = "invalid_slot_name"
UNDECLARED_SLOT = "undeclared_slot"
NON_SCALAR_SLOT = "non_scalar_slot"


class BindingValidationError(ValueError):
    """Raised when a binding or substitution is not closed and documented."""


class BindingError(ValueError):
    """Raised when a package's binding or slot cannot be resolved exactly.

    ``code`` is the incomplete reason package preparation reports:
    ``binding_invalid`` (the default), ``binding_missing``, ``binding_mistyped``,
    or ``stimulus_invalid``, which preparation reports as ``binding_invalid``.
    """

    def __init__(self, message: str, *, code: str = "binding_invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RuntimeBinding:
    """One typed value that downstream execution resolves without re-authoring."""

    name: str
    expected_type: str
    source_kind: str
    source_ref: str
    selector: str
    consumers: tuple[str, ...]
    on_missing: str

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "consumers": list(self.consumers)}

    @classmethod
    def from_dict(cls, value: Any) -> RuntimeBinding:
        """Read an authored declaration: every field a string, every consumer closed."""

        if not isinstance(value, dict):
            raise BindingValidationError("binding must be an object")
        _require_binding_fields(value)
        consumers = value["consumers"]
        if not isinstance(consumers, list) or not all(
            isinstance(item, str) and item.strip() for item in consumers
        ):
            raise BindingValidationError("binding consumers must be non-empty strings")
        _require_string_fields(value)
        if not all(BINDING_SPEC.is_closed_consumer(item) for item in consumers):
            raise BindingValidationError("binding consumer is not a closed path")
        return cls(**{**value, "consumers": tuple(consumers)})

    @classmethod
    def parse(cls, value: Any) -> RuntimeBinding:
        """Read a package declaration: the field set and the consumer list.

        ``validate_binding_declarations`` checks the closed values afterwards.
        """

        if not isinstance(value, dict):
            raise BindingError("binding must be an object")
        missing, unknown = _BINDING_FIELDS - set(value), set(value) - _BINDING_FIELDS
        if missing or unknown:
            raise BindingError(
                f"binding fields invalid (missing={sorted(missing)}, unknown={sorted(unknown)})",
                code="binding_missing",
            )
        consumers = value["consumers"]
        if not isinstance(consumers, list) or not all(
            isinstance(item, str) and item.strip() for item in consumers
        ):
            raise BindingError("binding consumers must be non-empty strings")
        return cls(**{**value, "consumers": tuple(consumers)})


def _require_binding_fields(value: dict[str, Any]) -> None:
    missing = _BINDING_FIELDS - set(value)
    unknown = set(value) - _BINDING_FIELDS
    if missing or unknown:
        raise BindingValidationError(
            f"binding fields invalid (missing={sorted(missing)}, unknown={sorted(unknown)})"
        )


def _require_string_fields(value: dict[str, Any]) -> None:
    for field_name in BINDING_SPEC.string_fields:
        if not isinstance(value[field_name], str):
            raise BindingValidationError(f"binding {field_name} must be a string")


def selector_root(selector: str) -> str | None:
    """Return a selector's root part when it is ``result`` or ``value``, else None.

    Setup outputs are documented relative to ``result``; supplied facts use
    ``value``.  Requiring the root makes accidental field-name inference fail.
    """

    root = selector.split(".", 1)[0]
    return root if root in SELECTOR_ROOTS.values() else None


def selector_root_mismatch(name: Any, source_kind: Any, selector: str) -> str | None:
    """Describe a selector rooted for the other source kind, or return None.

    Execute reads a supplied fact under ``value`` and a setup output under
    ``result``, so the other root resolves to nothing even when the schema
    below it documents the path. An unknown root is left to the
    undocumented-selector check.
    """

    root = selector_root(selector)
    expected = SELECTOR_ROOTS.get(source_kind) if isinstance(source_kind, str) else None
    if root is None or expected is None or root == expected:
        return None
    return (
        f"selector for binding {name} must be rooted at {expected} for {source_kind}, "
        f"not {root}: {selector}"
    )


def schema_child(current: Any, part: str) -> Any:
    """Return the schema one selector part names, or None when it names none.

    A selector follows object properties only. A non-dict schema documents no
    type, so returning None for it leaves the caller's result unchanged.
    """

    if not isinstance(current, dict):
        return None
    if current.get("type") == "object":
        properties = current.get("properties")
        if not isinstance(properties, dict) or part not in properties:
            return None
        return properties[part]
    return None


def type_below_root(schema: Any, selector: str) -> str | None:
    """Return the type the parts after a selector's root name in ``schema``, or None."""

    current: Any = schema
    # A failed step yields None, and None stays None through later steps.
    for part in selector.split(".")[1:]:
        current = schema_child(current, part)
    return current.get("type") if isinstance(current, dict) else None


def selector_type(schema: Any, selector: str) -> str | None:
    """Return the documented type a ``result``- or ``value``-rooted selector names."""

    if selector_root(selector) is None:
        return None
    return type_below_root(schema, selector)


def selector_array_step(schema: Any, selector: str) -> str | None:
    """Return the selector prefix naming the array that ``selector`` steps into, or None.

    A selector follows object properties only. The ``items`` schema keyword, a
    numeric part (``content.0``), and a bracketed index (``content[0]``) are not
    properties, and a recorded value is never read through an array. A selector
    with an unknown root steps into nothing.
    """

    parts = selector.split(".")
    if selector_root(selector) is None:
        return None
    current = schema
    for index, part in enumerate(parts[1:], start=1):
        name = _INDEX_SUFFIX.sub("", part)
        if name != part and _is_array(schema_child(current, name)):
            return ".".join([*parts[:index], name])
        if _is_array(current) and (part == "items" or part.isdecimal()):
            return ".".join(parts[:index])
        current = schema_child(current, part)
    return None


def _is_array(schema: Any) -> bool:
    return isinstance(schema, dict) and schema.get("type") == "array"


def types_compatible(actual: str, expected: str) -> bool:
    """Return whether a source of type ``actual`` fills a binding of type ``expected``."""

    return actual == expected or (actual == "integer" and expected == "number")


def setup_operation(source_ref: str) -> str:
    """Return the operation a ``setup:<operation>`` source reference names."""

    prefix, _, operation = source_ref.partition(":")
    if prefix != "setup" or not operation:
        raise BindingError(f"setup binding source_ref must be setup:<operation>: {source_ref}")
    return operation


def validate_binding_declarations(
    declarations: Any,
    *,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> tuple[RuntimeBinding, ...]:
    """Check a package's declarations against its inventory and runtime contract.

    Raises ``BindingError`` for the first declaration that is not closed, not
    documented, or whose source type cannot fill it.
    """

    if not isinstance(declarations, list):
        raise BindingError("runtime_bindings must be a list")
    result: list[RuntimeBinding] = []
    names: set[str] = set()
    for raw in declarations:
        binding = RuntimeBinding.parse(raw)
        _check_closed_fields(binding, names)
        mismatch = selector_root_mismatch(binding.name, binding.source_kind, binding.selector)
        if mismatch is not None:
            raise BindingError(mismatch)
        schema = _source_schema(binding, inventory, runtime_contract)
        actual = _selected_type(binding, schema)
        if not types_compatible(actual, binding.expected_type):
            raise BindingError(
                f"binding type mismatch for {binding.name}: expected "
                f"{binding.expected_type}, source is {actual}"
            )
        names.add(binding.name)
        result.append(binding)
    return tuple(result)


def _check_closed_fields(binding: RuntimeBinding, names: set[str]) -> None:
    if not isinstance(binding.name, str) or not binding.name or binding.name in names:
        raise BindingError(f"duplicate or blank binding: {binding.name!r}")
    if not isinstance(binding.expected_type, str) or binding.expected_type not in CLOSED_TYPES:
        raise BindingError(f"binding expected_type is not closed: {binding.name}")
    if not isinstance(binding.source_kind, str) or binding.source_kind not in SOURCE_KINDS:
        raise BindingError(f"binding source_kind is not closed: {binding.name}")
    if not isinstance(binding.on_missing, str) or binding.on_missing not in MISSING_POLICIES:
        raise BindingError(
            f"binding on_missing is not closed: {binding.name}", code="binding_missing"
        )
    if not binding.consumers:
        raise BindingError(f"binding consumers must be non-empty strings: {binding.name}")
    for consumer in binding.consumers:
        if not BINDING_SPEC.is_closed_consumer(consumer):
            raise BindingError(
                f"binding consumer is not a closed path for {binding.name}: {consumer}"
            )


def _source_schema(
    binding: RuntimeBinding, inventory: dict[str, Any], runtime_contract: dict[str, Any]
) -> dict[str, Any]:
    schema = (
        _setup_output_schema(binding, inventory, runtime_contract)
        if binding.source_kind == "setup_output"
        else _supplied_input_schema(binding, inventory)
    )
    if not isinstance(schema, dict):
        raise BindingError(
            f"missing source schema for binding: {binding.name}", code="binding_missing"
        )
    return schema


def _setup_output_schema(
    binding: RuntimeBinding, inventory: dict[str, Any], runtime_contract: dict[str, Any]
) -> Any:
    operation = setup_operation(binding.source_ref)
    record = next(
        (
            item
            for item in inventory.get("operations", [])
            if isinstance(item, dict) and item.get("name") == operation
        ),
        None,
    )
    if record is None:
        raise BindingError(f"unknown setup operation: {operation}")
    permitted = runtime_contract.get("setup_permissions", [])
    if not isinstance(permitted, list) or operation not in permitted:
        raise BindingError(f"setup operation is not permitted: {operation}")
    return record.get("result_schema")


def _supplied_input_schema(binding: RuntimeBinding, inventory: dict[str, Any]) -> Any:
    fact_name = binding.source_ref.removeprefix("facts:")
    record = next(
        (
            item
            for item in inventory.get("facts", [])
            if isinstance(item, dict) and item.get("ref") == fact_name
        ),
        None,
    )
    return record.get("schema") if record else None


def _selected_type(binding: RuntimeBinding, schema: dict[str, Any]) -> str:
    """Return the documented type that ``binding.selector`` names in ``schema``."""

    array_path = selector_array_step(schema, binding.selector)
    if array_path is not None:
        raise BindingError(
            f"selector for binding {binding.name} steps into the array at "
            f"{array_path}: {binding.selector}"
        )
    actual = selector_type(schema, binding.selector)
    if actual is None:
        raise BindingError(
            f"undocumented selector for binding {binding.name}: {binding.selector}",
            code="binding_missing",
        )
    return actual


def slot_names(text: str) -> list[str]:
    """Return the unique slot names in ``text``, in first-appearance order."""

    return list(dict.fromkeys(match.group(1) for match in SLOT_PATTERN.finditer(text)))


def slot_problems(
    slot: str, declared: Collection[str], expected_types: Mapping[str, Any]
) -> tuple[str, ...]:
    """Return why one slot cannot be filled: its name problem first, then its type.

    A name that is not an identifier is ``invalid_slot_name`` and an identifier
    no binding declares is ``undeclared_slot``; a declared binding whose
    expected type is an array or object adds ``non_scalar_slot``.
    """

    problems: list[str] = []
    if not SLOT_NAME.fullmatch(slot):
        problems.append(INVALID_SLOT_NAME)
    elif slot not in declared:
        problems.append(UNDECLARED_SLOT)
    if expected_types.get(slot) in NON_SCALAR_TYPES:
        problems.append(NON_SCALAR_SLOT)
    return tuple(problems)


def slot_reference_problems(
    text: str, declared: Collection[str], expected_types: Mapping[str, Any]
) -> list[tuple[str, str]]:
    """Return ``(slot, problem)`` for every problem of every slot in ``text``, in order."""

    return [
        (slot, problem)
        for slot in slot_names(text)
        for problem in slot_problems(slot, declared, expected_types)
    ]


def check_slot_reference(slot: str, expected_types: Mapping[str, Any]) -> None:
    """Raise ``BindingError`` when the declarations in ``expected_types`` cannot fill ``slot``.

    A slot whose name is not an identifier is reported as undeclared.
    """

    for problem in slot_problems(slot, expected_types, expected_types):
        if problem == NON_SCALAR_SLOT:
            raise BindingError(f"non-scalar slot value: {slot}")
        raise BindingError(f"undeclared slot: {slot}")


def declared_types(
    declarations: tuple[RuntimeBinding, ...] | list[RuntimeBinding],
) -> dict[str, str]:
    """Map each declared binding name to its expected type."""

    return {item.name: item.expected_type for item in declarations}


def validate_slot_references(
    template: str,
    declarations: tuple[RuntimeBinding, ...] | list[RuntimeBinding],
) -> None:
    """Reject invalid or undeclared slots without resolving their values."""

    expected_types = declared_types(declarations)
    for match in SLOT_PATTERN.finditer(template):
        check_slot_reference(match.group(1), expected_types)


def stimulus_history(stimulus: dict[str, Any]) -> list[Any]:
    """Return a package stimulus's history, which must be a list."""

    history = stimulus.get("history", [])
    if not isinstance(history, list):
        raise BindingError("stimulus history must be a list", code="stimulus_invalid")
    return history


def validate_stimulus_declarations(
    stimulus: Any,
    declarations: tuple[RuntimeBinding, ...] | list[RuntimeBinding],
) -> None:
    """Check a package stimulus's shape and declared slots without resolving values."""

    if not isinstance(stimulus, dict) or not isinstance(stimulus.get("user_text"), str):
        raise BindingError("stimulus user_text is unavailable", code="stimulus_invalid")
    validate_slot_references(stimulus["user_text"], declarations)
    for turn in stimulus_history(stimulus):
        if isinstance(turn, dict) and isinstance(turn.get("content"), str):
            validate_slot_references(turn["content"], declarations)


__all__ = [
    "BINDING_SPEC",
    "CLOSED_TYPES",
    "INVALID_SLOT_NAME",
    "JUDGE_CONSUMERS",
    "MISSING_POLICIES",
    "NON_SCALAR_SLOT",
    "NON_SCALAR_TYPES",
    "PREREQUISITE_CONSUMERS",
    "SELECTOR_ROOTS",
    "SETUP_ARGUMENT_CONSUMERS",
    "SLOT_NAME",
    "SLOT_PATTERN",
    "SOURCE_KINDS",
    "UNDECLARED_SLOT",
    "BindingError",
    "BindingSpec",
    "BindingValidationError",
    "ConsumerPrefix",
    "RuntimeBinding",
    "check_slot_reference",
    "declared_types",
    "schema_child",
    "selector_array_step",
    "selector_root",
    "selector_root_mismatch",
    "selector_type",
    "setup_operation",
    "slot_names",
    "slot_problems",
    "slot_reference_problems",
    "stimulus_history",
    "type_below_root",
    "types_compatible",
    "validate_binding_declarations",
    "validate_slot_references",
    "validate_stimulus_declarations",
]
