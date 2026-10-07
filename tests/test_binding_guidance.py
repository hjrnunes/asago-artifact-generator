from __future__ import annotations

import copy

from asago_artifact_generator.authoring.checks import collect_plan_findings_v2
from asago_artifact_generator.authoring.contracts import _binding_contract
from asago_artifact_generator.authoring.prompt_context import build_plan_author_context

from .support import world_builders

_inventory, _plan, _runtime_contract, _view = world_builders(
    "ehr", "inventory", "plan", "runtime_contract", "view"
)


def _no_setup_runtime() -> dict:
    runtime = copy.deepcopy(_runtime_contract())
    runtime["setup_permissions"] = []
    return runtime


def test_current_supplied_input_example_uses_the_complete_fact_ref_form() -> None:
    contract = _binding_contract()
    example = contract["valid_examples"]["supplied_input"]

    assert example["source_ref"] == "facts:<fact ref>"
    assert example["selector"] == "value.<documented field path>"
    assert "facts:state:loans, never facts:loans" in contract["source_ref_rule"]


def test_current_consumer_rule_names_exact_destinations() -> None:
    rule = _binding_contract()["consumer_rule"]

    assert "prerequisites.<binding name>" in rule
    assert "this binding's own name" in rule
    assert "never a * wildcard" in rule
    assert "observation_claim" in rule and "are not consumers" in rule
    assert "prerequisites.*" not in rule


def test_neutral_binding_example_uses_a_supplied_scalar_fact_without_setup() -> None:
    runtime = _no_setup_runtime()
    context = build_plan_author_context(_view(), _inventory(), runtime)
    example = context["field_guide"]["neutral_binding_example"]

    assert example["runtime_bindings"] == [
        {
            "name": "status",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:draft:status",
            "selector": "value",
            "consumers": ["prerequisites.status"],
            "on_missing": "stop",
        }
    ]
    assert example["prerequisites"] == [
        {
            "name": "status_matches_supplied_fact",
            "check": "The resolved status value equals the supplied fact draft:status.",
            "evidence_refs": ["draft:status"],
            "binding": "status",
            "equals": "AWAITING_REVIEW",
        }
    ]

    plan = copy.deepcopy(_plan())
    plan["runtime_bindings"] = example["runtime_bindings"]
    plan["prerequisites"] = example["prerequisites"]
    assert collect_plan_findings_v2(plan, _inventory(), runtime) == []


def test_plan_instruction_limits_bindings_to_values_that_exist_before_the_run() -> None:
    instruction = build_plan_author_context(_view(), _inventory(), _no_setup_runtime())["task"][
        "instruction"
    ]

    rules = (
        "A runtime binding carries a value that exists before the run",
        "Write content the experiment chooses",
        "directly in the stimulus; it needs no binding",
        "A value the target produces during the run",
        "is captured evidence in evidence.tool_calls or evidence.messages",
        "not a runtime binding or a setup:<operation> source",
        "an empty list or empty object supplies nothing to bind",
    )
    for rule in rules:
        assert rule in instruction


def test_wildcard_prerequisite_consumer_gets_exact_binding_consumer() -> None:
    plan = copy.deepcopy(_plan())
    plan["runtime_bindings"] = [
        {
            "name": "status",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:draft:status",
            "selector": "value",
            "consumers": ["prerequisites.*"],
            "on_missing": "stop",
        }
    ]
    plan["prerequisites"] = [
        {
            "name": "status_ready",
            "check": "The draft status is supplied.",
            "evidence_refs": ["draft:status"],
            "binding": "status",
            "equals": "AWAITING_REVIEW",
        }
    ]

    assert collect_plan_findings_v2(plan, _inventory(), _no_setup_runtime()) == []
    assert plan["runtime_bindings"][0]["consumers"] == [
        "prerequisites.*",
        "prerequisites.status",
    ]


def test_plan_validation_persists_canonical_keyed_binding_paths() -> None:
    inventory = _inventory()
    inventory["facts"].extend(
        [
            {
                "ref": "state:orders",
                "value": {"ORD-1": {"customer_id": "CUST-1"}},
                "schema": {
                    "type": "object",
                    "properties": {
                        "ORD-1": {
                            "type": "object",
                            "properties": {"customer_id": {"type": "string"}},
                        }
                    },
                },
            },
            {
                "ref": "state:orders:records",
                "value": {"ORD-1": {"record_key": "ORD-1"}},
                "schema": {
                    "type": "object",
                    "properties": {
                        "ORD-1": {
                            "type": "object",
                            "properties": {"record_key": {"type": "string"}},
                        }
                    },
                },
            },
        ]
    )
    plan = copy.deepcopy(_plan())
    plan["runtime_bindings"] = [
        {
            "name": "customer_id",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:state:orders:ORD-1:customer_id",
            "selector": "value",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    plan["stimulus_approach"]["request"] = "Show the orders for customer {{customer_id}}."

    assert collect_plan_findings_v2(plan, inventory, _runtime_contract()) == []
    assert plan["runtime_bindings"][0]["source_ref"] == "facts:state:orders"
    assert plan["runtime_bindings"][0]["selector"] == "value.ORD-1.customer_id"
