"""Offline tests for plan and artifact validation in the two-call authoring seam."""

from __future__ import annotations

from asago_artifact_generator.authoring.checks import (
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
)

from .support import (
    world_builders,
)

_inventory_v2, _plan_v2, _runtime_contract_v2 = world_builders(
    "refund", "inventory", "plan", "runtime_contract"
)
_inventory, _contract, _minimal_plan = world_builders(
    "refund-minimal", "inventory", "runtime_contract", "plan"
)


def _plan(**overrides):
    """Return the minimal refund plan with the two root fields only v2 plans carry."""

    return _minimal_plan(
        assumptions=[],
        required_observations={"tool_calls": {"required": True, "missing": "inconclusive"}},
        **overrides,
    )


def test_plan_binding_findings_accumulate_nested_faults_without_coercion() -> None:
    malformed = {
        "name": "draft_id",
        "expected_type": "any",
        "source_kind": "supplied_input",
        "source_ref": "stimulus.turns[0].text",
        "selector": "stimulus.turns[0].text",
        "consumers": ["stimulus.turns[0].text"],
        "on_missing": "ignore",
    }
    plan = _plan(runtime_bindings=[malformed])

    findings = collect_plan_findings_v2(plan, _inventory(), _contract())

    binding_findings = [
        finding for finding in findings if finding.path.startswith("runtime_bindings[0]")
    ]
    assert len(binding_findings) >= 5
    assert all(finding.code == "plan_binding_validation" for finding in binding_findings)
    details = " ".join(finding.detail for finding in binding_findings).lower()
    assert "consumer" in details
    assert "source_ref" in details
    assert "selector" in details
    assert "expected_type" in details
    assert "on_missing" in details
    assert all(finding.code != "artifact_validation" for finding in binding_findings)
    assert plan["runtime_bindings"] == [malformed]


def test_plan_validation_carries_canonical_deduplicated_bindings_into_prerequisites() -> None:
    inventory = _inventory()
    inventory["facts"].extend(
        [
            {
                "ref": "catalog:items",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ITEM-A": {
                            "type": "object",
                            "properties": {"owner": {"type": "string"}},
                        }
                    },
                },
                "value": {"ITEM-A": {"owner": "OWNER-A"}},
            },
            {
                "ref": "catalog:items:records",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ITEM-A": {
                            "type": "object",
                            "properties": {"record_key": {"type": "string"}},
                        }
                    },
                },
                "value": {"ITEM-A": {"record_key": "ITEM-A"}},
            },
        ]
    )
    binding = {
        "name": "item_owner",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:catalog:items:records:ITEM-A:owner",
        "selector": "value.owner",
        "consumers": ["prerequisites.item_owner", "setup.arguments.item_owner"],
        "on_missing": "stop",
    }
    plan = _plan(
        runtime_bindings=[binding, dict(binding)],
        prerequisites=[
            {
                "name": "owner_is_supplied",
                "check": "the supplied owner is available",
                "evidence_refs": ["order:owned"],
                "binding": "item_owner",
                "equals": "OWNER-A",
            }
        ],
    )
    transformations: list[dict] = []

    findings = collect_plan_findings_v2(
        plan,
        inventory,
        _contract(),
        transformations=transformations,
    )

    assert findings == []
    assert len(plan["runtime_bindings"]) == 1
    assert plan["runtime_bindings"][0]["source_ref"] == "facts:catalog:items"
    assert plan["runtime_bindings"][0]["selector"] == "value.ITEM-A.owner"
    assert "prerequisites.item_owner" in plan["runtime_bindings"][0]["consumers"]
    assert [item["transformation"] for item in transformations] == [
        "binding_canonicalized",
        "binding_canonicalized",
        "binding_duplicate_dropped",
    ]


def test_plan_validation_accepts_a_record_shorthand_whose_selector_repeats_the_key() -> None:
    inventory = _inventory()
    inventory["facts"].extend(
        [
            {
                "ref": "catalog:items",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ITEM-A": {
                            "type": "object",
                            "properties": {"owner": {"type": "string"}},
                        }
                    },
                },
                "value": {"ITEM-A": {"owner": "OWNER-A"}},
            },
            {
                "ref": "catalog:items:records",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ITEM-A": {
                            "type": "object",
                            "properties": {"record_key": {"type": "string"}},
                        }
                    },
                },
                "value": {"ITEM-A": {"record_key": "ITEM-A"}},
            },
        ]
    )
    plan = _plan(
        runtime_bindings=[
            {
                "name": "item_key",
                "expected_type": "string",
                "source_kind": "supplied_input",
                "source_ref": "facts:catalog:items:records:ITEM-A",
                "selector": "value.ITEM-A.record_key",
                "consumers": ["setup.arguments.item_key"],
                "on_missing": "stop",
            },
            {
                "name": "item_owner",
                "expected_type": "string",
                "source_kind": "supplied_input",
                "source_ref": "facts:catalog:items:ITEM-A",
                "selector": "value.ITEM-A.owner",
                "consumers": ["setup.arguments.item_owner"],
                "on_missing": "stop",
            },
        ],
        prerequisites=[],
    )

    findings = collect_plan_findings_v2(plan, inventory, _contract())

    assert findings == []
    assert [(item["source_ref"], item["selector"]) for item in plan["runtime_bindings"]] == [
        ("facts:catalog:items:records", "value.ITEM-A.record_key"),
        ("facts:catalog:items", "value.ITEM-A.owner"),
    ]


def test_plan_validation_adds_missing_prerequisite_consumer_and_records_rewrite() -> None:
    inventory = _inventory()
    inventory["facts"].append(
        {
            "ref": "synthetic:owner",
            "value": {"owner": "OWNER-A"},
            "schema": {
                "type": "object",
                "properties": {"owner": {"type": "string"}},
            },
        }
    )
    binding = {
        "name": "owner",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:synthetic:owner",
        "selector": "value.owner",
        "consumers": ["setup.arguments.owner"],
        "on_missing": "stop",
    }
    plan = _plan(
        runtime_bindings=[binding],
        prerequisites=[
            {
                "name": "owner_is_present",
                "check": "The supplied owner is present.",
                "evidence_refs": ["order:owned"],
                "binding": "owner",
                "equals": "OWNER-A",
            }
        ],
    )
    transformations: list[dict] = []

    findings = collect_plan_findings_v2(
        plan,
        inventory,
        _contract(),
        transformations=transformations,
    )

    assert findings == []
    assert plan["runtime_bindings"][0]["consumers"] == [
        "setup.arguments.owner",
        "prerequisites.owner",
    ]
    assert transformations == [
        {
            "transformation": "binding_consumer_added",
            "binding": "owner",
            "original_consumers": ["setup.arguments.owner"],
            "canonical_consumers": ["setup.arguments.owner", "prerequisites.owner"],
            "prerequisite_index": 0,
            "prerequisite_name": "owner_is_present",
            "consumer": "prerequisites.owner",
        }
    ]


def test_plan_validation_accumulates_all_structural_findings() -> None:
    malformed = {
        "interpretation": "wrong",
        "selected_evidence": ["wrong"],
        "assumptions": "wrong",
        "setup_recipe": "wrong",
        "runtime_bindings": {"wrong": True},
        "prerequisites": ["wrong"],
        "stimulus_approach": "wrong",
        "observation_claim": "wrong",
        "required_observations": "wrong",
        "semantic_judge": "wrong",
        "unresolved_requirements": "wrong",
    }

    findings = collect_plan_findings_v2(malformed, _inventory_v2(), _runtime_contract_v2())

    assert len(findings) >= 11
    paths = {finding.path for finding in findings}
    assert all(
        any(path == expected or path.startswith(f"{expected}[") for path in paths)
        for expected in {
            "interpretation",
            "selected_evidence",
            "assumptions",
            "setup_recipe",
            "runtime_bindings",
            "prerequisites",
            "stimulus_approach",
            "observation_claim",
            "required_observations",
            "semantic_judge",
            "unresolved_requirements",
        }
    )


def test_artifact_validation_accumulates_all_structural_findings() -> None:
    malformed = {
        "stimulus": "wrong",
        "semantic_judge_spec": 7,
        "explanation": [],
        "examples": [],
    }

    findings = collect_artifact_findings_v2(
        malformed, _plan_v2(), _inventory_v2(), _runtime_contract_v2()
    )

    assert {finding.path for finding in findings} >= {
        "stimulus",
        "semantic_judge_spec",
        "explanation",
        "examples",
    }
