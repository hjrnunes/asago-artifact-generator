"""The binding and slot rules of an artifact package, in one place.

Authoring checks the declarations and slots a plan and an artifact write with
these rules and words its own findings for the model. Package preparation, which
runs downstream, checks a frozen package with ``validate_binding_declarations``
and ``validate_slot_references`` before it resolves any value; each
``BindingError`` carries the incomplete reason preparation reports.

The closed vocabulary, the selector rules (root, schema walk, array step, type
compatibility) and the slot rules (pattern, identifier name, declared binding,
scalar value) are defined here once, for both. Declarations are checked by one
engine, ``check_declarations``: a ``BindingPolicy`` names the checks it runs and
in what order, and the engine returns typed bindings and structured
``Violation``s for the caller to word. ``PACKAGE_POLICY`` is how preparation
reads a package; authoring states its own policies beside its wording.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict, dataclass, replace
from dataclasses import field as dataclass_field
from typing import Any, Literal


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


def _setup_reference(source_ref: str) -> str | None:
    """Return the operation a ``setup:<operation>`` source reference names, or None."""

    prefix, _, operation = source_ref.partition(":")
    return operation if prefix == "setup" and operation else None


def setup_operation(source_ref: str) -> str:
    """Return the operation a ``setup:<operation>`` source reference names."""

    operation = _setup_reference(source_ref)
    if operation is None:
        raise BindingError(f"setup binding source_ref must be setup:<operation>: {source_ref}")
    return operation


def first_fact_named(inventory: Mapping[str, Any], reference: Any) -> dict[str, Any] | None:
    """Return the first supplied fact whose ref equals ``reference``."""

    return next(
        (
            item
            for item in inventory.get("facts", [])
            if isinstance(item, dict) and item.get("ref") == reference
        ),
        None,
    )


# The declaration-validation engine. Every check of a declaration is named below,
# once; a ``BindingPolicy`` picks the checks and their order, and each caller words
# the ``Violation``s it gets back.

Chain = tuple[str, ...]
# (source_kind, source_ref, selector, inventory) -> the canonical (source_ref, selector).
CanonicalPaths = Callable[[str, str, str, dict[str, Any]], tuple[str, str]]


@dataclass(frozen=True)
class Violation:
    """One rule a declaration breaks, for the caller to word.

    ``index`` is the declaration's position (-1 for the declaration list itself),
    ``name`` its ``name`` as written, and ``field`` the part of the declaration
    the rule reads (``selector``, ``consumers[1]``), when it reads one.
    """

    kind: str
    index: int
    name: Any = None
    field: str | None = None
    details: Mapping[str, Any] = dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class BindingPolicy:
    """Which checks the engine runs on declarations, in what order, and how strictly.

    ``phases`` run in order, each over every declaration before the next phase.
    A phase is a sequence of chains; a chain stops at its first check that
    reports a violation or a gate (``if_str:<field>``, ``if_closed:<field>``,
    ``if_schema``) that does not hold. With ``stop_at_first`` the engine returns
    at the first violation; otherwise it runs every chain. A declaration that is
    not an object takes no check after ``object``. ``readable`` is the chain that
    makes one declaration readable as a ``RuntimeBinding``.

    The options state the rules that differ between readers:
    ``canonical_paths`` rewrites a source to its canonical path before the
    source is looked up (``canonical_selector`` says whether with the declared
    selector or the source kind's root); ``facts_prefix_required`` requires a
    supplied source to read ``facts:<ref>``; ``unknown_fact_reported`` reports a
    fact the inventory lacks rather than its missing schema;
    ``permissions_must_be_list`` permits nothing unless ``setup_permissions`` is a
    list; ``empty_selector_parts_undocumented`` treats a selector with an empty
    part as undocumented.
    """

    readable: Chain
    phases: tuple[tuple[Chain, ...], ...]
    stop_at_first: bool = True
    canonical_paths: CanonicalPaths | None = None
    canonical_selector: Literal["declared", "root"] = "declared"
    facts_prefix_required: bool = False
    unknown_fact_reported: bool = False
    permissions_must_be_list: bool = True
    empty_selector_parts_undocumented: bool = False


@dataclass(frozen=True)
class BindingCheck:
    """The engine's result: the declarations that broke no rule, and every violation."""

    bindings: tuple[RuntimeBinding, ...]
    violations: tuple[Violation, ...]


@dataclass
class _Declaration:
    raw: Any
    index: int
    schema: dict[str, Any] | None = None
    actual: str | None = None
    ended: bool = False


@dataclass
class _Context:
    inventory: dict[str, Any]
    runtime_contract: dict[str, Any]
    policy: BindingPolicy
    names: set[Any] = dataclass_field(default_factory=set)


_STOP = object()
_Outcome = Any  # None (holds), _STOP (gate closed), or a list of violations


def check_declarations(
    declarations: Any,
    *,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    policy: BindingPolicy,
) -> BindingCheck:
    """Run ``policy``'s checks over ``declarations`` and return what they found."""

    if not isinstance(declarations, list):
        return BindingCheck((), (Violation("not_list", -1),))
    context = _Context(inventory, runtime_contract, policy)
    states = [_Declaration(raw, index) for index, raw in enumerate(declarations)]
    violations: list[Violation] = []
    for phase in policy.phases:
        for state in states:
            for chain in phase:
                if state.ended:
                    break
                found = _run_chain(chain, state, context)
                violations.extend(found)
                if found and policy.stop_at_first:
                    return BindingCheck((), tuple(violations))
    broken = {item.index for item in violations}
    return BindingCheck(
        tuple(_binding(state.raw) for state in states if state.index not in broken),
        tuple(violations),
    )


def read_binding(value: Any, policy: BindingPolicy) -> RuntimeBinding | Violation:
    """Read one declaration with ``policy.readable``; return it or its first violation."""

    context = _Context({}, {}, policy)
    found = _run_chain(policy.readable, _Declaration(value, 0), context)
    return found[0] if found else _binding(value)


def resolve_source(
    source_kind: Any,
    source_ref: str,
    selector: Any,
    *,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    policy: BindingPolicy,
) -> tuple[dict[str, Any] | None, Violation | None]:
    """Return the schema a binding's source documents, or the violation that hides it."""

    if policy.canonical_paths is not None:
        lookup = (
            selector
            if policy.canonical_selector == "declared"
            else ("value" if source_kind == "supplied_input" else "result")
        )
        source_ref, _ = policy.canonical_paths(source_kind, source_ref, lookup, inventory)
    if source_kind == "setup_output":
        operation = _setup_reference(source_ref)
        if operation is None:
            return None, _source_violation("source_ref_form", source_kind, source_ref)
        record = next(
            (
                item
                for item in inventory.get("operations", [])
                if isinstance(item, dict) and item.get("name") == operation
            ),
            None,
        )
        if record is None:
            return None, _source_violation("unknown_operation", source_kind, source_ref, operation)
        permitted = runtime_contract.get("setup_permissions", [])
        if (policy.permissions_must_be_list and not isinstance(permitted, list)) or (
            operation not in permitted
        ):
            return None, _source_violation(
                "operation_not_permitted", source_kind, source_ref, operation
            )
        schema = record.get("result_schema")
    else:
        if policy.facts_prefix_required:
            prefix, _, reference = source_ref.partition(":")
            if prefix != "facts" or not reference:
                return None, _source_violation("source_ref_form", source_kind, source_ref)
        else:
            reference = source_ref.removeprefix("facts:")
        fact = first_fact_named(inventory, reference)
        if fact is None and policy.unknown_fact_reported:
            return None, _source_violation("unknown_fact", source_kind, source_ref, reference)
        schema = fact.get("schema") if fact else None
    if not isinstance(schema, dict):
        return None, _source_violation("schema_missing", source_kind, source_ref)
    return schema, None


def _source_violation(
    kind: str, source_kind: Any, source_ref: str, reference: str | None = None
) -> Violation:
    details = {"source_kind": source_kind, "source_ref": source_ref, "reference": reference}
    return Violation(kind, -1, field="source_ref", details=details)


def _binding(raw: dict[str, Any]) -> RuntimeBinding:
    return RuntimeBinding(**{**raw, "consumers": tuple(raw["consumers"])})


def _run_chain(chain: Chain, state: _Declaration, context: _Context) -> list[Violation]:
    for check in chain:
        outcome = _check(check, state, context)
        if outcome is _STOP:
            return []
        if outcome:
            return outcome
        if state.ended:
            return []
    return []


def _check(check: str, state: _Declaration, context: _Context) -> _Outcome:
    gate, _, field = check.partition(":")
    if gate == "if_str":
        return None if isinstance(state.raw.get(field), str) else _STOP
    if gate == "if_closed":
        closed = {"source_kind": SOURCE_KINDS, "expected_type": CLOSED_TYPES}[field]
        value = state.raw.get(field)
        return None if isinstance(value, str) and value in closed else _STOP
    return _CHECKS[check](state, context)


def _violation(state: _Declaration, kind: str, field: str | None = None, **details: Any) -> list:
    return [Violation(kind, state.index, state.raw.get("name"), field, details)]


def _check_object(state: _Declaration, context: _Context) -> _Outcome:
    if isinstance(state.raw, dict):
        return None
    state.ended = True
    return [Violation("not_object", state.index)]


def _check_fields(state: _Declaration, context: _Context) -> _Outcome:
    missing = _BINDING_FIELDS - set(state.raw)
    unknown = set(state.raw) - _BINDING_FIELDS
    if missing or unknown:
        return _violation(state, "fields", missing=missing, unknown=unknown)
    return None


def _check_string_fields(state: _Declaration, context: _Context) -> _Outcome:
    return [
        Violation("string_field", state.index, state.raw.get("name"), field_name)
        for field_name in BINDING_SPEC.string_fields
        if field_name in state.raw and not isinstance(state.raw[field_name], str)
    ]


def _check_consumers_list(state: _Declaration, context: _Context) -> _Outcome:
    if isinstance(state.raw.get("consumers"), list):
        return None
    return _violation(state, "consumers_not_list", "consumers")


def _check_consumers_empty(state: _Declaration, context: _Context) -> _Outcome:
    return (
        None if state.raw.get("consumers") else _violation(state, "consumers_empty", "consumers")
    )


def _consumer_items(state: _Declaration) -> list[Violation]:
    """Return each consumer that is blank or not a closed destination, in order."""

    found: list[Violation] = []
    for index, consumer in enumerate(state.raw["consumers"]):
        field_name = f"consumers[{index}]"
        if not isinstance(consumer, str) or not consumer.strip():
            found += _violation(state, "consumer_blank", field_name)
        elif not BINDING_SPEC.is_closed_consumer(consumer):
            found += _violation(state, "consumer_not_closed", field_name, consumer=consumer)
    return found


def _check_consumer_items(state: _Declaration, context: _Context) -> _Outcome:
    return _consumer_items(state)


def _check_consumer_blank(state: _Declaration, context: _Context) -> _Outcome:
    return [item for item in _consumer_items(state) if item.kind == "consumer_blank"]


def _check_consumer_closed(state: _Declaration, context: _Context) -> _Outcome:
    return [item for item in _consumer_items(state) if item.kind == "consumer_not_closed"]


def _check_duplicate(state: _Declaration, context: _Context) -> _Outcome:
    name = state.raw.get("name") if isinstance(state.raw, dict) else None
    if not isinstance(name, str):
        return None
    if name in context.names:
        return _violation(state, "duplicate")
    context.names.add(name)
    return None


def _check_name_or_duplicate(state: _Declaration, context: _Context) -> _Outcome:
    name = state.raw.get("name")
    if not isinstance(name, str) or not name or name in context.names:
        return _violation(state, "name_invalid", "name")
    context.names.add(name)
    return None


def _check_name_blank(state: _Declaration, context: _Context) -> _Outcome:
    return None if state.raw["name"].strip() else _violation(state, "name_blank", "name")


def _closed_check(field_name: str, values: frozenset[str]) -> Callable[..., _Outcome]:
    def check(state: _Declaration, context: _Context) -> _Outcome:
        value = state.raw.get(field_name)
        if isinstance(value, str) and value in values:
            return None
        return _violation(state, f"{field_name}_not_closed", field_name)

    return check


def _blank_check(field_name: str, kind: str) -> Callable[..., _Outcome]:
    def check(state: _Declaration, context: _Context) -> _Outcome:
        return None if state.raw[field_name].strip() else _violation(state, kind, field_name)

    return check


def _check_selector_string(state: _Declaration, context: _Context) -> _Outcome:
    if isinstance(state.raw.get("selector"), str):
        return None
    if "selector" in state.raw:
        return _violation(state, "selector_not_string", "selector")
    return _STOP


def _check_root_known(state: _Declaration, context: _Context) -> _Outcome:
    if selector_root(state.raw["selector"]) is not None:
        return None
    return _violation(state, "root_unknown", "selector")


def _check_root_mismatch(state: _Declaration, context: _Context) -> _Outcome:
    raw = state.raw
    text = selector_root_mismatch(raw.get("name"), raw.get("source_kind"), raw["selector"])
    return None if text is None else _violation(state, "root_mismatch", "selector", text=text)


def _check_source(state: _Declaration, context: _Context) -> _Outcome:
    raw = state.raw
    schema, violation = resolve_source(
        raw["source_kind"],
        raw["source_ref"],
        raw.get("selector"),
        inventory=context.inventory,
        runtime_contract=context.runtime_contract,
        policy=context.policy,
    )
    state.schema = schema
    if violation is None:
        return None
    return [replace(violation, index=state.index, name=raw.get("name"))]


def _check_schema(state: _Declaration, context: _Context) -> _Outcome:
    return None if state.schema is not None else _STOP


def _check_array_step(state: _Declaration, context: _Context) -> _Outcome:
    selector = state.raw["selector"]
    path = selector_array_step(state.schema, selector)
    if path is None:
        return None
    return _violation(state, "array_step", "selector", array_path=path, selector=selector)


def _check_selector_documented(state: _Declaration, context: _Context) -> _Outcome:
    selector = state.raw["selector"]
    if context.policy.empty_selector_parts_undocumented and not all(selector.split(".")):
        state.actual = None
    else:
        state.actual = selector_type(state.schema, selector)
    if state.actual is not None:
        return None
    return _violation(state, "selector_undocumented", "selector", selector=selector)


def _check_type(state: _Declaration, context: _Context) -> _Outcome:
    expected = state.raw["expected_type"]
    if types_compatible(state.actual, expected):
        return None
    return _violation(state, "type_mismatch", "selector", expected=expected, actual=state.actual)


def _check_record_key(state: _Declaration, context: _Context) -> _Outcome:
    """Reject a record-key companion binding that does not select the key string.

    The companion's records are ``{"record_key": key}`` wrappers, so any other
    selector yields a wrapper that carries none of the record's fields.
    """

    raw = state.raw
    canonical_paths = context.policy.canonical_paths
    if raw["source_kind"] != "supplied_input" or canonical_paths is None:
        return None
    source_ref, selector = canonical_paths(
        raw["source_kind"], raw["source_ref"], raw["selector"], context.inventory
    )
    reference = source_ref.removeprefix("facts:")
    fact = first_fact_named(context.inventory, reference)
    provenance = fact.get("provenance") if fact is not None else None
    if not isinstance(provenance, dict) or provenance.get("derivation") != "keyed_map_record_key":
        return None
    parts = selector.split(".")
    if len(parts) == 3 and parts[0] == "value" and parts[2] == "record_key":
        return None
    base = provenance.get("derived_from") or reference.removesuffix(":records")
    return _violation(
        state, "record_key_selector", "selector", selector=selector, reference=reference, base=base
    )


_CHECKS: dict[str, Callable[[_Declaration, _Context], _Outcome]] = {
    "object": _check_object,
    "fields": _check_fields,
    "string_fields": _check_string_fields,
    "consumers_list": _check_consumers_list,
    "consumers_empty": _check_consumers_empty,
    "consumer_items": _check_consumer_items,
    "consumer_blank": _check_consumer_blank,
    "consumer_closed": _check_consumer_closed,
    "duplicate": _check_duplicate,
    "name_or_duplicate": _check_name_or_duplicate,
    "name_blank": _check_name_blank,
    "expected_type": _closed_check("expected_type", CLOSED_TYPES),
    "source_kind": _closed_check("source_kind", SOURCE_KINDS),
    "on_missing": _closed_check("on_missing", MISSING_POLICIES),
    "source_ref_blank": _blank_check("source_ref", "source_reference_blank"),
    "selector_blank": _blank_check("selector", "selector_blank"),
    "selector_string": _check_selector_string,
    "root_known": _check_root_known,
    "root_mismatch": _check_root_mismatch,
    "source": _check_source,
    "if_schema": _check_schema,
    "array_step": _check_array_step,
    "selector_documented": _check_selector_documented,
    "type": _check_type,
    "record_key": _check_record_key,
}

_PACKAGE_READABLE: Chain = ("object", "fields", "consumers_list", "consumer_blank")
# A package declaration is checked as preparation reads it: one chain per
# declaration, in declaration order, stopping at the first violation.
PACKAGE_POLICY = BindingPolicy(
    readable=_PACKAGE_READABLE,
    phases=(
        (
            (
                *_PACKAGE_READABLE,
                "name_or_duplicate",
                "expected_type",
                "source_kind",
                "on_missing",
                "consumers_empty",
                "consumer_closed",
                "root_mismatch",
                "source",
                "array_step",
                "selector_documented",
                "type",
            ),
        ),
    ),
)
# The reason preparation reports for each violation; any other is binding_invalid.
_PACKAGE_MISSING = frozenset(
    {"fields", "on_missing_not_closed", "schema_missing", "selector_undocumented"}
)


def package_error(violation: Violation) -> BindingError:
    """Word a violation of ``PACKAGE_POLICY`` as the ``BindingError`` preparation reports."""

    name, details = violation.name, violation.details
    messages = {
        "not_list": "runtime_bindings must be a list",
        "not_object": "binding must be an object",
        "consumers_not_list": "binding consumers must be non-empty strings",
        "consumer_blank": "binding consumers must be non-empty strings",
        "consumers_empty": f"binding consumers must be non-empty strings: {name}",
        "name_invalid": f"duplicate or blank binding: {name!r}",
        "expected_type_not_closed": f"binding expected_type is not closed: {name}",
        "source_kind_not_closed": f"binding source_kind is not closed: {name}",
        "on_missing_not_closed": f"binding on_missing is not closed: {name}",
        "root_mismatch": details.get("text"),
        "source_ref_form": "setup binding source_ref must be setup:<operation>: "
        f"{details.get('source_ref')}",
        "unknown_operation": f"unknown setup operation: {details.get('reference')}",
        "operation_not_permitted": f"setup operation is not permitted: {details.get('reference')}",
        "schema_missing": f"missing source schema for binding: {name}",
        "selector_undocumented": f"undocumented selector for binding {name}: "
        f"{details.get('selector')}",
        "type_mismatch": f"binding type mismatch for {name}: expected "
        f"{details.get('expected')}, source is {details.get('actual')}",
    }
    if violation.kind == "fields":
        message = (
            f"binding fields invalid (missing={sorted(details['missing'])}, "
            f"unknown={sorted(details['unknown'])})"
        )
    elif violation.kind == "consumer_not_closed":
        message = f"binding consumer is not a closed path for {name}: {details['consumer']}"
    elif violation.kind == "array_step":
        message = (
            f"selector for binding {name} steps into the array at "
            f"{details['array_path']}: {details['selector']}"
        )
    else:
        message = messages[violation.kind]
    code = "binding_missing" if violation.kind in _PACKAGE_MISSING else "binding_invalid"
    return BindingError(message, code=code)


def parse_binding(value: Any) -> RuntimeBinding:
    """Read one package declaration: its field set and its consumer list."""

    read = read_binding(value, PACKAGE_POLICY)
    if isinstance(read, Violation):
        raise package_error(read)
    return read


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

    checked = check_declarations(
        declarations,
        inventory=inventory,
        runtime_contract=runtime_contract,
        policy=PACKAGE_POLICY,
    )
    if checked.violations:
        raise package_error(checked.violations[0])
    return checked.bindings


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
    "PACKAGE_POLICY",
    "PREREQUISITE_CONSUMERS",
    "SELECTOR_ROOTS",
    "SETUP_ARGUMENT_CONSUMERS",
    "SLOT_NAME",
    "SLOT_PATTERN",
    "SOURCE_KINDS",
    "UNDECLARED_SLOT",
    "BindingCheck",
    "BindingError",
    "BindingPolicy",
    "BindingSpec",
    "CanonicalPaths",
    "Chain",
    "ConsumerPrefix",
    "RuntimeBinding",
    "Violation",
    "check_declarations",
    "check_slot_reference",
    "declared_types",
    "first_fact_named",
    "package_error",
    "parse_binding",
    "read_binding",
    "resolve_source",
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
