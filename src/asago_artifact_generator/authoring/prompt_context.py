"""Deterministic author context views: supplied facts and guidance."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from ..bindings import (
    BindingValidationError,
    _binding_selector_type,
    _first_fact_named,
    canonical_binding_paths,
    normalize_binding_declarations,
    supplied_binding_values,
    validate_bindings,
)
from ..input_adapter import V5_FIELDS, InputView, build_scenario_handoff_view
from .attack_path_outline import ATTACK_PATH_FORMAT, attack_path_outline, attack_path_provenance
from .checks import _collect_canonical_prerequisite_findings
from .contracts import (
    NEUTRAL_ESTABLISHED_OMISSION_OUTCOME_EXAMPLE,
    NEUTRAL_OMISSION_OUTCOME_EXAMPLE,
    NEUTRAL_PLAN_OUTCOME_EXAMPLE,
    PLAN_FIELD_MEANINGS,
    _call2_contract_v2,
    neutral_artifact_plan_v2,
    neutral_artifact_response_without_source,
)
from .core import _sha256
from .inventory import _inventory_fact_map, _inventory_references
from .planted_item import plan_record_choices, planted_text_limit, runtime_contract_for_prompt
from .sequential_turns import plan_response_contract, shape_delivery, turn_count
from .templates import prose


def _authoritative_context(
    view: InputView,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any]:
    """Build one source-derived context shared by authors and reviewers."""

    facts = [deepcopy(fact) for fact in inventory.get("facts", []) if isinstance(fact, dict)]
    source_handles = [
        deepcopy(handle)
        for handle in inventory.get("source_handles", [])
        if isinstance(handle, dict)
    ]
    operations = _explained_operations(inventory, None)
    return {
        "facts": facts,
        "operations": operations,
        "source_handles": source_handles,
        "runtime_capabilities": runtime_contract_for_prompt(runtime_contract),
    }


_PLAN_AUTHOR_GUIDANCE = prose("prompt_context/plan_author_guidance")
_CURRENT_PLAN_AUTHOR_GUIDANCE = prose("prompt_context/current_plan_author_guidance")
_DISCRIMINATING_CONDITION_GUIDANCE = prose("prompt_context/discriminating_condition_guidance")


_NOT_CALLED_CONDITION_GUIDANCE = " " + prose("prompt_context/not_called_condition_guidance")


_OMISSION_STIMULUS_AUTHOR_GUIDANCE = prose("prompt_context/omission_stimulus_author_guidance")
_OMISSION_STIMULUS_REVIEWER_GUIDANCE = prose("prompt_context/omission_stimulus_reviewer_guidance")


_ORDER_COMPARISON_MEANING = prose("prompt_context/order_comparison_meaning")


def _condition_comparisons(view: InputView) -> list[dict[str, Any]]:
    """Return the comparisons of the producer's condition that are objects."""

    condition = view.payload.get("discriminating_condition")
    comparisons = condition.get("comparisons") if isinstance(condition, dict) else None
    return [item for item in comparisons or [] if isinstance(item, dict)]


def _order_comparison_rule(view: InputView) -> dict[str, Any]:
    """Return the order comparisons of the producer's condition for the plan reviewer."""

    orders = [
        {key: item[key] for key in ("operation", "requires_prior", "same_argument") if key in item}
        for item in _condition_comparisons(view)
        if item.get("kind") == "order"
    ]
    if not orders:
        return {}
    return {"order_comparisons": {"meaning": _ORDER_COMPARISON_MEANING, "comparisons": orders}}


def _omission_trigger_rule(view: InputView) -> dict[str, str]:
    """Return the plan reviewer's omission-trigger rule when the condition omits a call."""

    if not _has_not_called_comparison(view):
        return {}
    return {"omission_trigger": _OMISSION_STIMULUS_REVIEWER_GUIDANCE}


def _condition_has_not_called(condition: Any) -> bool:
    comparisons = condition.get("comparisons") if isinstance(condition, dict) else None
    return isinstance(comparisons, list) and any(
        isinstance(item, dict) and item.get("kind") == "not_called" for item in comparisons
    )


def _has_discriminating_condition(view: InputView) -> bool:
    return view.payload.get("discriminating_condition") is not None


def _has_not_called_comparison(view: InputView) -> bool:
    return _condition_has_not_called(view.payload.get("discriminating_condition"))


def _discriminating_condition_rule(view: InputView) -> dict[str, str]:
    if not _has_discriminating_condition(view):
        return {}
    guidance = _DISCRIMINATING_CONDITION_GUIDANCE
    if _has_not_called_comparison(view):
        guidance += _NOT_CALLED_CONDITION_GUIDANCE
    return {"discriminating_condition": guidance}


def _neutral_outcome_example(view: InputView) -> str:
    if not _has_not_called_comparison(view):
        return NEUTRAL_PLAN_OUTCOME_EXAMPLE
    return (
        f"{NEUTRAL_PLAN_OUTCOME_EXAMPLE}\n\n{NEUTRAL_OMISSION_OUTCOME_EXAMPLE}"
        f"\n\n{NEUTRAL_ESTABLISHED_OMISSION_OUTCOME_EXAMPLE}"
    )


_ARTIFACT_AUTHOR_GUIDANCE = prose("prompt_context/artifact_author_guidance")


def _original_scenario_context(view: InputView) -> dict[str, Any]:
    """Return source-owned scenario meaning without answer-bearing fixtures."""

    meaning = _case_meaning(view)
    meaning["input_identity"] = _v2_input_projection(view)
    return meaning


def _permitted_status_operation(operation: Any, permitted: Any) -> str | None:
    """Return the name of a permitted operation whose result documents a string status."""

    if not isinstance(operation, dict):
        return None
    name = operation.get("name")
    result_schema = operation.get("result_schema")
    properties = result_schema.get("properties", {}) if isinstance(result_schema, dict) else {}
    status_schema = properties.get("status") if isinstance(properties, dict) else None
    if (
        not isinstance(name, str)
        or name not in permitted
        or not isinstance(status_schema, dict)
        or status_schema.get("type") != "string"
    ):
        return None
    return name


def _neutral_status_binding_example(
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any]:
    """Return one resolver-checked status binding example.

    An input without a usable permitted setup operation gets a resolver-checked
    example built from a supplied scalar fact instead of an empty illustration.
    """

    permitted = runtime_contract.get("setup_permissions", [])
    references = _inventory_references(inventory)
    for operation in inventory.get("operations", []):
        name = _permitted_status_operation(operation, permitted)
        if name is None:
            continue
        binding = {
            "name": "setup_status",
            "expected_type": "string",
            "source_kind": "setup_output",
            "source_ref": f"setup:{name}",
            "selector": "result.status",
            "consumers": ["prerequisites.setup_status"],
            "on_missing": "stop",
        }
        prerequisite = {
            "name": "setup_ready",
            "check": f"The {name} operation returned a ready result.",
            "evidence_refs": [f"operation:{name}"],
            "binding": "setup_status",
            "equals": "READY",
        }
        try:
            validate_bindings(
                [binding],
                inventory=inventory,
                runtime_contract=runtime_contract,
            )
        except BindingValidationError:
            continue
        if any(ref not in references for ref in prerequisite["evidence_refs"]):
            continue
        return {
            "runtime_bindings": [binding],
            "prerequisites": [prerequisite],
            "label": "case-permitted operation example",
            "explanation": prose(
                "prompt_context/neutral_binding_example/setup_operation_explanation"
            ),
        }
    example = _supplied_fact_binding_example(inventory, runtime_contract, references)
    if example is not None:
        return example
    return {
        "runtime_bindings": [],
        "prerequisites": [],
        "label": "generic illustration; no case operation is implied",
        "explanation": prose("prompt_context/neutral_binding_example/generic_explanation"),
    }


_SCALAR_SCHEMA_TYPES = frozenset({"boolean", "integer", "number", "string"})


def _scalar_fact_binding_name(fact: dict[str, Any]) -> str | None:
    """Return the binding name a supplied scalar fact yields, or None when it has none."""

    ref = fact.get("ref")
    schema = fact.get("schema")
    if (
        not isinstance(ref, str)
        or not isinstance(schema, dict)
        or schema.get("type") not in _SCALAR_SCHEMA_TYPES
        or "value" not in fact
    ):
        return None
    name = re.sub(r"[^A-Za-z0-9_]", "_", ref.rsplit(":", 1)[-1]).strip("_")
    if not name or not re.match(r"[A-Za-z_]", name):
        return None
    return name


def _supplied_fact_binding_example(
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    references: set[str],
) -> dict[str, Any] | None:
    """Build a binding and prerequisite over the first supplied scalar fact."""

    for fact in sorted(
        (item for item in inventory.get("facts", []) if isinstance(item, dict)),
        key=lambda item: str(item.get("ref")),
    ):
        name = _scalar_fact_binding_name(fact)
        if name is None:
            continue
        ref = fact["ref"]
        binding = {
            "name": name,
            "expected_type": fact["schema"]["type"],
            "source_kind": "supplied_input",
            "source_ref": f"facts:{ref}",
            "selector": "value",
            "consumers": [f"prerequisites.{name}"],
            "on_missing": "stop",
        }
        prerequisite = {
            "name": f"{name}_matches_supplied_fact",
            "check": f"The resolved {name} value equals the supplied fact {ref}.",
            "evidence_refs": [ref],
            "binding": name,
            "equals": deepcopy(fact["value"]),
        }
        try:
            validate_bindings([binding], inventory=inventory, runtime_contract=runtime_contract)
        except BindingValidationError:
            continue
        if _collect_canonical_prerequisite_findings([prerequisite], references, {name}, [binding]):
            continue
        return {
            "runtime_bindings": [binding],
            "prerequisites": [prerequisite],
            "label": prose("prompt_context/neutral_binding_example/supplied_fact_label"),
            "explanation": prose(
                "prompt_context/neutral_binding_example/supplied_fact_explanation",
                ref=ref,
                name=name,
            ),
        }
    return None


def _first_record_field(value: Any) -> tuple[Any, str] | None:
    """Return the first record key and its first field of a non-empty map of records."""

    if not _is_record_map(value):
        return None
    record_key = sorted(value, key=str)[0]
    fields = sorted(
        field for field in value[record_key] if isinstance(field, str) and field != "record_key"
    )
    return (record_key, fields[0]) if fields else None


def _is_record_map(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(value)
        and all(isinstance(record, dict) for record in value.values())
    )


def _keyed_record_example(ref: str, facts: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    selected = _first_record_field(facts[ref].get("value"))
    if selected is None:
        return None
    record_key, field = selected
    shorthand = {
        "record_field": (
            f"facts:{ref}:{record_key}:{field} -> facts:{ref} + value.{record_key}.{field}"
        )
    }
    companion_ref = f"{ref}:records"
    if companion_ref in facts:
        shorthand["record_key"] = (
            f"facts:{companion_ref}:{record_key}:record_key -> "
            f"facts:{companion_ref} + value.{record_key}.record_key"
        )
    return {
        "fact_ref": ref,
        "record_key": record_key,
        "field": field,
        "accepted_to_canonical": shorthand,
    }


def _keyed_map_binding_forms(inventory: dict[str, Any]) -> dict[str, Any]:
    """Render compact, source-derived keyed-record binding examples."""

    facts = {
        item["ref"]: item
        for item in inventory.get("facts", [])
        if isinstance(item, dict) and isinstance(item.get("ref"), str)
    }
    examples = [
        example
        for example in (_keyed_record_example(ref, facts) for ref in sorted(facts))
        if example is not None
    ]
    return {
        "rule": prose("prompt_context/keyed_map_binding_rule"),
        "examples": examples[:1],
    }


def _scenario_provenance_index(view: InputView) -> list[dict[str, Any]]:
    """Return lineage and attack-path node IDs with plain-text locations."""

    appearances: dict[str, list[str]] = {}

    def add(identifier: Any, location: str) -> None:
        if isinstance(identifier, str) and identifier.strip():
            places = appearances.setdefault(identifier, [])
            if location not in places:
                places.append(location)

    lineage = view.payload.get("lineage")
    if isinstance(lineage, dict):
        for key in sorted(lineage):
            label = " ".join(
                "IDs" if word == "ids" else "ID" if word == "id" else word
                for word in key.split("_")
            )
            value = lineage[key]
            for item in value if isinstance(value, list) else [value]:
                add(item, f"scenario lineage ({label})")
    # The path carries no lineage of its own, so the handoff names its scenario ID.
    add(view.payload.get("scenario_id"), "scenario handoff (scenario ID)")
    for identifier, node_id in attack_path_provenance(view.payload["attack_path"]):
        add(
            identifier,
            f"named by attack-path node {node_id}"
            if node_id is not None
            else "named by an attack-path node",
        )
    return [
        {"id": identifier, "appears_in": places}
        for identifier, places in sorted(appearances.items())
    ]


def scenario_provenance_ids(view: InputView) -> frozenset[str]:
    """Return provenance IDs valid in interpretation.source_refs."""

    return frozenset(item["id"] for item in _scenario_provenance_index(view))


def _string_field_values(items: Any, key: str) -> list[str]:
    return [
        item[key] for item in items if isinstance(item, dict) and isinstance(item.get(key), str)
    ]


def _plan_evidence_references(view: InputView, inventory: dict[str, Any]) -> dict[str, Any]:
    """Explain every citable reference form and where each form is valid."""

    facts = sorted(_string_field_values(inventory.get("facts", []), "ref"))
    handles = sorted(_string_field_values(inventory.get("source_handles", []), "ref"))
    operations = sorted(
        f"operation:{name}"
        for name in _string_field_values(inventory.get("operations", []), "name")
    )
    return {
        "purpose": prose("prompt_context/evidence_references/purpose"),
        "field_rules": {
            "interpretation.source_refs": prose(
                "prompt_context/evidence_references/interpretation_source_refs"
            ),
            "selected_evidence[].ref": prose(
                "prompt_context/evidence_references/selected_evidence_ref"
            ),
            "assumptions[].ref": prose("prompt_context/evidence_references/assumptions_ref"),
            "prerequisites[].evidence_refs": prose(
                "prompt_context/evidence_references/prerequisites_evidence_refs"
            ),
        },
        "not_references": [
            prose("prompt_context/evidence_references/not_observation_scopes"),
            prose("prompt_context/evidence_references/not_scenario_text"),
            prose("prompt_context/evidence_references/not_binding_forms"),
        ],
        "citable_references": {
            "facts": facts,
            "source_handles": handles,
            "operations": operations,
        },
        "provenance_ids": {
            "rule": prose("prompt_context/evidence_references/provenance_ids_rule"),
            "ids": {
                item["id"]: ", ".join(item["appears_in"])
                for item in _scenario_provenance_index(view)
            },
        },
    }


_SCENARIO_KIND_STIMULUS_RULES = {
    "adversarial": prose("prompt_context/scenario_kind_stimulus_rules/adversarial"),
    "functional": prose("prompt_context/scenario_kind_stimulus_rules/functional"),
}
_SCENARIO_KIND_UNAVAILABLE_RULE = prose("prompt_context/scenario_kind_unavailable_rule")


def _attack_path_design(path: dict[str, Any]) -> dict[str, Any]:
    """Project an ``attack-path-v1`` path as an outline of numbered parts."""

    return {"format": ATTACK_PATH_FORMAT, "outline": attack_path_outline(path)}


def _scenario_design(view: InputView) -> dict[str, Any]:
    """Project the producer's scenario kind and attack path for plan design."""

    kind = view.payload.get("kind")
    kind_value = kind if isinstance(kind, str) and kind in _SCENARIO_KIND_STIMULUS_RULES else None
    classification = view.payload.get("classification")
    return {
        "purpose": prose("prompt_context/scenario_design/purpose"),
        "kind": kind_value if kind_value is not None else "unavailable",
        "stimulus_rule": (
            _SCENARIO_KIND_STIMULUS_RULES[kind_value]
            if kind_value is not None
            else _SCENARIO_KIND_UNAVAILABLE_RULE
        ),
        "classification": (
            deepcopy(classification)
            if isinstance(classification, dict) and classification
            else {
                "status": "unavailable",
                "reason": prose("prompt_context/scenario_design/classification_unavailable"),
            }
        ),
        "attack_path": _attack_path_design(view.payload["attack_path"]),
    }


def _scenario_design_prompt_view(design: Any) -> Any:
    """Avoid repeating scenario meaning that already appears in the task context."""

    if not isinstance(design, dict):
        return design
    result = deepcopy(design)
    result["purpose"] = prose("prompt_context/scenario_design/attack_path_prompt_view_purpose")
    result.pop("classification", None)
    path = result.get("attack_path")
    if isinstance(path, dict):
        result["attack_path"] = {
            "note": prose("prompt_context/scenario_design/attack_path_note"),
            "outline": deepcopy(path.get("outline", [])),
        }
    return result


def build_plan_author_context(
    view: InputView,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any]:
    """Build the source-derived context for the plan author role."""

    response_contract = plan_response_contract(
        turn_count(view),
        shape_delivery(view),
        plan_record_choices(view, runtime_contract),
    )
    context = {
        "task": {
            "instruction": (
                prose("prompt_context/plan_author_instruction")
                + " "
                + _PLAN_AUTHOR_GUIDANCE
                + " "
                + _CURRENT_PLAN_AUTHOR_GUIDANCE
                + (
                    " " + _discriminating_condition_rule(view)["discriminating_condition"]
                    if _has_discriminating_condition(view)
                    else ""
                )
                + (
                    " " + _OMISSION_STIMULUS_AUTHOR_GUIDANCE
                    if _has_not_called_comparison(view)
                    else ""
                )
            ),
            "scenario": _original_scenario_context(view),
        },
        "source_context": _authoritative_context(view, inventory, runtime_contract),
        "execution_capabilities": {
            "available_operations": prose(
                "prompt_context/execution_capabilities/available_operations"
            ),
            "runtime_contract": prose("prompt_context/execution_capabilities/runtime_contract"),
            "target_access": runtime_contract.get("target_access", "downstream_only"),
            "setup_permissions": deepcopy(runtime_contract.get("setup_permissions", [])),
            "observation": deepcopy(runtime_contract.get("observation", {})),
            "limits": deepcopy(runtime_contract.get("limits", {})),
        },
        "field_guide": {
            "binding_meanings": {
                "source_ref": prose("prompt_context/field_guide/binding_meanings/source_ref"),
                "selector": prose("prompt_context/field_guide/binding_meanings/selector"),
                "name": prose("prompt_context/field_guide/binding_meanings/name"),
                "consumers": prose("prompt_context/field_guide/binding_meanings/consumers"),
                "binding": prose("prompt_context/field_guide/binding_meanings/binding"),
                "equals": prose("prompt_context/field_guide/binding_meanings/equals"),
                "assumptions": prose("prompt_context/field_guide/binding_meanings/assumptions"),
                "evidence_refs": prose(
                    "prompt_context/field_guide/binding_meanings/evidence_refs"
                ),
            },
            "reference_forms": {
                "evidence_citation": prose(
                    "prompt_context/field_guide/reference_forms/evidence_citation"
                ),
                "setup_binding_source": prose(
                    "prompt_context/field_guide/reference_forms/setup_binding_source"
                ),
                "plain_binding_name": prose(
                    "prompt_context/field_guide/reference_forms/plain_binding_name"
                ),
                "closed_consumer": prose(
                    "prompt_context/field_guide/reference_forms/closed_consumer"
                ),
                "slot": prose("prompt_context/field_guide/reference_forms/slot"),
            },
            "neutral_binding_example": _neutral_status_binding_example(
                inventory, runtime_contract
            ),
        },
        "plan_field_meanings": PLAN_FIELD_MEANINGS,
        "neutral_outcome_example": _neutral_outcome_example(view),
        "response_contract": {
            **response_contract,
            "example_response": neutral_artifact_plan_v2(),
        },
    }
    context["field_guide"]["keyed_map_path_forms"] = _keyed_map_binding_forms(inventory)
    context["evidence_references"] = _plan_evidence_references(view, inventory)
    design = _scenario_design(view)
    context["scenario_design"] = design
    context["task"]["scenario"]["classification"] = deepcopy(design["classification"])
    return context


_RESOLVED_BINDING_VALUES_MEANING = prose("prompt_context/resolved_binding_values_meaning")
_RESOLVED_BINDING_VALUES_INSTRUCTION = prose("prompt_context/resolved_binding_values_instruction")
_RECORD_KEY_SOURCE_INSTRUCTION = " " + prose("prompt_context/record_key_source_instruction")


def _record_key_source(
    source_ref: str, selector: str, inventory: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Return the documented pair that binds the key of the record a selector selects inside."""

    reference = source_ref.removeprefix("facts:")
    parts = selector.split(".")
    if reference.endswith(":records") or len(parts) < 2 or parts[0] != "value":
        return None
    record_key = parts[1]
    companion_ref = f"{reference}:records"
    companion = _first_fact_named(dict(inventory), companion_ref)
    if not isinstance(companion, dict) or not isinstance(companion.get("schema"), dict):
        return None
    key_selector = f"value.{record_key}.record_key"
    if _binding_selector_type(companion["schema"], key_selector) is None:
        return None
    return {
        "source_ref": f"facts:{companion_ref}",
        "selector": key_selector,
        "record_key": _record_key_value(companion.get("value"), record_key),
    }


def _record_key_value(value: Any, record_key: str) -> Any:
    """Return the record_key a record states for itself, else the key it sits under."""

    record = value.get(record_key) if isinstance(value, dict) else None
    resolved = record.get("record_key") if isinstance(record, dict) else None
    return resolved if resolved is not None else record_key


def _binding_values_instruction(values: Sequence[Mapping[str, Any]]) -> str:
    """Return the value_meaning instruction, with the record-key sentence when it applies."""

    if any("record_key_source" in entry for entry in values):
        return _RESOLVED_BINDING_VALUES_INSTRUCTION + _RECORD_KEY_SOURCE_INSTRUCTION
    return _RESOLVED_BINDING_VALUES_INSTRUCTION


def _resolved_supplied_binding_values(
    plan: Mapping[str, Any], inventory: Mapping[str, Any]
) -> dict[str, Any]:
    """Show the reviewer what each supplied binding actually resolves to."""

    declarations = [item for item in plan.get("runtime_bindings") or [] if isinstance(item, dict)]
    resolved = supplied_binding_values(declarations, inventory)
    values: list[dict[str, Any]] = []
    run_time: list[str] = []
    for declaration in normalize_binding_declarations(declarations, inventory=dict(inventory)):
        name = declaration.get("name")
        if not isinstance(name, str):
            continue
        if declaration.get("source_kind") == "setup_output":
            run_time.append(name)
            continue
        if name not in resolved:
            continue
        source_ref, selector = canonical_binding_paths(
            "supplied_input",
            str(declaration.get("source_ref")),
            str(declaration.get("selector")),
            dict(inventory),
        )
        entry = {
            "name": name,
            "source_ref": source_ref,
            "selector": selector,
            "resolved_value": deepcopy(resolved[name]),
        }
        record_key_source = _record_key_source(source_ref, selector, inventory)
        if record_key_source is not None:
            entry["record_key_source"] = record_key_source
        values.append(entry)
    return {
        "meaning": _RESOLVED_BINDING_VALUES_MEANING,
        "reviewer_instruction": _binding_values_instruction(values),
        "values": values,
        "resolved_at_run_time": run_time,
    }


def build_artifact_author_context(
    view: InputView,
    plan: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any]:
    """Build the immutable-plan context for the artifact author."""

    response_contract = deepcopy(
        _call2_contract_v2(
            plan, planted_max_chars=planted_text_limit(view, runtime_contract, plan)
        )
    )
    # The neutral example is rendered in its own section so the request has
    # one readable copy of it.
    response_contract.pop("neutral_example", None)
    context = {
        "original_scenario": _original_scenario_context(view),
        "authoritative_context": _authoritative_context(view, inventory, runtime_contract),
        "plan_field_meanings": PLAN_FIELD_MEANINGS,
        "accepted_plan": deepcopy(plan),
        "accepted_plan_read_only": True,
        "runtime_contract": runtime_contract_for_prompt(runtime_contract),
        "response_contract": response_contract,
        "neutral_example": {
            "artifact": neutral_artifact_response_without_source(),
            "label": "illustrative neutral example, not provider output",
        },
    }
    context["evidence_references"] = _plan_evidence_references(view, inventory)
    context["semantic_judge_fact_ref_guidance"] = _semantic_judge_fact_ref_guidance(inventory)
    return context


def _plan_semantic_judge_needed(plan: Any) -> bool:
    """Return whether the accepted plan declares a semantic-judge stage."""

    if not isinstance(plan, dict):
        return False
    semantic_judge = plan.get("semantic_judge")
    return isinstance(semantic_judge, dict) and semantic_judge.get("needed") is True


def _semantic_judge_fact_ref_guidance(inventory: dict[str, Any]) -> dict[str, Any]:
    """Explain the authoritative fact-reference namespace for judge metadata."""

    fact_refs = list(_inventory_fact_map(inventory))
    return {
        "rule": prose("prompt_context/semantic_judge_fact_ref_rule"),
        "valid_fact_refs": fact_refs,
        "namespace": "inventory.facts[].ref",
    }


def _render_sections(
    sections: tuple[tuple[str, Any], ...],
    *,
    compact_titles: frozenset[str] = frozenset(),
) -> str:
    """Render ordered prompt sections with one readable value per section."""

    rendered: list[str] = []
    for title, value in sections:
        rendered.append(title)
        rendered.append(
            value
            if isinstance(value, str)
            else json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":") if title in compact_titles else None,
                indent=None if title in compact_titles else 2,
                sort_keys=True,
            )
        )
        rendered.append("")
    return "\n".join(rendered).rstrip() + "\n"


def _v2_input_projection(view: InputView) -> dict[str, Any]:
    """Return v2 input identity and digests without repeating case meaning."""

    return {
        "kind": view.kind.value,
        "scenario_id": view.scenario_id,
        "narrative_bytes_sha256": _sha256(view.narrative_bytes),
        "gherkin_bytes_sha256": _sha256(view.gherkin_bytes),
        "source_digests": dict(view.source_digests),
    }


def _case_meaning(view: InputView) -> dict[str, Any]:
    handoff = build_scenario_handoff_view(view)
    observation = handoff.get("observation")
    observation_level = (
        observation["assessment"]["disposition"]
        if isinstance(observation, dict)
        else "selected by the plan and bounded by runtime evidence"
    )
    result = {
        "scenario_id": view.scenario_id,
        "narrative": view.narrative,
        "gherkin": view.gherkin_text,
        "semantic_failure": handoff["semantic_failure_condition"],
        "safe_behavior": handoff["safe_alternative"],
        "observation_level": observation_level,
        "classification": {"family": None, "test_class": None, "adversary": None},
    }

    if observation is not None:
        result["observation"] = observation
    if "semantic_failure_call" in handoff:
        result["semantic_failure_call"] = handoff["semantic_failure_call"]
        result["semantic_failure_call_note"] = prose("prompt_context/semantic_failure_call_note")
    if "discriminating_condition" in handoff:
        result["discriminating_condition"] = _discriminating_condition_prompt_view(
            handoff["discriminating_condition"], view.gherkin_text
        )
    if "condition_check" in handoff:
        result["condition_check"] = _condition_check_prompt_view(handoff["condition_check"])
    result["structured_analysis"] = _structured_analysis(view)
    return result


_STRUCTURED_ANALYSIS_NOTE = prose("prompt_context/structured_analysis_note")


def _structured_analysis(view: InputView) -> dict[str, Any]:
    """Return the fields a v5 handoff adds, leaving out a null or empty one, with their note."""

    analysis: dict[str, Any] = {"note": _STRUCTURED_ANALYSIS_NOTE}
    for key in V5_FIELDS:
        value = view.payload.get(key)
        if value is not None and value not in ("", [], {}):
            analysis[key] = deepcopy(value)
    return analysis


_CONDITION_STATEMENT_LOCATION = prose("prompt_context/condition_statement_location")


def _discriminating_condition_prompt_view(condition: Any, gherkin_text: str) -> Any:
    """Keep the condition statement once when the producer Gherkin already states it."""

    if not isinstance(condition, dict):
        return condition
    result = deepcopy(condition)
    statement = result.get("statement")
    if (
        isinstance(statement, str)
        and statement.strip()
        and f"the discriminating condition holds: {statement}" in gherkin_text
    ):
        del result["statement"]
        result["statement_location"] = _CONDITION_STATEMENT_LOCATION
    return result


def _condition_check_prompt_view(check: Any) -> Any:
    """Keep the producer's pre-execution status and per-comparison results.

    The producer's per-comparison reasons restate the comparison and its result,
    so model context omits them; the handoff keeps them unchanged.
    """

    if not isinstance(check, dict):
        return check
    result = {key: deepcopy(value) for key, value in check.items() if key != "comparisons"}
    comparisons = check.get("comparisons")
    if isinstance(comparisons, list):
        result["comparisons"] = [
            {key: value for key, value in item.items() if key != "reason"}
            if isinstance(item, dict)
            else item
            for item in comparisons
        ]
    return result


def _explained_operations(
    inventory: dict[str, Any],
    selected_names: set[str] | None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for operation in inventory.get("operations", []):
        if not isinstance(operation, dict) or not isinstance(operation.get("name"), str):
            continue
        if selected_names is not None and operation["name"] not in selected_names:
            continue
        result.append(
            {
                **operation,
                "identifier": {
                    "name": operation["name"],
                    "kind": "operation_name",
                    "meaning": operation.get("description", "documented operation"),
                },
            }
        )
    return result
