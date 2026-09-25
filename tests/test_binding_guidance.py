from __future__ import annotations

import copy

from asago_artifact_generator.authoring import (
    _binding_contract,
    build_plan_author_context,
    collect_plan_findings_v2,
)

from .test_versioned_prompt_roles import _inventory, _plan, _runtime_contract, _view


def _no_setup_runtime() -> dict:
    runtime = copy.deepcopy(_runtime_contract())
    runtime["setup_permissions"] = []
    return runtime


def test_current_supplied_input_example_uses_the_complete_fact_ref_form() -> None:
    contract = _binding_contract()
    example = contract["valid_examples"]["supplied_input"]

    assert example["source_ref"] == "facts:<fact ref>"
    assert example["selector"] == "value.<documented field path>"
    assert "facts:state:orders, never facts:orders" in contract["source_ref_rule"]
    assert _binding_contract(legacy=True)["valid_examples"]["supplied_input"]["source_ref"] == (
        "facts:order"
    )


def test_current_consumer_rule_names_exact_destinations() -> None:
    rule = _binding_contract()["consumer_rule"]

    assert "prerequisites.<binding name>" in rule
    assert "this binding's own name" in rule
    assert "never a * wildcard" in rule
    assert "observation_claim" in rule and "are not consumers" in rule
    assert "prerequisites.*" not in rule
    assert "prerequisites.*" in _binding_contract(legacy=True)["consumer_rule"]


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


def test_legacy_binding_contract_keeps_the_empty_neutral_example() -> None:
    context = build_plan_author_context(
        _view(), _inventory(), _no_setup_runtime(), legacy_binding_contract=True
    )

    assert context["field_guide"]["neutral_binding_example"]["runtime_bindings"] == []


def test_wildcard_prerequisite_consumer_stays_invalid() -> None:
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

    codes = [
        finding.code
        for finding in collect_plan_findings_v2(plan, _inventory(), _no_setup_runtime())
    ]

    assert "consumer_mismatch" in codes
