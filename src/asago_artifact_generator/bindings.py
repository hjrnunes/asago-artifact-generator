"""Closed runtime binding declarations for authored artifact packages.

Bindings are declarations only.  This module never calls setup operations and
never evaluates template expressions; downstream execution resolves the exact
selectors after it has captured a permitted setup result.  The closed
vocabulary, the rules and the declaration-validation engine live in
``binding_rules``, which package preparation uses too.  This module adds
authoring's canonical paths, which run before the engine, the policy authoring
checks a declaration list with, and authoring's wording of each violation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .binding_rules import (
    BINDING_SPEC,
    CLOSED_TYPES,
    SLOT_PATTERN,
    BindingPolicy,
    BindingSpec,
    ConsumerPrefix,
    RuntimeBinding,
    Violation,
    check_declarations,
    first_fact_named,
    read_binding,
    selector_type,
    type_below_root,
)


class BindingValidationError(ValueError):
    """Raised when a binding or substitution is not closed and documented."""


def validate_bindings(
    declarations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    transformations: list[dict[str, Any]] | None = None,
) -> tuple[RuntimeBinding, ...]:
    """Validate and normalize declarations against exact supplied schemas.

    A mutable declaration list receives the canonicalized, de-duplicated wire
    form.  Exact duplicates are removed before duplicate names are checked;
    same-name declarations that differ in any field remain an error.
    """

    normalized_declarations = normalize_binding_declarations(
        declarations,
        inventory=inventory,
        transformations=transformations,
    )
    return check_bindings(
        normalized_declarations, inventory=inventory, runtime_contract=runtime_contract
    )


def check_bindings(
    declarations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> tuple[RuntimeBinding, ...]:
    """Validate declarations as written, without canonicalizing or de-duplicating them.

    The authoring checks read declarations the normalization step already
    rewrote; ``validate_bindings`` normalizes first, then checks the same way.
    """

    checked = check_declarations(
        declarations,
        inventory=inventory,
        runtime_contract=runtime_contract,
        policy=AUTHORING_POLICY,
    )
    if checked.violations:
        raise BindingValidationError(authoring_message(checked.violations[0]))
    return checked.bindings


def binding_from_dict(value: Any) -> RuntimeBinding:
    """Read an authored declaration: every field a string, every consumer closed."""

    read = read_binding(value, AUTHORING_POLICY)
    if isinstance(read, Violation):
        raise BindingValidationError(authoring_message(read))
    return read


_first_fact_named = first_fact_named


def canonical_binding_paths(
    source_kind: str,
    source_ref: str,
    selector: str,
    inventory: dict[str, Any],
) -> tuple[str, str]:
    """Resolve a keyed-map shorthand to the documented fact and selector.

    The model-facing wire documents ``facts:<ref>`` plus a dot selector.  A
    keyed-map shorthand may instead put the record key and field in the source
    reference, for example ``facts:state:loans:LN-201:borrower_id``.  This
    helper only canonicalizes a path when the referenced key and field exist
    in the supplied schemas; unresolved shorthands remain unchanged and fail
    the normal closed validation.
    """

    if source_kind != "supplied_input" or not source_ref.startswith("facts:"):
        return source_ref, selector
    reference = source_ref.removeprefix("facts:")
    fact_by_ref = _facts_by_ref(inventory)
    fallback_targets: set[tuple[str, str]] = set()
    if reference in fact_by_ref:
        exact = _exact_fact_paths(source_ref, reference, selector, fact_by_ref, fallback_targets)
        if exact is not None:
            return exact
    for companion_ref in sorted(fact_by_ref):
        if not companion_ref.endswith(":records"):
            continue
        resolved = _companion_shorthand_paths(
            reference, selector, companion_ref, fact_by_ref, fallback_targets
        )
        if resolved is not None:
            return resolved
    if len(fallback_targets) == 1:
        resolved_ref, resolved_selector = next(iter(fallback_targets))
        return f"facts:{resolved_ref}", resolved_selector
    return source_ref, selector


def _facts_by_ref(inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        item["ref"]: item
        for item in inventory.get("facts", [])
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
    }


def _exact_fact_paths(
    source_ref: str,
    reference: str,
    selector: str,
    fact_by_ref: dict[str, Any],
    fallback_targets: set[tuple[str, str]],
) -> tuple[str, str] | None:
    """Resolve a reference that names a supplied fact exactly.

    Returns None only for an undocumented selector on a records companion,
    after recording its base fact as a fallback when only the base documents
    the selector; the keyed shorthand search then continues.
    """

    exact_fact = fact_by_ref[reference]
    if (
        _schema_at_selector(
            exact_fact.get("schema", {}) if isinstance(exact_fact, dict) else {},
            selector,
        )
        is not None
    ):
        return source_ref, selector
    if not reference.endswith(":records"):
        return _record_key_companion_path(reference, selector, fact_by_ref) or (
            source_ref,
            selector,
        )
    base_ref = reference.removesuffix(":records")
    selector_parts = selector.split(".")
    if len(selector_parts) >= 2 and selector_parts[0] == "value":
        targets = _documented_targets(
            ((reference, exact_fact), (base_ref, fact_by_ref.get(base_ref))),
            selector,
        )
        if len(targets) == 1 and targets[0][0] == base_ref:
            fallback_targets.add((base_ref, selector))
    return None


def _companion_shorthand_paths(
    reference: str,
    selector: str,
    companion_ref: str,
    fact_by_ref: dict[str, Any],
    fallback_targets: set[tuple[str, str]],
) -> tuple[str, str] | None:
    """Resolve a keyed-map shorthand under one records companion and its base fact."""

    base_ref = companion_ref.removesuffix(":records")
    parsed = _keyed_source_suffix(reference, companion_ref, base_ref)
    if parsed is None:
        return None
    record_key, field = parsed
    original = fact_by_ref.get(base_ref)
    companion = fact_by_ref[companion_ref]
    companion_source = reference == companion_ref or reference.startswith(f"{companion_ref}:")
    sources = (
        ((companion_ref, companion), (base_ref, original))
        if companion_source
        else ((base_ref, original), (companion_ref, companion))
    )
    target_selector = _keyed_target_selector(selector, record_key, field)
    if target_selector is None:
        return None
    return _keyed_resolution(
        sources,
        companion_source=companion_source,
        companion_ref=companion_ref,
        base_ref=base_ref,
        selector=target_selector,
        fallback_targets=fallback_targets,
    )


def _keyed_target_selector(selector: str, record_key: str, field: str | None) -> str | None:
    """Return the full selector a keyed-map shorthand names, or None if it names none."""

    selector_parts = selector.split(".")
    if _selector_repeats_record_key(selector_parts, record_key, field):
        # The selector already names the full path from the fact root, so
        # the key in source_ref is redundant rather than a second level.
        return selector
    if field is None:
        if selector == "value":
            return f"value.{record_key}"
        if len(selector_parts) == 2 and selector_parts[0] == "value":
            return f"value.{record_key}.{selector_parts[1]}"
        return None
    if selector == "value" or selector_parts == ["value", field]:
        return f"value.{record_key}.{field}"
    return None


def _record_key_companion_path(
    reference: str,
    selector: str,
    fact_by_ref: dict[str, Any],
) -> tuple[str, str] | None:
    """Move a record-key selector written on a keyed fact to its records companion.

    ``value.<key>.record_key`` is documented only on ``<ref>:records`` unless the
    keyed fact's own record has that field, in which case the caller has already
    accepted the exact selector.
    """

    parts = selector.split(".")
    if len(parts) != 3 or parts[0] != "value" or parts[2] != "record_key":
        return None
    companion = fact_by_ref.get(f"{reference}:records")
    if not isinstance(companion, dict):
        return None
    provenance = companion.get("provenance")
    if isinstance(provenance, dict) and provenance.get("derivation") not in (
        None,
        "keyed_map_record_key",
    ):
        return None
    schema = companion.get("schema")
    if not isinstance(schema, dict) or _schema_at_selector(schema, selector) is None:
        return None
    return f"facts:{reference}:records", selector


def _selector_repeats_record_key(
    selector_parts: list[str],
    record_key: str,
    field: str | None,
) -> bool:
    """Return whether a selector below the record restates the shorthand key and field."""

    if len(selector_parts) < 3 or selector_parts[0] != "value":
        return False
    if selector_parts[1] != record_key:
        return False
    if field is None:
        return True
    field_parts = field.split(".")
    return selector_parts[2 : 2 + len(field_parts)] == field_parts


def _keyed_resolution(
    sources: tuple[tuple[str, dict[str, Any] | None], ...],
    *,
    companion_source: bool,
    companion_ref: str,
    base_ref: str,
    selector: str,
    fallback_targets: set[tuple[str, str]],
) -> tuple[str, str] | None:
    """Resolve one keyed-map target selector, or record it as a fallback candidate.

    A records-companion source resolves directly only when the companion
    documents the selector; otherwise its base fact becomes a fallback that
    applies only when it is the single candidate.
    """

    targets = _documented_targets(sources, selector)
    if not targets:
        return None
    resolved_ref = targets[0][0]
    if companion_source:
        if resolved_ref == companion_ref:
            return f"facts:{companion_ref}", selector
        fallback_targets.add((base_ref, selector))
        return None
    if fallback_targets:
        fallback_targets.add((resolved_ref, selector))
        return None
    return f"facts:{resolved_ref}", selector


def _documented_targets(
    sources: tuple[tuple[str, dict[str, Any] | None], ...],
    selector: str,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Return the source facts that document one selector."""

    return [
        (resolved_ref, resolved_fact)
        for resolved_ref, resolved_fact in sources
        if _schema_at_selector(
            resolved_fact.get("schema", {}) if isinstance(resolved_fact, dict) else {},
            selector,
        )
        is not None
    ]


def _record_binding_transformation(
    transformations: list[dict[str, Any]] | None,
    *,
    transformation: str,
    binding: str,
    original_source_ref: str,
    original_selector: str,
    canonical_source_ref: str,
    canonical_selector: str,
    **details: Any,
) -> None:
    """Append one deterministic binding rewrite record when requested."""

    if transformations is None:
        return
    transformations.append(
        {
            "transformation": transformation,
            "binding": binding,
            "original_source_ref": original_source_ref,
            "original_selector": original_selector,
            "canonical_source_ref": canonical_source_ref,
            "canonical_selector": canonical_selector,
            **details,
        }
    )


def normalize_binding_declarations(
    declarations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    inventory: dict[str, Any],
    transformations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Canonicalize paths and drop exact duplicate declaration objects.

    Invalid declaration shapes remain in the returned sequence so callers can
    report their structural findings instead of hiding them during repair.
    """

    if not isinstance(declarations, (list, tuple)):
        raise BindingValidationError("runtime_bindings must be a list")
    normalized: list[dict[str, Any]] = []
    first_indices: dict[RuntimeBinding, int] = {}
    for declaration_index, raw in enumerate(declarations):
        if not isinstance(raw, dict):
            normalized.append(raw)
            continue
        binding = read_binding(raw, AUTHORING_POLICY)
        if isinstance(binding, Violation):
            normalized.append(raw)
            continue
        original_source_ref = binding.source_ref
        original_selector = binding.selector
        canonical_source_ref, canonical_selector = canonical_binding_paths(
            binding.source_kind,
            binding.source_ref,
            binding.selector,
            inventory,
        )
        if (canonical_source_ref, canonical_selector) != (
            binding.source_ref,
            binding.selector,
        ):
            binding = replace(
                binding, source_ref=canonical_source_ref, selector=canonical_selector
            )
            _record_binding_transformation(
                transformations,
                transformation="binding_canonicalized",
                binding=binding.name,
                original_source_ref=original_source_ref,
                original_selector=original_selector,
                canonical_source_ref=canonical_source_ref,
                canonical_selector=canonical_selector,
            )
        if binding in first_indices:
            _record_binding_transformation(
                transformations,
                transformation="binding_duplicate_dropped",
                binding=binding.name,
                original_source_ref=original_source_ref,
                original_selector=original_selector,
                canonical_source_ref=binding.source_ref,
                canonical_selector=binding.selector,
                kept_index=first_indices[binding],
                dropped_index=declaration_index,
            )
            continue
        first_indices[binding] = declaration_index
        normalized.append(binding.to_dict())
    if isinstance(declarations, list):
        declarations[:] = normalized
    return normalized


def named_record_facts(
    source_kind: str,
    source_ref: str,
    inventory: dict[str, Any],
) -> tuple[str, tuple[tuple[str, dict[str, Any]], ...]] | None:
    """Return the record key a keyed-map shorthand names and the facts documenting it.

    ``facts:state:loans:LN-201`` names record ``LN-201`` of ``state:loans``
    and of its ``state:loans:records`` companion.  Each returned pair holds a
    fact reference and that record's own schema within the fact.  An exact
    fact reference names no record.
    """

    if source_kind != "supplied_input" or not source_ref.startswith("facts:"):
        return None
    reference = source_ref.removeprefix("facts:")
    fact_by_ref = _facts_by_ref(inventory)
    if reference in fact_by_ref:
        return None
    for companion_ref in sorted(fact_by_ref):
        if not companion_ref.endswith(":records"):
            continue
        base_ref = companion_ref.removesuffix(":records")
        parsed = _keyed_source_suffix(reference, companion_ref, base_ref)
        if parsed is None:
            continue
        record_key = parsed[0]
        documented = [
            (fact_ref, record_schema)
            for fact_ref in (base_ref, companion_ref)
            if (record_schema := _record_schema(fact_by_ref.get(fact_ref), record_key)) is not None
        ]
        if documented:
            return record_key, tuple(documented)
    return None


def _record_schema(fact: Any, record_key: str) -> dict[str, Any] | None:
    """Return the schema an object-typed fact documents for one record key."""

    schema = fact.get("schema") if isinstance(fact, dict) else None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if (
        isinstance(schema, dict)
        and schema.get("type") == "object"
        and isinstance(properties, dict)
        and isinstance(properties.get(record_key), dict)
    ):
        return properties[record_key]
    return None


def _keyed_source_suffix(
    reference: str,
    companion_ref: str,
    base_ref: str,
) -> tuple[str, str | None] | None:
    """Parse one keyed-map source shorthand against a known fact namespace."""

    prefix: str | None = None
    if reference.startswith(f"{companion_ref}:"):
        prefix = companion_ref
    elif reference.startswith(f"{base_ref}:"):
        prefix = base_ref
    if prefix is None:
        return None
    suffix = reference[len(prefix) + 1 :]
    if not suffix:
        return None
    if "." in suffix:
        record_key, field = suffix.split(".", 1)
    elif ":" in suffix:
        record_key, field = suffix.split(":", 1)
    else:
        record_key, field = suffix, None
    if not record_key or field == "":
        return None
    return record_key, field


def supplied_binding_values(
    declarations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve values that are available from supplied facts.

    Authoring can inspect supplied facts, but it cannot run setup operations.
    This helper therefore resolves only ``supplied_input`` declarations.  An
    invalid or unavailable source is skipped; the normal binding validator
    reports that structural error separately.  The declarations passed in are
    not changed.
    """

    if _supplied_facts_by_ref(declarations, inventory) is None:
        return {}
    return supplied_values_as_declared(
        normalize_binding_declarations(tuple(declarations), inventory=dict(inventory)),
        inventory,
    )


def supplied_values_as_declared(
    declarations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve supplied values of declarations as written, without de-duplicating them.

    The authoring checks read declarations the normalization step already
    rewrote; ``supplied_binding_values`` normalizes first, then resolves the
    same way.
    """

    fact_by_ref = _supplied_facts_by_ref(declarations, inventory)
    if fact_by_ref is None:
        return {}
    values: dict[str, Any] = {}
    for raw in declarations:
        declared = _supplied_declaration_paths(raw)
        if declared is None:
            continue
        name, source_ref, selector = declared
        canonical_source_ref, canonical_selector = canonical_binding_paths(
            "supplied_input", source_ref, selector, dict(inventory)
        )
        reference = canonical_source_ref.removeprefix("facts:")
        fact = fact_by_ref.get(reference)
        if not isinstance(fact, dict) or "value" not in fact:
            continue
        try:
            values[name] = _value_at_selector(fact["value"], canonical_selector)
        except (BindingValidationError, KeyError, IndexError, TypeError):
            continue
    return values


def _supplied_facts_by_ref(declarations: Any, inventory: Any) -> dict[str, Any] | None:
    if not isinstance(declarations, (list, tuple)) or not isinstance(inventory, Mapping):
        return None
    if not isinstance(inventory.get("facts", []), list):
        return None
    return _facts_by_ref(inventory)


def _supplied_declaration_paths(raw: Any) -> tuple[str, str, str] | None:
    """Return (name, source_ref, selector) of a well-formed supplied_input declaration."""

    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str):
        return None
    if raw.get("source_kind") != "supplied_input":
        return None
    source_ref = raw.get("source_ref")
    selector = raw.get("selector")
    if not isinstance(source_ref, str) or not isinstance(selector, str):
        return None
    return name, source_ref, selector


def find_stimulus_user_text_consumer_mismatches(
    declarations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    user_text: str,
    *,
    resolved_values: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Find bindings that claim user-text use without authored text use.

    Downstream dispatch treats ``stimulus.user_text`` as a request-record
    identity check.  A binding belongs in that consumer list only when the
    authored text contains its exact scalar value or a declared
    ``{{binding_name}}`` slot.  Missing values are reported rather than
    guessed, so setup-derived values without a slot also fail closed.
    """

    if not isinstance(user_text, str):
        return ()
    values = resolved_values if isinstance(resolved_values, Mapping) else {}
    slots = {match.group(1) for match in SLOT_PATTERN.finditer(user_text)}
    findings: list[dict[str, Any]] = []
    for binding_index, raw in enumerate(declarations):
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        consumers = raw.get("consumers")
        if not isinstance(name, str) or not isinstance(consumers, list):
            continue
        findings.extend(
            _user_text_consumer_findings(binding_index, name, consumers, slots, values, user_text)
        )
    return tuple(findings)


def _user_text_consumer_findings(
    binding_index: int,
    name: str,
    consumers: list[Any],
    slots: set[str],
    values: Mapping[str, Any],
    user_text: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for consumer_index, consumer in enumerate(consumers):
        if consumer != "stimulus.user_text":
            continue
        if name in slots:
            continue
        value = values.get(name)
        value_used = value is not None and str(value) in user_text
        if value_used:
            continue
        findings.append(
            {
                "binding_index": binding_index,
                "consumer_index": consumer_index,
                "binding_name": name,
                "value_available": name in values,
            }
        )
    return findings


def _value_at_selector(value: Any, selector: str) -> Any:
    """Resolve a canonical value-rooted selector against one fact value."""

    parts = selector.split(".")
    if not parts or parts[0] != "value":
        raise BindingValidationError(f"invalid supplied selector: {selector}")
    current = value
    for part in parts[1:]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise BindingValidationError(f"supplied selector not found: {selector}")
    return current


def _schema_at_selector(schema: dict[str, Any], selector: str) -> str | None:
    if not all(selector.split(".")):
        return None
    return selector_type(schema, selector)


def _binding_selector_type(schema: Any, selector: str) -> str | None:
    """Return the type a selector names below its root part, or None."""

    if not all(selector.split(".")):
        return None
    return type_below_root(schema, selector)


_AUTHORING_READABLE = (
    "object",
    "fields",
    "consumers_list",
    "consumer_blank",
    "string_fields",
    "consumer_closed",
)
# Authoring reads every declaration first, then checks the names, then checks each
# declaration's values, stopping at the first violation. It accepts an empty
# consumer list, and adds the canonical paths and the record-key companion rule.
AUTHORING_POLICY = BindingPolicy(
    readable=_AUTHORING_READABLE,
    phases=(
        (_AUTHORING_READABLE,),
        (("duplicate",),),
        (
            (
                "name_blank",
                "expected_type",
                "source_kind",
                "source_ref_blank",
                "selector_blank",
                "on_missing",
                "root_mismatch",
                "source",
                "selector_documented",
                "type",
                "record_key",
            ),
        ),
    ),
    canonical_paths=canonical_binding_paths,
    canonical_selector="declared",
    facts_prefix_required=True,
    unknown_fact_reported=True,
    permissions_must_be_list=False,
    empty_selector_parts_undocumented=True,
)


def authoring_message(violation: Violation) -> str:
    """Word a violation of ``AUTHORING_POLICY`` as authoring reports it."""

    name, details = violation.name, violation.details
    if violation.kind == "fields":
        return (
            f"binding fields invalid (missing={sorted(details['missing'])}, "
            f"unknown={sorted(details['unknown'])})"
        )
    if violation.kind == "source_ref_form":
        if details["source_kind"] == "setup_output":
            return f"setup binding source_ref must be setup:<operation>: {name}"
        return f"supplied binding source_ref must be facts:<ref>: {name}"
    if violation.kind == "record_key_selector":
        return (
            f"binding {name} selects {details['selector']} from facts:{details['reference']}, "
            f"whose records hold only record_key: select value.<key>.record_key there for the "
            f"key string, or bind facts:{details['base']} with value.<key> for the whole record "
            f"or value.<key>.<field> for one field"
        )
    return {
        "not_list": "runtime_bindings must be a list",
        "not_object": "binding must be an object",
        "consumers_not_list": "binding consumers must be non-empty strings",
        "consumer_blank": "binding consumers must be non-empty strings",
        "string_field": f"binding {violation.field} must be a string",
        "consumer_not_closed": "binding consumer is not a closed path",
        "duplicate": f"duplicate binding: {name}",
        "name_blank": "binding name is blank",
        "expected_type_not_closed": f"binding expected_type is not closed: {name}",
        "source_kind_not_closed": f"binding source_kind is not closed: {name}",
        "source_reference_blank": f"binding source reference is blank: {name}",
        "selector_blank": f"binding source reference is blank: {name}",
        "on_missing_not_closed": f"binding on_missing is not closed: {name}",
        "root_mismatch": details.get("text"),
        "unknown_operation": f"unknown setup operation: {details.get('reference')}",
        "operation_not_permitted": f"setup operation is not permitted: {details.get('reference')}",
        "unknown_fact": f"unknown supplied fact: {details.get('reference')}",
        "schema_missing": f"missing source schema for binding: {name}",
        "selector_undocumented": f"undocumented selector for binding {name}: "
        f"{details.get('selector')}",
        "type_mismatch": f"binding type mismatch for {name}: expected "
        f"{details.get('expected')}, source is {details.get('actual')}",
    }[violation.kind]


__all__ = [
    "AUTHORING_POLICY",
    "BINDING_SPEC",
    "BindingSpec",
    "BindingValidationError",
    "CLOSED_TYPES",
    "ConsumerPrefix",
    "RuntimeBinding",
    "authoring_message",
    "binding_from_dict",
    "canonical_binding_paths",
    "check_bindings",
    "find_stimulus_user_text_consumer_mismatches",
    "named_record_facts",
    "normalize_binding_declarations",
    "supplied_binding_values",
    "supplied_values_as_declared",
    "validate_bindings",
]
