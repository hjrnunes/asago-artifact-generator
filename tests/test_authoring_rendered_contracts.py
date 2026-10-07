"""Offline tests for the prompts the two-call authoring seam renders."""

from __future__ import annotations

from asago_artifact_generator.authoring.core import (
    PromptPacket,
)
from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
    build_call2_packet_v2,
)
from asago_artifact_generator.bindings import validate_bindings

from .support import (
    world_builders,
)

_inventory_v2, _plan_v2, _runtime_contract_v2 = world_builders(
    "refund", "inventory", "plan", "runtime_contract"
)

(_view,) = world_builders("refund-minimal", "view")


def test_call_packets_are_deterministic_and_include_complete_inventory() -> None:
    view = _view()

    first = build_call1_packet_v2(view, _inventory_v2(), _runtime_contract_v2())
    second = build_call1_packet_v2(view, _inventory_v2(), _runtime_contract_v2())

    assert isinstance(first, PromptPacket)
    assert first.system == second.system
    assert first.user == second.user
    assert "process_refund" in first.user
    assert "order:owned" in first.user
    assert "scenario:constraint" in first.user
    assert first.version.startswith("authoring-call1-")


def test_rendered_contracts_expose_complete_validator_shapes_and_empty_permissions() -> None:
    view = _view()
    contract = _runtime_contract_v2()
    call1 = build_call1_packet_v2(view, _inventory_v2(), contract)
    call2 = build_call2_packet_v2(view, _plan_v2(), _inventory_v2(), contract)

    for packet in (call1, call2):
        response_contract = packet.payload["response_contract"]
        schema = response_contract["schema"]
        assert schema["type"] == "object"
        assert set(schema["required"]) == set(response_contract["fields"])

    call1_contract = call1.payload["response_contract"]
    assert call1_contract["binding_declaration"]["required"] == [
        "name",
        "expected_type",
        "source_kind",
        "source_ref",
        "selector",
        "consumers",
        "on_missing",
    ]
    assert call1_contract["binding_declaration"]["consumer_rule"]
    assert call1_contract["binding_declaration"]["selector_rule"]
    assert {
        "field": "setup_recipe",
        "value": [],
        "when": "setup is unavailable or no permitted setup operation is needed",
    } in call1_contract["empty_value_guidance"]
    call1_schema = call1_contract["schema"]
    assert call1_schema["properties"]["setup_recipe"]["type"] == "array"
    assert call1_schema["properties"]["runtime_bindings"]["type"] == "array"
    assert call1_schema["properties"]["semantic_judge"]["properties"]["needed"]["type"] == (
        "boolean"
    )
    call2_contract = call2.payload["response_contract"]
    assert call2_contract["schema"]["properties"]["explanation"]["type"] == "string"
    assert "detector_interface" not in call2_contract


def test_rendered_binding_contract_explains_direction_grammar_and_example() -> None:
    call1 = build_call1_packet_v2(_view(), _inventory_v2(), _runtime_contract_v2())

    binding = call1.payload["response_contract"]["binding_declaration"]
    assert "facts:<fact ref>" in binding["source_ref_rule"]
    assert "setup:<operation>" in binding["source_ref_rule"]
    assert binding["direction"] == "source_ref -> selector -> consumers"
    assert "extracts one value" in binding["selector_rule"]
    assert "destination paths" in binding["consumer_rule"]
    assert binding["valid_example"] == {
        "name": "setup_status",
        "expected_type": "string",
        "source_kind": "setup_output",
        "source_ref": "setup:case_permitted_operation",
        "selector": "result.status",
        "consumers": ["prerequisites.setup_status"],
        "on_missing": "stop",
    }


def test_rendered_binding_contract_explains_applicability_and_both_source_examples() -> None:
    call1 = build_call1_packet_v2(_view(), _inventory_v2(), _runtime_contract_v2())

    binding = call1.payload["response_contract"]["binding_declaration"]
    assert binding["source_scope"] == (
        "Only environment inventory facts are bindable supplied sources; "
        "input payloads and source handles remain context and are not bindable sources."
    )
    assert binding["applicability"].startswith(
        "runtime_bindings is [] (an empty list) only when no consumer needs a bound value"
    )
    assert binding["valid_examples"]["supplied_input"] == {
        "name": "loan_id",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:<fact ref>",
        "selector": "value.<documented field path>",
        "consumers": ["stimulus.user_text"],
        "on_missing": "stop",
    }
    assert binding["valid_examples"]["setup_output"] == binding["valid_example"]


def test_rendered_binding_examples_are_accepted_by_closed_validator() -> None:
    packet = build_call1_packet_v2(_view(), _inventory_v2(), _runtime_contract_v2())
    examples = packet.payload["response_contract"]["binding_declaration"]["valid_examples"]
    supplied = {
        **examples["supplied_input"],
        "source_ref": examples["supplied_input"]["source_ref"].replace("<fact ref>", "loan"),
        "selector": examples["supplied_input"]["selector"].replace(
            "<documented field path>", "loan_id"
        ),
    }
    inventory = {
        "facts": [
            {
                "ref": "loan",
                "schema": {
                    "type": "object",
                    "properties": {"loan_id": {"type": "string"}},
                },
            }
        ],
        "operations": [
            {
                "name": "case_permitted_operation",
                "result_schema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                    },
                },
            }
        ],
    }
    runtime_contract = {"setup_permissions": ["case_permitted_operation"]}

    validated = validate_bindings(
        [supplied, examples["setup_output"]],
        inventory=inventory,
        runtime_contract=runtime_contract,
    )

    assert [binding.name for binding in validated] == ["loan_id", "setup_status"]
