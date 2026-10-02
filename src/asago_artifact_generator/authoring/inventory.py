from __future__ import annotations

from typing import Any

from ..input_adapter import InputView, build_scenario_handoff_view
from .core import ArtifactValidationError, _mapping_sha256, _sha256


def _expected_authoring_input_pins(
    *,
    input_view: InputView,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any]:
    """Return the source and contract pins for one authoring input set."""

    return {
        "schema_version": "authoring-input-pins-v1",
        "scenario_id": input_view.scenario_id,
        "input_sha256": input_view.source_sha256,
        "source_digests": dict(input_view.source_digests),
        "inventory_sha256": _mapping_sha256(inventory),
        "runtime_contract_sha256": _mapping_sha256(runtime_contract),
    }


def _inventory_references(inventory: dict[str, Any]) -> set[str]:
    references = {
        str(item.get("ref"))
        for item in inventory.get("facts", [])
        if isinstance(item, dict) and item.get("ref")
    }
    references.update(
        str(item.get("ref"))
        for item in inventory.get("source_handles", [])
        if isinstance(item, dict) and item.get("ref")
    )
    references.update(
        f"operation:{item.get('name')}"
        for item in inventory.get("operations", [])
        if isinstance(item, dict) and item.get("name")
    )
    return references


def _inventory_fact_map(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return supplied static facts keyed by their authoritative reference."""

    return {
        item["ref"]: item
        for item in inventory.get("facts", [])
        if isinstance(item, dict) and isinstance(item.get("ref"), str) and item["ref"].strip()
    }


def _resolved_judge_spec(
    judge_spec: Any,
    inventory: dict[str, Any],
) -> dict[str, Any] | None:
    """Replace model-selected fact references with immutable supplied facts."""

    if judge_spec is None:
        return None
    if not isinstance(judge_spec, dict):
        raise ArtifactValidationError("judge specification must be an object", "judge.json")
    refs = judge_spec.get("fact_refs")
    if not isinstance(refs, list):
        raise ArtifactValidationError(
            "judge specification fact_refs must be a list",
            "judge.json.fact_refs",
        )
    fact_map = _inventory_fact_map(inventory)
    facts: list[dict[str, Any]] = []
    for index, ref in enumerate(refs):
        path = f"judge.json.fact_refs[{index}]"
        if not isinstance(ref, str) or not ref.strip():
            raise ArtifactValidationError(
                f"unknown static fact reference: {ref}",
                path,
            )
        supplied = fact_map.get(ref)
        if not isinstance(supplied, dict):
            raise ArtifactValidationError(
                f"unknown static fact reference: {ref}",
                path,
            )
        if "value" not in supplied:
            raise ArtifactValidationError(
                f"static fact has no supplied value: {ref}",
                path,
            )
        fact: dict[str, Any] = {
            "ref": ref,
            "value": supplied["value"],
            "source": ref,
        }
        if "provenance" in supplied:
            fact["provenance"] = supplied["provenance"]
        facts.append(fact)
    return {
        "question": judge_spec.get("question"),
        "criteria": judge_spec.get("criteria"),
        "facts": facts,
    }


def _selected_refs(plan: dict[str, Any], inventory: dict[str, Any]) -> dict[str, set[str]]:
    selected = {"operations": set(), "facts": set(), "sources": set()}
    operation_names = {
        item.get("name")
        for item in inventory.get("operations", [])
        if isinstance(item, dict) and item.get("name")
    }
    fact_refs = {
        item.get("ref")
        for item in inventory.get("facts", [])
        if isinstance(item, dict) and item.get("ref")
    }
    for item in plan.get("selected_evidence", []):
        ref = item.get("ref") if isinstance(item, dict) else ""
        if ref in operation_names:
            selected["operations"].add(ref)
        elif ref.startswith("operation:") and ref.split(":", 1)[1] in operation_names:
            selected["operations"].add(ref.split(":", 1)[1])
        elif ref in fact_refs:
            selected["facts"].add(ref)
        else:
            selected["sources"].add(ref)
    for item in plan.get("runtime_bindings", []):
        if isinstance(item, dict):
            ref = item.get("source_ref", "")
            if ref.startswith("setup:"):
                selected["operations"].add(ref.split(":", 1)[1])
            elif ref.startswith("facts:"):
                selected["facts"].add(ref.split(":", 1)[1])
    return selected


def _input_view_payload(
    view: InputView,
    *,
    include_scenario_handoff: bool = True,
) -> dict[str, Any]:
    """Build the meaning-preserving model-facing input projection."""

    payload = {
        "kind": view.kind.value,
        "scenario_id": view.scenario_id,
        "narrative": view.narrative,
        "narrative_bytes_sha256": _sha256(view.narrative_bytes),
        "gherkin_text": view.gherkin_text,
        "gherkin_bytes_sha256": _sha256(view.gherkin_bytes),
        "source_digests": view.source_digests,
    }
    if include_scenario_handoff:
        payload["scenario_handoff"] = build_scenario_handoff_view(view)
    return payload


def _source_input_payload(view: InputView) -> dict[str, Any]:
    """Build the complete source-bearing package record outside model context."""

    return {
        "kind": view.kind.value,
        "scenario_id": view.scenario_id,
        "payload": view.payload,
        "narrative": view.narrative,
        "narrative_bytes_sha256": _sha256(view.narrative_bytes),
        "gherkin_text": view.gherkin_text,
        "gherkin_bytes_sha256": _sha256(view.gherkin_bytes),
        "source_digests": view.source_digests,
    }
