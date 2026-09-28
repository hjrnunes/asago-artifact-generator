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

CLOSED_TYPES = frozenset({"array", "boolean", "integer", "number", "object", "string"})
SOURCE_KINDS = frozenset({"supplied_input", "setup_output"})
MISSING_POLICIES = frozenset({"inconclusive", "stop"})
CONSUMER_PREFIXES = ("detector.", "prerequisites.", "setup.arguments.")
_SLOT_RE = re.compile(r"\{\{([^{}]*)\}\}")


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
        required = {
            "name",
            "expected_type",
            "source_kind",
            "source_ref",
            "selector",
            "consumers",
            "on_missing",
        }
        missing = required - set(value)
        unknown = set(value) - required
        if missing or unknown:
            raise BindingValidationError(
                f"binding fields invalid (missing={sorted(missing)}, unknown={sorted(unknown)})"
            )
        consumers = value["consumers"]
        if not isinstance(consumers, list) or not all(
            isinstance(item, str) and item.strip() for item in consumers
        ):
            raise BindingValidationError("binding consumers must be non-empty strings")
        for field_name in (
            "name",
            "expected_type",
            "source_kind",
            "source_ref",
            "selector",
            "on_missing",
        ):
            if not isinstance(value[field_name], str):
                raise BindingValidationError(f"binding {field_name} must be a string")
        if any(
            item not in {"stimulus.user_text", "stimulus.history"}
            and not item.startswith(CONSUMER_PREFIXES)
            for item in consumers
        ):
            raise BindingValidationError("binding consumer is not a closed path")
        return cls(
            name=value["name"],
            expected_type=value["expected_type"],
            source_kind=value["source_kind"],
            source_ref=value["source_ref"],
            selector=value["selector"],
            consumers=tuple(consumers),
            on_missing=value["on_missing"],
        )


def validate_bindings(
    declarations: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> tuple[RuntimeBinding, ...]:
    """Validate declarations against exact supplied schemas and permissions."""

    if not isinstance(declarations, (list, tuple)):
        raise BindingValidationError("runtime_bindings must be a list")
    bindings: list[RuntimeBinding] = []
    names: set[str] = set()
    for raw in declarations:
        binding = RuntimeBinding.from_dict(raw)
        canonical_source_ref, canonical_selector = canonical_binding_paths(
            binding.source_kind,
            binding.source_ref,
            binding.selector,
            inventory,
        )
        if canonical_source_ref != binding.source_ref or canonical_selector != binding.selector:
            binding = RuntimeBinding(
                name=binding.name,
                expected_type=binding.expected_type,
                source_kind=binding.source_kind,
                source_ref=canonical_source_ref,
                selector=canonical_selector,
                consumers=binding.consumers,
                on_missing=binding.on_missing,
            )
        if binding.name in names:
            raise BindingValidationError(f"duplicate binding: {binding.name}")
        names.add(binding.name)
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
        schema = _source_schema(binding, inventory, runtime_contract)
        actual_type = _schema_at_selector(schema, binding.selector)
        if actual_type is None:
            raise BindingValidationError(
                f"undocumented selector for binding {binding.name}: {binding.selector}"
            )
        if not _types_compatible(actual_type, binding.expected_type):
            raise BindingValidationError(
                f"binding type mismatch for {binding.name}: "
                f"expected {binding.expected_type}, source is {actual_type}"
            )
        bindings.append(binding)
    return tuple(bindings)


def canonical_binding_paths(
    source_kind: str,
    source_ref: str,
    selector: str,
    inventory: dict[str, Any],
) -> tuple[str, str]:
    """Resolve a keyed-map shorthand to the documented fact and selector.

    The model-facing wire documents ``facts:<ref>`` plus a dot selector.  A
    keyed-map shorthand may instead put the record key and field in the source
    reference, for example ``facts:state:orders:ORD-201:customer_id``.  This
    helper only canonicalizes a path when the referenced key and field exist
    in the supplied schemas; unresolved shorthands remain unchanged and fail
    the normal closed validation.
    """

    if source_kind != "supplied_input" or not source_ref.startswith("facts:"):
        return source_ref, selector
    reference = source_ref.removeprefix("facts:")
    fact_by_ref = {
        item["ref"]: item
        for item in inventory.get("facts", [])
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
    }
    if reference in fact_by_ref:
        return source_ref, selector

    for companion_ref in sorted(fact_by_ref):
        if not companion_ref.endswith(":records"):
            continue
        base_ref = companion_ref.removesuffix(":records")
        parsed = _keyed_source_suffix(reference, companion_ref, base_ref)
        if parsed is None:
            continue
        record_key, field = parsed
        original = fact_by_ref.get(base_ref)
        companion = fact_by_ref[companion_ref]
        companion_source = reference.startswith(f"{companion_ref}:")
        if field is None:
            selector_parts = selector.split(".")
            if selector == "value":
                sources = (
                    ((companion_ref, companion), (base_ref, original))
                    if companion_source
                    else ((base_ref, original), (companion_ref, companion))
                )
                for resolved_ref, resolved_fact in sources:
                    if (
                        _schema_at_selector(
                            resolved_fact.get("schema", {})
                            if isinstance(resolved_fact, dict)
                            else {},
                            f"value.{record_key}",
                        )
                        is not None
                    ):
                        return f"facts:{resolved_ref}", f"value.{record_key}"
            elif len(selector_parts) == 2 and selector_parts[0] == "value":
                field = selector_parts[1]
                sources = (
                    ((companion_ref, companion), (base_ref, original))
                    if companion_source
                    else ((base_ref, original), (companion_ref, companion))
                )
                for resolved_ref, resolved_fact in sources:
                    if (
                        _schema_at_selector(
                            resolved_fact.get("schema", {})
                            if isinstance(resolved_fact, dict)
                            else {},
                            f"value.{record_key}.{field}",
                        )
                        is not None
                    ):
                        return f"facts:{resolved_ref}", f"value.{record_key}.{field}"
            continue
        if selector != "value":
            continue
        sources = (
            ((companion_ref, companion), (base_ref, original))
            if companion_source
            else ((base_ref, original), (companion_ref, companion))
        )
        for resolved_ref, resolved_fact in sources:
            if (
                _schema_at_selector(
                    resolved_fact.get("schema", {}) if isinstance(resolved_fact, dict) else {},
                    f"value.{record_key}.{field}",
                )
                is not None
            ):
                return f"facts:{resolved_ref}", f"value.{record_key}.{field}"
    return source_ref, selector


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


def substitute_slots(
    template: str,
    values: dict[str, Any],
    declarations: list[RuntimeBinding] | tuple[RuntimeBinding, ...],
) -> str:
    """Replace only declared ``{{name}}`` slots with already-resolved values."""

    if not isinstance(template, str):
        raise BindingValidationError("slot template must be a string")
    by_name = {binding.name: binding for binding in declarations}
    tokens = [match.group(1) for match in _SLOT_RE.finditer(template)]
    invalid = [token for token in tokens if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token)]
    if invalid:
        raise BindingValidationError(f"undeclared or invalid slot expression: {invalid[0]}")
    unknown = {token for token in tokens if token not in by_name}
    if unknown:
        raise BindingValidationError(f"undeclared slot: {sorted(unknown)[0]}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise BindingValidationError(f"missing bound value: {name}")
        value = values[name]
        if not _value_matches_type(value, by_name[name].expected_type):
            raise BindingValidationError(f"mistyped bound value: {name}")
        if isinstance(value, (dict, list)):
            raise BindingValidationError(f"non-scalar slot value: {name}")
        return str(value)

    return _SLOT_RE.sub(replace, template)


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

    if not isinstance(declarations, (list, tuple)) or not isinstance(inventory, Mapping):
        return {}
    facts = inventory.get("facts", [])
    if not isinstance(facts, list):
        return {}
    fact_by_ref = {
        item["ref"]: item
        for item in facts
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
    }
    values: dict[str, Any] = {}
    for raw in declarations:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str):
            continue
        if raw.get("source_kind") != "supplied_input":
            continue
        source_ref = raw.get("source_ref")
        selector = raw.get("selector")
        if not isinstance(source_ref, str) or not isinstance(selector, str):
            continue
        canonical_source_ref, canonical_selector = canonical_binding_paths(
            "supplied_input",
            source_ref,
            selector,
            dict(inventory),
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
    return tuple(findings)


def _value_at_selector(value: Any, selector: str) -> Any:
    """Resolve a canonical value-rooted selector against one fact value."""

    parts = selector.split(".")
    if not parts or parts[0] != "value":
        raise BindingValidationError(f"invalid supplied selector: {selector}")
    current = value
    for part in parts[1:]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part == "items":
            continue
        else:
            raise BindingValidationError(f"supplied selector not found: {selector}")
    return current


def _source_schema(
    binding: RuntimeBinding,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any]:
    if binding.source_kind == "setup_output":
        prefix, _, operation = binding.source_ref.partition(":")
        if prefix != "setup" or not operation:
            raise BindingValidationError(
                f"setup binding source_ref must be setup:<operation>: {binding.name}"
            )
        operations = inventory.get("operations", [])
        operation_record = next(
            (
                item
                for item in operations
                if isinstance(item, dict) and item.get("name") == operation
            ),
            None,
        )
        if operation_record is None:
            raise BindingValidationError(f"unknown setup operation: {operation}")
        permissions = runtime_contract.get("setup_permissions", [])
        if operation not in permissions:
            raise BindingValidationError(f"setup operation is not permitted: {operation}")
        schema = operation_record.get("result_schema")
    else:
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
        schema = fact.get("schema")
    if not isinstance(schema, dict):
        raise BindingValidationError(f"missing source schema for binding: {binding.name}")
    return schema


def _schema_at_selector(schema: dict[str, Any], selector: str) -> str | None:
    current: Any = schema
    parts = selector.split(".")
    if not parts or any(not part for part in parts):
        return None
    # Setup outputs are documented relative to ``result``; supplied facts use
    # ``value``.  Requiring the root makes accidental field-name inference fail.
    if parts[0] not in {"result", "value"}:
        return None
    for part in parts[1:]:
        if not isinstance(current, dict):
            return None
        if current.get("type") == "object":
            properties = current.get("properties")
            if not isinstance(properties, dict) or part not in properties:
                return None
            current = properties[part]
        elif current.get("type") == "array" and part == "items":
            current = current.get("items")
        else:
            return None
    return current.get("type") if isinstance(current, dict) else None


def _types_compatible(actual: str, expected: str) -> bool:
    if actual == expected:
        return True
    return actual == "integer" and expected == "number"


def _value_matches_type(value: Any, expected: str) -> bool:
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return False


__all__ = [
    "BindingValidationError",
    "CLOSED_TYPES",
    "RuntimeBinding",
    "canonical_binding_paths",
    "find_stimulus_user_text_consumer_mismatches",
    "supplied_binding_values",
    "substitute_slots",
    "validate_bindings",
]
