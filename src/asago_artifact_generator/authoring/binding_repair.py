"""Binding and reference repair options for correction prompts.

These helpers list documented selectors, source references, and consumers
that a correction may choose from; they never rewrite the plan themselves.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from ..bindings import CLOSED_TYPES, canonical_binding_paths, named_record_facts
from .checks import _binding_selector_type, _binding_source_schema, _binding_types_compatible
from .core import Call1FramingError, Finding
from .response_decode import _decode_v2_json_response

_BINDING_REPAIR_SELECTOR_LIMIT = 40
_BINDING_SELECTOR_FINDING_PATH = re.compile(r"^runtime_bindings\[(\d+)\]\.selector$")
_BINDING_SOURCE_REF_FINDING_PATH = re.compile(r"^runtime_bindings\[(\d+)\]\.source_ref$")
_BINDING_SOURCE_KIND_FINDING_PATH = re.compile(r"^runtime_bindings\[(\d+)\]\.source_kind$")
_UNKNOWN_BINDING_FINDING_PATH = re.compile(r"^prerequisites\[(\d+)\]\.binding$")
_BINDING_REPAIR_OPTIONS_DESCRIPTION = (
    "binding_repair_options is deterministic, source-derived assistance for binding "
    "findings. It is prompt context, not a new response field and not a recommended "
    "repair. Each option lists the exact source, selector, declaration, or consumer "
    "choices that the current validator accepts; choose among them using the scenario "
    "meaning and preserve supported content. If no listed option fits the scenario, "
    "the RESPONSE CONTRACT empty_value_guidance entries still apply. An equals value "
    "remains a JSON literal and still requires a declared binding."
)
_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS_V9 = {
    "description": (
        "Explains that this object is deterministic correction context, not a response "
        "field or a recommended repair."
    ),
    "field_descriptions": (
        "Maps each binding_repair_options field name to its model-facing meaning."
    ),
    "options": ("One deterministic entry for each binding-related finding that can be assisted."),
    "code": "The existing finding code; it does not change the finding or validation rules.",
    "path": "The exact existing finding path in the candidate plan.",
    "kind": ("The repair category: selector, unknown_binding, or consumer_mismatch."),
    "binding_name": "The candidate runtime binding or prerequisite binding name.",
    "expected_type": (
        "The candidate binding expected_type used for selector compatibility checks."
    ),
    "source_kind": ("The candidate binding source kind: supplied_input or setup_output."),
    "source_ref": "The candidate binding source_ref, when one is present.",
    "resolved_source": (
        "Whether source_kind and source_ref resolve to a documented source under the "
        "current inventory and runtime contract."
    ),
    "source_schema_type": (
        "The JSON type at the resolved source root, when the source schema declares one."
    ),
    "documented_selectors": (
        "A path-to-JSON-type mapping. Paths are the exact documented selectors accepted "
        "by the current selector validator, rooted at value for supplied_input or result "
        "for setup_output."
    ),
    "matching_expected_type": (
        "The documented selector paths whose JSON types are compatible with "
        "expected_type; integer is compatible with expected number."
    ),
    "no_matching_selector_note": (
        "Explicitly states that no enumerated documented selector produces the "
        "candidate expected type."
    ),
    "declared_binding_names": (
        "The currently declared runtime binding names, sorted deterministically; an "
        "empty list means no bindings are declared."
    ),
    "declared_binding_names_truncated": (
        "Whether the declared binding name list was capped at the deterministic enumeration limit."
    ),
    "declaration_requirement": (
        "The exact requirement for repairing unknown_binding: a runtime_bindings entry "
        "with the named binding whose consumers include prerequisites.<name>."
    ),
    "evidence_ref_sources": (
        "For each evidence_refs entry that exactly names a supplied fact, the "
        "corresponding facts:<ref> source and its documented selectors and JSON types."
    ),
    "evidence_ref_sources_truncated": (
        "Whether the supplied fact source list was capped at the deterministic enumeration limit."
    ),
    "evidence_ref": "The supplied fact reference copied from the prerequisite evidence_refs.",
    "required_consumer": ("The exact consumer path that the binding declaration must include."),
    "consumer_requirement": (
        "The exact action needed to repair consumer_mismatch without changing the consumer rule."
    ),
    "truncated": (
        "Whether the selector mapping was capped at the deterministic enumeration limit."
    ),
    "truncation_note": (
        "An explicit note when a deterministic enumeration exceeds 40 entries and "
        "only the first 40 sorted entries are shown."
    ),
}
_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS = {
    **_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS_V9,
    "findings": (
        "The grouped existing finding code and exact path entries for this binding, "
        "in finding order."
    ),
    "kind": ("The repair category: source, selector, unknown_binding, or consumer_mismatch."),
    "referenced_fact_sources": (
        "Bindable supplied fact sources that the candidate plan cites by exact "
        "facts:<ref> or <ref> value, with their documented selectors."
    ),
    "referenced_fact_sources_truncated": (
        "Whether the referenced supplied fact source list was capped at 40 entries."
    ),
    "permitted_setup_sources": (
        "Bindable setup operation results permitted by the runtime contract, with "
        "result-rooted documented selectors; an empty list means no setup operation "
        "is permitted."
    ),
    "permitted_setup_sources_truncated": (
        "Whether the permitted setup source list was capped at 40 entries."
    ),
    "other_fact_source_refs": (
        "Bindable supplied fact source names not cited by exact reference in the "
        "candidate plan, sorted and capped at 40."
    ),
    "other_fact_source_refs_truncated": (
        "Whether the other supplied fact source name list was capped at 40 entries."
    ),
    "supplied_value_empty": (
        "Present and true when the supplied fact value is an empty list or empty "
        "object; the source then contains nothing to bind."
    ),
    "supplied_value_empty_note": ("Names the empty supplied fact source and its empty shape."),
    "named_record_key": (
        "Present when source_ref names one record of a keyed supplied fact, as in "
        "facts:<ref>:<record_key>; the record key it names."
    ),
    "named_record_sources": (
        "Each supplied fact that documents the named record, listing only that record's "
        "selectors written in full from value; each source_ref and selector pair is "
        "accepted as written. A facts:<ref>:records source holds the record key itself "
        "at value.<record_key>.record_key."
    ),
}
_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS_V26 = {
    **_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS,
    "code": (
        "The existing finding code; it does not change the finding or validation rules. "
        "semantic_review marks a binding that a semantic review finding concerns."
    ),
    "path": (
        "The exact existing finding path in the candidate plan; for a review_binding "
        "option, the runtime binding that the review finding concerns."
    ),
    "kind": (
        "The repair category: source, selector, review_binding, unknown_binding, or "
        "consumer_mismatch. review_binding lists the documented choices for a binding "
        "that a semantic review finding concerns; when the binding selects inside one "
        "keyed record, it lists only that record's sources in named_record_sources."
    ),
    "selector": "The candidate binding selector, as written.",
    "named_record_key": (
        "Present when source_ref names one record of a keyed supplied fact, as in "
        "facts:<ref>:<record_key>, or when the selector selects inside one such "
        "record, as in value.<record_key>.<field>; the record key it names."
    ),
    "review_selector_checks": (
        "Each selector path that the review finding's required change names, checked "
        "by code against the documented selectors. documented_on_binding_source states "
        "whether the binding's current source_ref documents that selector; "
        "documented_source_refs lists every source_ref that documents it. A selector is "
        "valid only together with a listed source_ref; an empty list means no supplied "
        "source documents it."
    ),
}
_REVIEW_BINDING_LOCATION = re.compile(r"^(?:candidate_plan\.|plan\.)?runtime_bindings\[(\d+)\]")
_REVIEW_SELECTOR_TOKEN = re.compile(r"(?<![\w.:-])((?:value|result)(?:\.[A-Za-z0-9_-]+)+)")


def _correction_plan_candidate(correction_context: dict[str, Any]) -> dict[str, Any] | None:
    """Decode the failed plan only for deterministic correction assistance."""

    candidate = correction_context.get("current_output")
    if isinstance(candidate, dict):
        return deepcopy(candidate)
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    try:
        decoded, _ = _decode_v2_json_response(candidate.encode("utf-8"))
    except (Call1FramingError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _correction_binding_inputs(
    correction_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recover the source inventory and runtime contract from the author context."""

    original_context = correction_context.get("original_context")
    if not isinstance(original_context, dict):
        return {}, {}
    source_context = original_context.get("source_context")
    if not isinstance(source_context, dict):
        source_context = original_context.get("authoritative_context")
    if not isinstance(source_context, dict):
        source_context = {}
    inventory = {
        "facts": deepcopy(source_context.get("facts", [])),
        "operations": deepcopy(source_context.get("operations", [])),
    }
    execution_capabilities = original_context.get("execution_capabilities")
    runtime_contract = (
        execution_capabilities.get("runtime_contract")
        if isinstance(execution_capabilities, dict)
        else None
    )
    if not isinstance(runtime_contract, dict):
        runtime_contract = source_context.get("runtime_capabilities")
    return inventory, deepcopy(runtime_contract) if isinstance(runtime_contract, dict) else {}


def _documented_binding_selectors(
    schema: dict[str, Any],
    *,
    root: str,
    limit: int = _BINDING_REPAIR_SELECTOR_LIMIT,
) -> tuple[dict[str, str], bool]:
    """Enumerate the selectors accepted by ``_binding_selector_type``."""

    discovered: dict[str, str] = {}

    def visit(current: Any, path: str) -> None:
        if len(discovered) >= limit:
            return
        if not isinstance(current, dict):
            return
        current_type = current.get("type")
        if not isinstance(current_type, str):
            return
        discovered[path] = current_type
        if current_type == "object":
            properties = current.get("properties")
            if isinstance(properties, dict):
                for property_name in sorted(properties):
                    visit(properties[property_name], f"{path}.{property_name}")
        elif current_type == "array":
            visit(current.get("items"), f"{path}.items")

    visit(schema, root)
    has_more = False

    def count_paths(current: Any) -> int:
        if not isinstance(current, dict) or not isinstance(current.get("type"), str):
            return 0
        count = 1
        if current["type"] == "object" and isinstance(current.get("properties"), dict):
            count += sum(
                count_paths(current.get("properties", {}).get(name))
                for name in current["properties"]
            )
        elif current["type"] == "array":
            count += count_paths(current.get("items"))
        return count

    has_more = count_paths(schema) > limit
    return discovered, has_more


def _repair_truncation_note(label: str) -> str:
    return (
        f"{label} enumeration truncated after {_BINDING_REPAIR_SELECTOR_LIMIT} entries; "
        f"only the first {_BINDING_REPAIR_SELECTOR_LIMIT} sorted entries are shown."
    )


def _repair_selector_option(
    finding: Finding | dict[str, Any],
    binding: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    selected_record: bool = False,
) -> dict[str, Any] | None:
    """Build selector repair choices from the exact source schema.

    Returns None when the binding source does not resolve; an unresolved source
    is repaired through a source option instead.
    ``selected_record`` also lists the named record sources when the selector,
    rather than source_ref, names one keyed record.
    """

    code = finding.code if isinstance(finding, Finding) else finding.get("code")
    path = finding.path if isinstance(finding, Finding) else finding.get("path", "")
    name = binding.get("name")
    source_kind = binding.get("source_kind")
    source_ref = binding.get("source_ref")
    expected_type = binding.get("expected_type")
    option: dict[str, Any] = {
        "code": code,
        "path": path,
        "kind": "selector",
        "binding_name": name,
        "expected_type": expected_type,
        "source_kind": source_kind,
        "source_ref": source_ref,
    }
    schema = None
    if isinstance(source_kind, str) and isinstance(source_ref, str):
        schema, _ = _binding_source_schema(
            source_kind,
            source_ref,
            inventory,
            runtime_contract,
            name,
        )
    if schema is None:
        return None

    root = "value" if source_kind == "supplied_input" else "result"
    selectors, matching, truncated = _binding_selector_details(
        schema,
        root=root,
        expected_type=expected_type,
    )
    option.update(
        {
            "resolved_source": True,
            "source_schema_type": schema.get("type"),
            "documented_selectors": selectors,
            "matching_expected_type": matching,
            "truncated": truncated,
        }
    )
    option.update(_supplied_value_empty_fields(source_kind, source_ref, inventory))
    named_fields = _named_record_source_fields(source_kind, source_ref, expected_type, inventory)
    if not named_fields and selected_record:
        named_fields = _selected_record_source_fields(
            source_kind, source_ref, binding.get("selector"), expected_type, inventory
        )
    option.update(named_fields)
    option.update(_selector_notes(truncated, matching, source_ref, expected_type))
    return option


def _selector_notes(
    truncated: bool, matching: Any, source_ref: Any, expected_type: Any
) -> dict[str, str]:
    notes: dict[str, str] = {}
    if truncated:
        notes["truncation_note"] = (
            "Documented selector enumeration truncated after "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} selectors; only the first "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} sorted paths are shown."
        )
    if not matching:
        notes["no_matching_selector_note"] = (
            f"No documented selector of source {source_ref} yields expected_type {expected_type}."
        )
    return notes


def _first_fact_named(inventory: dict[str, Any], reference: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in inventory.get("facts", [])
            if isinstance(item, dict) and item.get("ref") == reference
        ),
        None,
    )


def _supplied_value_empty_fields(
    source_kind: Any,
    source_ref: Any,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """Mark a supplied fact whose captured value is an empty list or object.

    Its schema can still document ``value``, so the selector list alone does
    not show that the source holds nothing to bind.
    """

    if source_kind != "supplied_input" or not isinstance(source_ref, str):
        return {}
    canonical_ref, _ = canonical_binding_paths(source_kind, source_ref, "value", inventory)
    reference = canonical_ref.removeprefix("facts:")
    fact = _first_fact_named(inventory, reference)
    if not isinstance(fact, dict) or "value" not in fact:
        return {}
    value = fact["value"]
    if not isinstance(value, (list, dict)) or value:
        return {}
    shape = "list" if isinstance(value, list) else "object"
    return {
        "supplied_value_empty": True,
        "supplied_value_empty_note": (
            f"The supplied value of source facts:{reference} is an empty {shape}; "
            "it contains no element or field to bind."
        ),
    }


def _named_record_source_fields(
    source_kind: Any,
    source_ref: Any,
    expected_type: Any,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """List only the named record's selectors when source_ref names one keyed record.

    The whole-source enumeration is sorted and capped, so a named record late
    in a large keyed fact can fall outside it; the key itself lives only in
    the records companion fact.
    """

    if not isinstance(source_kind, str) or not isinstance(source_ref, str):
        return {}
    named = named_record_facts(source_kind, source_ref, inventory)
    if named is None:
        return {}
    record_key, facts = named
    sources: list[dict[str, Any]] = []
    for reference, record_schema in facts:
        fact_schema = next(
            item["schema"]
            for item in inventory.get("facts", [])
            if isinstance(item, dict) and item.get("ref") == reference
        )
        selectors, matching, truncated = _binding_selector_details(
            record_schema,
            root=f"value.{record_key}",
            expected_type=expected_type,
        )
        entry: dict[str, Any] = {
            "source_kind": "supplied_input",
            "source_ref": f"facts:{reference}",
            "source_schema_type": fact_schema.get("type"),
            "documented_selectors": selectors,
            "matching_expected_type": matching,
            "truncated": truncated,
        }
        if truncated:
            entry["truncation_note"] = (
                "Documented selector enumeration truncated after "
                f"{_BINDING_REPAIR_SELECTOR_LIMIT} selectors; only the first "
                f"{_BINDING_REPAIR_SELECTOR_LIMIT} sorted paths are shown."
            )
        sources.append(entry)
    return {"named_record_key": record_key, "named_record_sources": sources}


def _selected_record_source_fields(
    source_kind: Any,
    source_ref: Any,
    selector: Any,
    expected_type: Any,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """List the named record's sources when the selector selects inside one keyed record.

    A binding such as facts:<ref> with value.<record key>.<field> names the
    record in its selector; the record key string itself is documented only
    by the <ref>:records companion.
    """

    if (
        source_kind != "supplied_input"
        or not isinstance(source_ref, str)
        or not isinstance(selector, str)
    ):
        return {}
    canonical_ref, canonical_selector = canonical_binding_paths(
        source_kind, source_ref, selector, inventory
    )
    parts = canonical_selector.split(".")
    if len(parts) < 2 or parts[0] != "value" or not parts[1]:
        return {}
    return _named_record_source_fields(
        source_kind, f"{canonical_ref}:{parts[1]}", expected_type, inventory
    )


def _review_binding_schema(
    binding: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the schema of the binding's declared source, if it resolves."""

    source_kind = binding.get("source_kind")
    source_ref = binding.get("source_ref")
    if not (isinstance(source_kind, str) and isinstance(source_ref, str)):
        return None
    binding_schema, _ = _binding_source_schema(
        source_kind, source_ref, inventory, runtime_contract, binding.get("name")
    )
    return binding_schema


def _review_documented_sources(
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Return every supplied fact and permitted setup result with its schema."""

    return [
        *_documented_fact_sources(inventory),
        *_documented_setup_sources(inventory, runtime_contract),
    ]


def _documented_fact_sources(inventory: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"facts:{fact['ref']}", fact["schema"])
        for fact in inventory.get("facts", [])
        if isinstance(fact, dict)
        and isinstance(fact.get("ref"), str)
        and fact["ref"]
        and isinstance(fact.get("schema"), dict)
    ]


def _documented_setup_sources(
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    permitted = runtime_contract.get("setup_permissions", [])
    permitted_names = set(permitted) if isinstance(permitted, list) else set()
    return [
        (f"setup:{operation['name']}", operation["result_schema"])
        for operation in inventory.get("operations", [])
        if isinstance(operation, dict)
        and isinstance(operation.get("name"), str)
        and operation["name"] in permitted_names
        and isinstance(operation.get("result_schema"), dict)
    ]


def _review_selector_checks(
    required_change: str,
    binding: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    """Check the selector paths a review's required change names against documented sources.

    This is a structural lookup of exact dot paths; it makes no judgment about
    which selector the scenario needs.
    """

    selectors = list(dict.fromkeys(_REVIEW_SELECTOR_TOKEN.findall(required_change)))
    if not selectors:
        return []
    binding_schema = _review_binding_schema(binding, inventory, runtime_contract)
    documented_sources = _review_documented_sources(inventory, runtime_contract)
    checks: list[dict[str, Any]] = []
    for selector in selectors[:_BINDING_REPAIR_SELECTOR_LIMIT]:
        root = "value" if selector.startswith("value") else "result"
        refs = sorted(
            {
                reference
                for reference, schema in documented_sources
                if reference.startswith("facts:" if root == "value" else "setup:")
                and _binding_selector_type(schema, selector) is not None
            }
        )
        checks.append(
            {
                "selector": selector,
                "documented_on_binding_source": (
                    binding_schema is not None
                    and _binding_selector_type(binding_schema, selector) is not None
                ),
                "documented_source_refs": refs[:_BINDING_REPAIR_SELECTOR_LIMIT],
            }
        )
    return checks


def _review_binding_indices(
    finding: Finding | dict[str, Any],
    bindings: list[Any],
) -> list[int]:
    """Return the runtime bindings a semantic review finding points to.

    The finding's location pointer names one binding index, or its location
    or required change names declared binding identifiers exactly.
    """

    details = finding.details if isinstance(finding, Finding) else finding.get("details")
    if not isinstance(details, dict):
        return []
    location = _str_or_empty(details.get("review_location"))
    required_change = _str_or_empty(details.get("review_required_change"))
    match = _REVIEW_BINDING_LOCATION.match(location.strip())
    if match:
        index = int(match.group(1))
        return [index] if index < len(bindings) else []
    return _bindings_naming_identifier(bindings, location, required_change)


def _str_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _bindings_naming_identifier(
    bindings: list[Any], location: str, required_change: str
) -> list[int]:
    indices: list[int] = []
    for index, binding in enumerate(bindings):
        name = binding.get("name") if isinstance(binding, dict) else None
        if not isinstance(name, str) or not name:
            continue
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
        if pattern.search(location) or pattern.search(required_change):
            indices.append(index)
    return indices


def _repair_review_binding_option(
    finding: Finding | dict[str, Any],
    index: int,
    binding: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the documented choices for a binding that a semantic review finding concerns."""

    option = _repair_selector_option(
        {"code": "semantic_review", "path": f"runtime_bindings[{index}]"},
        binding,
        inventory,
        runtime_contract,
        selected_record=True,
    )
    if option is None:
        return None
    option["kind"] = "review_binding"
    option["selector"] = binding.get("selector")
    if "named_record_sources" in option:
        # The capped whole-source list mostly repeats other records' fields.
        for key in (
            "documented_selectors",
            "matching_expected_type",
            "truncated",
            "truncation_note",
            "no_matching_selector_note",
        ):
            option.pop(key, None)
    details = finding.details if isinstance(finding, Finding) else finding.get("details")
    required_change = details.get("review_required_change") if isinstance(details, dict) else None
    if isinstance(required_change, str):
        checks = _review_selector_checks(required_change, binding, inventory, runtime_contract)
        if checks:
            option["review_selector_checks"] = checks
    return option


def _binding_selector_details(
    schema: dict[str, Any],
    *,
    root: str,
    expected_type: Any,
) -> tuple[dict[str, str], list[str], bool]:
    """Return documented selectors and those compatible with one expected type."""

    selectors, truncated = _documented_binding_selectors(schema, root=root)
    matching = [
        selector
        for selector, actual_type in selectors.items()
        if isinstance(expected_type, str)
        and expected_type in CLOSED_TYPES
        and _binding_types_compatible(actual_type, expected_type)
    ]
    return selectors, matching, truncated


def _supplied_fact_selector_source(
    reference: str,
    inventory: dict[str, Any],
) -> dict[str, Any] | None:
    """Return one evidence fact's selector choices, if it is bindable."""

    fact = next(
        (
            item
            for item in inventory.get("facts", [])
            if isinstance(item, dict) and item.get("ref") == reference
        ),
        None,
    )
    if not isinstance(fact, dict) or not isinstance(fact.get("schema"), dict):
        return None
    selectors, _, truncated = _binding_selector_details(
        fact["schema"],
        root="value",
        expected_type=None,
    )
    result: dict[str, Any] = {
        "evidence_ref": reference,
        "source_kind": "supplied_input",
        "source_ref": f"facts:{reference}",
        "source_schema_type": fact["schema"].get("type"),
        "documented_selectors": selectors,
        "truncated": truncated,
    }
    if truncated:
        result["truncation_note"] = (
            "Documented selector enumeration truncated after "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} selectors; only the first "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} sorted paths are shown."
        )
    return result


def _repair_unknown_binding_option(
    finding: Finding | dict[str, Any],
    prerequisite: dict[str, Any],
    candidate: dict[str, Any],
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """Build declaration and evidence-source choices for an unknown binding."""

    code = finding.code if isinstance(finding, Finding) else finding.get("code")
    path = finding.path if isinstance(finding, Finding) else finding.get("path", "")
    name = prerequisite.get("binding")
    declared_bindings = candidate.get("runtime_bindings", [])
    all_declared_names = sorted(
        {
            item.get("name")
            for item in declared_bindings
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
    )
    declared_names = all_declared_names[:_BINDING_REPAIR_SELECTOR_LIMIT]
    declared_names_truncated = len(all_declared_names) > _BINDING_REPAIR_SELECTOR_LIMIT
    evidence_sources = _prerequisite_evidence_sources(prerequisite, inventory)
    evidence_sources_truncated = len(evidence_sources) > _BINDING_REPAIR_SELECTOR_LIMIT
    option = {
        "code": code,
        "path": path,
        "kind": "unknown_binding",
        "binding_name": name,
        "declared_binding_names": declared_names,
        "declared_binding_names_truncated": declared_names_truncated,
        "declaration_requirement": (
            f'Add a runtime_bindings entry with name "{name}" whose consumers '
            f'include "prerequisites.{name}".'
        ),
        "evidence_ref_sources": evidence_sources[:_BINDING_REPAIR_SELECTOR_LIMIT],
        "evidence_ref_sources_truncated": evidence_sources_truncated,
    }
    labels = _truncated_labels(
        (declared_names_truncated, "binding name"),
        (evidence_sources_truncated, "supplied fact source"),
    )
    if labels:
        option["truncation_note"] = _repair_truncation_note(" and ".join(labels))
    return option


def _prerequisite_evidence_sources(
    prerequisite: dict[str, Any],
    inventory: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return selector choices for each bindable fact a prerequisite cites."""

    evidence_sources: list[dict[str, Any]] = []
    evidence_refs = prerequisite.get("evidence_refs", [])
    if isinstance(evidence_refs, list):
        for reference in sorted({ref for ref in evidence_refs if isinstance(ref, str)}):
            source = _supplied_fact_selector_source(reference, inventory)
            if source is not None:
                evidence_sources.append(source)
    return evidence_sources


def _truncated_labels(*flags: tuple[bool, str]) -> list[str]:
    """Return the labels whose truncation flag is set, in argument order."""

    return [label for truncated, label in flags if truncated]


def _repair_consumer_mismatch_option(
    finding: Finding | dict[str, Any],
    prerequisite: dict[str, Any],
) -> dict[str, Any]:
    """Build the exact consumer repair for one prerequisite binding."""

    code = finding.code if isinstance(finding, Finding) else finding.get("code")
    path = finding.path if isinstance(finding, Finding) else finding.get("path", "")
    name = prerequisite.get("binding")
    consumer = f"prerequisites.{name}"
    return {
        "code": code,
        "path": path,
        "kind": "consumer_mismatch",
        "binding_name": name,
        "required_consumer": consumer,
        "consumer_requirement": (
            f'Add "{consumer}" to the consumers list of the "{name}" runtime binding.'
        ),
    }


def _repair_source_entry(
    *,
    source_kind: str,
    source_ref: str,
    schema: dict[str, Any],
    expected_type: Any,
) -> dict[str, Any]:
    """Build one source choice with selectors rooted at its source result."""

    root = "value" if source_kind == "supplied_input" else "result"
    selectors, matching, truncated = _binding_selector_details(
        schema,
        root=root,
        expected_type=expected_type,
    )
    entry: dict[str, Any] = {
        "source_kind": source_kind,
        "source_ref": source_ref,
        "source_schema_type": schema.get("type"),
        "documented_selectors": selectors,
        "matching_expected_type": matching,
        "truncated": truncated,
    }
    if truncated:
        entry["truncation_note"] = (
            "Documented selector enumeration truncated after "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} selectors; only the first "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} sorted paths are shown."
        )
    return entry


def _candidate_string_values(candidate: dict[str, Any]) -> set[str]:
    """Return every string value in a candidate plan for exact citation checks."""

    values: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            values.add(value)

    visit(candidate)
    return values


def _repair_source_option(
    findings: list[Finding | dict[str, Any]],
    binding: dict[str, Any],
    candidate: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any]:
    """Build merged source repair choices for one runtime binding."""

    source_kind = binding.get("source_kind")
    source_ref = binding.get("source_ref")
    binding_name = binding.get("name")
    expected_type = binding.get("expected_type")
    resolved_schema: dict[str, Any] | None = None
    if isinstance(source_kind, str) and isinstance(source_ref, str):
        resolved_schema, _ = _binding_source_schema(
            source_kind,
            source_ref,
            inventory,
            runtime_contract,
            binding_name,
        )

    referenced_fact_sources, other_fact_source_refs = _fact_repair_sources(
        _candidate_string_values(candidate), inventory, expected_type
    )
    permitted_setup_sources = _permitted_setup_repair_sources(
        inventory, runtime_contract, expected_type
    )

    referenced_fact_sources_truncated = (
        len(referenced_fact_sources) > _BINDING_REPAIR_SELECTOR_LIMIT
    )
    permitted_setup_sources_truncated = (
        len(permitted_setup_sources) > _BINDING_REPAIR_SELECTOR_LIMIT
    )
    other_fact_source_refs_truncated = len(other_fact_source_refs) > _BINDING_REPAIR_SELECTOR_LIMIT
    option: dict[str, Any] = {
        "kind": "source",
        "findings": [
            {"code": _finding_field(finding, "code"), "path": _finding_field(finding, "path", "")}
            for finding in findings
        ],
        "binding_name": binding_name,
        "expected_type": expected_type,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "resolved_source": resolved_schema is not None,
        "referenced_fact_sources": referenced_fact_sources[:_BINDING_REPAIR_SELECTOR_LIMIT],
        "referenced_fact_sources_truncated": referenced_fact_sources_truncated,
        "permitted_setup_sources": permitted_setup_sources[:_BINDING_REPAIR_SELECTOR_LIMIT],
        "permitted_setup_sources_truncated": permitted_setup_sources_truncated,
        "other_fact_source_refs": other_fact_source_refs[:_BINDING_REPAIR_SELECTOR_LIMIT],
        "other_fact_source_refs_truncated": other_fact_source_refs_truncated,
        **_named_record_source_fields(source_kind, source_ref, expected_type, inventory),
    }
    truncated_labels = _truncated_labels(
        (referenced_fact_sources_truncated, "referenced fact source"),
        (permitted_setup_sources_truncated, "permitted setup source"),
        (other_fact_source_refs_truncated, "other fact source"),
    )
    if truncated_labels:
        option["truncation_note"] = _repair_truncation_note(" and ".join(truncated_labels))
    return option


def _fact_repair_sources(
    cited_values: set[str],
    inventory: dict[str, Any],
    expected_type: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split supplied facts into full choices for cited ones and refs for the rest."""

    referenced_fact_sources: list[dict[str, Any]] = []
    other_fact_source_refs: list[str] = []
    for fact in sorted(
        (
            item
            for item in inventory.get("facts", [])
            if (
                isinstance(item, dict)
                and isinstance(item.get("ref"), str)
                and item["ref"]
                and isinstance(item.get("schema"), dict)
            )
        ),
        key=lambda item: item["ref"],
    ):
        reference = fact["ref"]
        if reference in cited_values or f"facts:{reference}" in cited_values:
            referenced_fact_sources.append(
                {
                    **_repair_source_entry(
                        source_kind="supplied_input",
                        source_ref=f"facts:{reference}",
                        schema=fact["schema"],
                        expected_type=expected_type,
                    ),
                    **_supplied_value_empty_fields(
                        "supplied_input", f"facts:{reference}", inventory
                    ),
                }
            )
        else:
            other_fact_source_refs.append(f"facts:{reference}")
    return referenced_fact_sources, other_fact_source_refs


def _permitted_setup_repair_sources(
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    expected_type: Any,
) -> list[dict[str, Any]]:
    """Return one source choice per permitted setup operation with a result schema."""

    permitted_names = {
        operation
        for operation in runtime_contract.get("setup_permissions", [])
        if isinstance(operation, str)
    }
    permitted_setup_sources: list[dict[str, Any]] = []
    seen_operations: set[str] = set()
    for operation in sorted(
        (
            item
            for item in inventory.get("operations", [])
            if (
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"] in permitted_names
                and isinstance(item.get("result_schema"), dict)
            )
        ),
        key=lambda item: item["name"],
    ):
        operation_name = operation["name"]
        if operation_name in seen_operations:
            continue
        seen_operations.add(operation_name)
        permitted_setup_sources.append(
            _repair_source_entry(
                source_kind="setup_output",
                source_ref=f"setup:{operation_name}",
                schema=operation["result_schema"],
                expected_type=expected_type,
            )
        )
    return permitted_setup_sources


_REFERENCE_FIELD_PATTERNS = (
    ("interpretation.source_refs", re.compile(r"interpretation\.source_refs\[\d+\]")),
    ("selected_evidence[].ref", re.compile(r"selected_evidence\[\d+\](?:\.ref)?")),
    ("assumptions[].ref", re.compile(r"assumptions\[\d+\]\.ref")),
    ("prerequisites[].evidence_refs", re.compile(r"prerequisites\[\d+\]\.evidence_refs\[\d+\]")),
)
_REFERENCE_REPAIR_DESCRIPTION = (
    "Each option explains one unknown_reference finding: the rejected value, what "
    "kind of value it is, and the reference rule for its field from EVIDENCE "
    "REFERENCES in the original stage context. Replace the value with a listed "
    "reference that supports the same claim, keep a valid scenario lineage or "
    "attack-tree node ID in interpretation.source_refs or assumptions[].ref as "
    "permitted by that field, move an observation scope to required_observations, "
    "or remove the entry when no supplied reference supports it."
)
_REFERENCE_VALUE_KIND_REPAIRS = {
    "provenance_id": (
        "This is a scenario lineage or attack-tree node ID. It is valid only in "
        "interpretation.source_refs or assumptions[].ref; keep it at this field "
        "only when that field's rule permits provenance_ids."
    ),
    "observation_scope": (
        "This is an observation scope, not a reference. Declare the capture in "
        "required_observations and cite a supplied fact, source handle, or "
        "operation:<name> at this field."
    ),
    "unlisted": (
        "This value is not listed in EVIDENCE REFERENCES. Use an exact listed "
        "reference, or remove the entry when no supplied reference supports it."
    ),
}


def _listed_provenance_ids(references: dict[str, Any]) -> set[str]:
    """Return the provenance IDs listed in the rendered evidence references."""

    provenance = references.get("provenance_ids", {})
    listed = provenance.get("ids", {}) if isinstance(provenance, dict) else {}
    return set(listed) if isinstance(listed, dict) else set()


def _observation_scope_names(original: dict[str, Any]) -> set[str]:
    """Return the observation scopes, including the always-available message scopes."""

    capabilities = original.get("execution_capabilities")
    observation = capabilities.get("observation") if isinstance(capabilities, dict) else None
    scopes = set(observation) if isinstance(observation, dict) else set()
    scopes.update({"assistant_messages", "messages", "tool_calls"})
    return scopes


def _reference_field(path: Any) -> str | None:
    """Return the reference field rule name whose pattern matches a finding path."""

    return next(
        (
            name
            for name, pattern in _REFERENCE_FIELD_PATTERNS
            if isinstance(path, str) and pattern.fullmatch(path)
        ),
        None,
    )


def _reference_value_kind(value: str, provenance_ids: set[str], scopes: set[str]) -> str:
    """Classify a rejected reference value as provenance, observation scope, or unlisted."""

    if value in provenance_ids:
        return "provenance_id"
    if value in scopes or value.partition(":")[2] in scopes:
        return "observation_scope"
    return "unlisted"


def _reference_repair_option(
    finding: Any,
    provenance_ids: set[str],
    scopes: set[str],
    field_rules: Any,
) -> dict[str, Any] | None:
    """Explain one unknown_reference finding, or return None for any other finding."""

    if not isinstance(finding, dict) or finding.get("code") != "unknown_reference":
        return None
    path = finding.get("path")
    field = _reference_field(path)
    if field is None:
        return None
    detail = str(finding.get("detail", ""))
    value = detail.split(": ", 1)[1] if detail.startswith("unknown_reference: ") else detail
    kind = _reference_value_kind(value, provenance_ids, scopes)
    return {
        "path": path,
        "rejected_value": value,
        "rejected_value_kind": kind,
        "field_rule": field_rules.get(field),
        "repair": _REFERENCE_VALUE_KIND_REPAIRS[kind],
    }


def _reference_repair_options_for_correction(
    correction_context: dict[str, Any],
) -> dict[str, Any] | None:
    """Explain each rejected plan reference against the rendered reference rules."""

    original = correction_context.get("original_context")
    references = original.get("evidence_references") if isinstance(original, dict) else None
    if not isinstance(references, dict):
        return None
    provenance_ids = _listed_provenance_ids(references)
    scopes = _observation_scope_names(original)
    field_rules = references.get("field_rules", {})
    options: list[dict[str, Any]] = []
    for finding in correction_context.get("findings", []):
        option = _reference_repair_option(finding, provenance_ids, scopes, field_rules)
        if option is not None:
            options.append(option)
    if not options:
        return None
    return {
        "description": _REFERENCE_REPAIR_DESCRIPTION,
        "valid_provenance_ids": sorted(provenance_ids),
        "options": options,
    }


_BindingFindings = dict[int, dict[str, list[Finding | dict[str, Any]]]]
_BindingFindingOrder = dict[int, list[Finding | dict[str, Any]]]


def _finding_field(finding: Finding | dict[str, Any], name: str, default: Any = None) -> Any:
    return getattr(finding, name) if isinstance(finding, Finding) else finding.get(name, default)


def _group_binding_findings(
    findings: list[Finding | dict[str, Any]],
) -> tuple[_BindingFindings, _BindingFindingOrder]:
    """Group findings on a binding's source_ref, source_kind, or selector by binding index."""

    binding_findings: _BindingFindings = {}
    binding_finding_order: _BindingFindingOrder = {}
    for finding in findings:
        path = _finding_field(finding, "path", "")
        if not isinstance(path, str):
            continue
        for field_name, pattern in (
            ("source_ref", _BINDING_SOURCE_REF_FINDING_PATH),
            ("source_kind", _BINDING_SOURCE_KIND_FINDING_PATH),
            ("selector", _BINDING_SELECTOR_FINDING_PATH),
        ):
            match = pattern.fullmatch(path)
            if match:
                index = int(match.group(1))
                binding_findings.setdefault(index, {}).setdefault(field_name, []).append(finding)
                binding_finding_order.setdefault(index, []).append(finding)
                break
    return binding_findings, binding_finding_order


def _binding_field_repair_option(
    grouped: dict[str, list[Finding | dict[str, Any]]],
    ordered_findings: list[Finding | dict[str, Any]],
    binding: dict[str, Any],
    candidate: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the source option, or the selector option when the source resolves."""

    source_findings = grouped.get("source_ref", []) + grouped.get("source_kind", [])
    selector_findings = grouped.get("selector", [])
    source_schema = None
    source_kind = binding.get("source_kind")
    source_ref = binding.get("source_ref")
    name = binding.get("name")
    if isinstance(source_kind, str) and isinstance(source_ref, str):
        source_schema, _ = _binding_source_schema(
            source_kind,
            source_ref,
            inventory,
            runtime_contract,
            name,
        )
    if source_findings or source_schema is None:
        return _repair_source_option(
            ordered_findings,
            binding,
            candidate,
            inventory,
            runtime_contract,
        )
    if selector_findings:
        return _repair_selector_option(
            selector_findings[0],
            binding,
            inventory,
            runtime_contract,
            selected_record=True,
        )
    return None


def _binding_field_repair_options(
    binding_findings: _BindingFindings,
    binding_finding_order: _BindingFindingOrder,
    bindings: list[Any],
    candidate: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for index, grouped in binding_findings.items():
        if index >= len(bindings) or not isinstance(bindings[index], dict):
            continue
        option = _binding_field_repair_option(
            grouped,
            binding_finding_order[index],
            bindings[index],
            candidate,
            inventory,
            runtime_contract,
        )
        if option is not None:
            options.append(option)
    return options


def _review_binding_repair_options(
    findings: list[Finding | dict[str, Any]],
    bindings: list[Any],
    reviewed: set[int],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return one review_binding option per binding a semantic review finding concerns.

    *reviewed* holds the binding indices that already have an option; it is
    extended in place.
    """

    options: list[dict[str, Any]] = []
    for finding in findings:
        if _finding_field(finding, "code") != "semantic_review":
            continue
        for index in _review_binding_indices(finding, bindings):
            if index in reviewed or not isinstance(bindings[index], dict):
                continue
            review_option = _repair_review_binding_option(
                finding,
                index,
                bindings[index],
                inventory,
                runtime_contract,
            )
            if review_option is not None:
                reviewed.add(index)
                options.append(review_option)
    return options


def _prerequisite_binding_repair_options(
    findings: list[Finding | dict[str, Any]],
    prerequisites: list[Any],
    candidate: dict[str, Any],
    inventory: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return unknown_binding and consumer_mismatch options for prerequisite bindings."""

    options: list[dict[str, Any]] = []
    for finding in findings:
        code = _finding_field(finding, "code")
        path = _finding_field(finding, "path", "")
        prerequisite_match = (
            _UNKNOWN_BINDING_FINDING_PATH.fullmatch(path) if isinstance(path, str) else None
        )
        if not prerequisite_match or code not in {"unknown_binding", "consumer_mismatch"}:
            continue
        index = int(prerequisite_match.group(1))
        if index >= len(prerequisites) or not isinstance(prerequisites[index], dict):
            continue
        if code == "unknown_binding":
            options.append(
                _repair_unknown_binding_option(
                    finding,
                    prerequisites[index],
                    candidate,
                    inventory,
                )
            )
        else:
            options.append(
                _repair_consumer_mismatch_option(
                    finding,
                    prerequisites[index],
                )
            )
    return options


def _deduplicated_repair_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for option in options:
        if option.get("kind") == "source":
            findings_key = tuple(
                (item.get("code"), item.get("path"))
                for item in option.get("findings", [])
                if isinstance(item, dict)
            )
            key = (option.get("kind"), option.get("binding_name"), findings_key)
        else:
            key = (option.get("kind"), option.get("path"))
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(option)
    return deduplicated


def _binding_repair_options_for_correction(
    correction_context: dict[str, Any],
) -> dict[str, Any] | None:
    """Compute plan correction repair choices.

    The choices also cover bindings that semantic review findings concern and
    list the record sources of a selector that names one keyed record.
    """

    if correction_context.get("stage") != "plan":
        return None
    candidate = _correction_plan_candidate(correction_context)
    inventory, runtime_contract = _correction_binding_inputs(correction_context)
    options: list[dict[str, Any]] = []
    findings = correction_context.get("findings", [])
    if candidate is not None and isinstance(findings, list):
        bindings = candidate.get("runtime_bindings", [])
        prerequisites = candidate.get("prerequisites", [])
        binding_findings, binding_finding_order = _group_binding_findings(findings)
        if isinstance(bindings, list):
            options += _binding_field_repair_options(
                binding_findings,
                binding_finding_order,
                bindings,
                candidate,
                inventory,
                runtime_contract,
            )
            options += _review_binding_repair_options(
                findings, bindings, set(binding_findings), inventory, runtime_contract
            )
        if isinstance(prerequisites, list):
            options += _prerequisite_binding_repair_options(
                findings, prerequisites, candidate, inventory
            )
    deduplicated = _deduplicated_repair_options(options)
    if not deduplicated:
        return None
    return {
        "description": _BINDING_REPAIR_OPTIONS_DESCRIPTION,
        "field_descriptions": deepcopy(_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS_V26),
        "options": deduplicated,
    }
