"""Closed runtime binding declarations for authored artifact packages.

Bindings are declarations only.  This module never calls setup operations and
never evaluates template expressions; downstream execution resolves the exact
selectors after it has captured a permitted setup result.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
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
_SLOT_RE = re.compile(r"\{\{([^{}]*)\}\}")
_BINDING_FIELDS = frozenset(BINDING_SPEC.fields)


class BindingValidationError(ValueError):
    """Raised when a binding or substitution is not closed and documented."""


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
        return {
            "name": self.name,
            "expected_type": self.expected_type,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "selector": self.selector,
            "consumers": list(self.consumers),
            "on_missing": self.on_missing,
        }

    @classmethod
    def from_dict(cls, value: Any) -> RuntimeBinding:
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

    if not isinstance(declarations, (list, tuple)):
        raise BindingValidationError("runtime_bindings must be a list")
    normalized_declarations = normalize_binding_declarations(
        declarations,
        inventory=inventory,
        transformations=transformations,
    )
    parsed_bindings = [RuntimeBinding.from_dict(raw) for raw in normalized_declarations]
    names: set[str] = set()
    for binding in parsed_bindings:
        if binding.name in names:
            raise BindingValidationError(f"duplicate binding: {binding.name}")
        names.add(binding.name)
    bindings: list[RuntimeBinding] = []
    for binding in parsed_bindings:
        _require_closed_fields(binding)
        schema = _source_schema(binding, inventory, runtime_contract)
        actual_type = _schema_at_selector(schema, binding.selector)
        if actual_type is None:
            raise BindingValidationError(
                f"undocumented selector for binding {binding.name}: {binding.selector}"
            )
        if not _binding_types_compatible(actual_type, binding.expected_type):
            raise BindingValidationError(
                f"binding type mismatch for {binding.name}: "
                f"expected {binding.expected_type}, source is {actual_type}"
            )
        _require_record_key_selector(binding, inventory)
        bindings.append(binding)
    return tuple(bindings)


def _require_closed_fields(binding: RuntimeBinding) -> None:
    if not binding.name.strip():
        raise BindingValidationError("binding name is blank")
    if binding.expected_type not in CLOSED_TYPES:
        raise BindingValidationError(f"binding expected_type is not closed: {binding.name}")
    if binding.source_kind not in SOURCE_KINDS:
        raise BindingValidationError(f"binding source_kind is not closed: {binding.name}")
    if not binding.source_ref.strip() or not binding.selector.strip():
        raise BindingValidationError(f"binding source reference is blank: {binding.name}")
    if binding.on_missing not in MISSING_POLICIES:
        raise BindingValidationError(f"binding on_missing is not closed: {binding.name}")


def _require_record_key_selector(binding: RuntimeBinding, inventory: dict[str, Any]) -> None:
    """Reject a record-key companion binding that does not select the key string.

    The companion's records are ``{"record_key": key}`` wrappers, so any other
    selector yields a wrapper that carries none of the record's fields.
    """

    if binding.source_kind != "supplied_input":
        return
    source_ref, selector = canonical_binding_paths(
        binding.source_kind, binding.source_ref, binding.selector, inventory
    )
    reference = source_ref.removeprefix("facts:")
    provenance = _fact_provenance(inventory, reference)
    if not isinstance(provenance, dict):
        return
    if provenance.get("derivation") != "keyed_map_record_key":
        return
    parts = selector.split(".")
    if len(parts) == 3 and parts[0] == "value" and parts[2] == "record_key":
        return
    base = provenance.get("derived_from") or reference.removesuffix(":records")
    raise BindingValidationError(
        f"binding {binding.name} selects {selector} from facts:{reference}, whose records "
        f"hold only record_key: select value.<key>.record_key there for the key string, "
        f"or bind facts:{base} with value.<key> for the whole record or "
        f"value.<key>.<field> for one field"
    )


def _fact_provenance(inventory: dict[str, Any], reference: str) -> Any:
    fact = next(
        (
            item
            for item in inventory.get("facts", [])
            if isinstance(item, dict) and item.get("ref") == reference
        ),
        None,
    )
    return fact.get("provenance") if isinstance(fact, dict) else None


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
    seen: set[tuple[Any, ...]] = set()
    first_indices: dict[tuple[Any, ...], int] = {}
    for declaration_index, raw in enumerate(declarations):
        if not isinstance(raw, dict):
            normalized.append(raw)
            continue
        try:
            binding = RuntimeBinding.from_dict(raw)
        except BindingValidationError:
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
            binding = RuntimeBinding(
                name=binding.name,
                expected_type=binding.expected_type,
                source_kind=binding.source_kind,
                source_ref=canonical_source_ref,
                selector=canonical_selector,
                consumers=binding.consumers,
                on_missing=binding.on_missing,
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
        declaration = binding.to_dict()
        key = (
            binding.name,
            binding.expected_type,
            binding.source_kind,
            binding.source_ref,
            binding.selector,
            binding.consumers,
            binding.on_missing,
        )
        if key in seen:
            _record_binding_transformation(
                transformations,
                transformation="binding_duplicate_dropped",
                binding=binding.name,
                original_source_ref=original_source_ref,
                original_selector=original_selector,
                canonical_source_ref=binding.source_ref,
                canonical_selector=binding.selector,
                kept_index=first_indices[key],
                dropped_index=declaration_index,
            )
            continue
        seen.add(key)
        first_indices[key] = declaration_index
        normalized.append(declaration)
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
    reports that structural error separately.
    """

    fact_by_ref = _supplied_facts_by_ref(declarations, inventory)
    if fact_by_ref is None:
        return {}
    values: dict[str, Any] = {}
    normalized_declarations = normalize_binding_declarations(
        declarations,
        inventory=dict(inventory),
    )
    for raw in normalized_declarations:
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
    facts = inventory.get("facts", [])
    if not isinstance(facts, list):
        return None
    return {
        item["ref"]: item
        for item in facts
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
    }


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
    slots = {match.group(1) for match in _SLOT_RE.finditer(user_text)}
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


def _source_schema(
    binding: RuntimeBinding,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any]:
    if binding.source_kind == "setup_output":
        schema = _setup_output_schema(binding, inventory, runtime_contract)
    else:
        schema = _supplied_fact_schema(binding, inventory)
    if not isinstance(schema, dict):
        raise BindingValidationError(f"missing source schema for binding: {binding.name}")
    return schema


def _setup_output_schema(
    binding: RuntimeBinding,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> Any:
    """Return the result schema of the permitted setup operation a binding names."""

    prefix, _, operation = binding.source_ref.partition(":")
    if prefix != "setup" or not operation:
        raise BindingValidationError(
            f"setup binding source_ref must be setup:<operation>: {binding.name}"
        )
    operations = inventory.get("operations", [])
    operation_record = next(
        (item for item in operations if isinstance(item, dict) and item.get("name") == operation),
        None,
    )
    if operation_record is None:
        raise BindingValidationError(f"unknown setup operation: {operation}")
    permissions = runtime_contract.get("setup_permissions", [])
    if operation not in permissions:
        raise BindingValidationError(f"setup operation is not permitted: {operation}")
    return operation_record.get("result_schema")


def _supplied_fact_schema(binding: RuntimeBinding, inventory: dict[str, Any]) -> Any:
    """Return the schema of the supplied fact a binding's canonical source names."""

    source_ref, _ = canonical_binding_paths(
        binding.source_kind,
        binding.source_ref,
        binding.selector,
        inventory,
    )
    prefix, _, reference = source_ref.partition(":")
    if prefix != "facts" or not reference:
        raise BindingValidationError(
            f"supplied binding source_ref must be facts:<ref>: {binding.name}"
        )
    facts = inventory.get("facts", [])
    fact = next(
        (item for item in facts if isinstance(item, dict) and item.get("ref") == reference),
        None,
    )
    if fact is None:
        raise BindingValidationError(f"unknown supplied fact: {reference}")
    return fact.get("schema")


def _schema_at_selector(schema: dict[str, Any], selector: str) -> str | None:
    # Setup outputs are documented relative to ``result``; supplied facts use
    # ``value``.  Requiring the root makes accidental field-name inference fail.
    if selector.split(".", 1)[0] not in {"result", "value"}:
        return None
    return _binding_selector_type(schema, selector)


def _binding_selector_type(schema: Any, selector: str) -> str | None:
    """Return the type a selector names below its root part, or None."""

    parts = selector.split(".")
    if not all(parts):
        return None
    current: Any = schema
    for part in parts[1:]:
        current = _child_schema(current, part)
    return current.get("type") if isinstance(current, dict) else None


def _child_schema(current: Any, part: str) -> Any:
    """Return the schema one selector part names, or None when it names none.

    A non-dict schema documents no type, so returning None for it leaves the
    caller's result unchanged.
    """

    if not isinstance(current, dict):
        return None
    if current.get("type") == "object":
        properties = current.get("properties")
        if not isinstance(properties, dict) or part not in properties:
            return None
        return properties[part]
    return None


def selector_array_step(schema: Any, selector: str) -> str | None:
    """Return the selector prefix naming the array that ``selector`` steps into, or None.

    A selector follows object properties only. The ``items`` schema keyword is not
    a property, and a recorded value is never read through an array.
    """

    parts = selector.split(".")
    current = schema
    for index, part in enumerate(parts[1:], start=1):
        if isinstance(current, dict) and current.get("type") == "array" and part == "items":
            return ".".join(parts[:index])
        current = _child_schema(current, part)
    return None


def _binding_types_compatible(actual: str, expected: str) -> bool:
    return actual == expected or (actual == "integer" and expected == "number")


__all__ = [
    "BINDING_SPEC",
    "BindingSpec",
    "BindingValidationError",
    "CLOSED_TYPES",
    "RuntimeBinding",
    "canonical_binding_paths",
    "find_stimulus_user_text_consumer_mismatches",
    "named_record_facts",
    "normalize_binding_declarations",
    "supplied_binding_values",
    "validate_bindings",
]
