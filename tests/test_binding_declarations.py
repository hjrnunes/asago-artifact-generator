"""Offline tests for closed runtime binding declarations."""

from __future__ import annotations

import dataclasses
import re

import pytest

from asago_artifact_generator.authoring import contracts
from asago_artifact_generator.authoring.checks import (
    _RUNTIME_BINDING_CLAUSES,
    PLAN_MECHANICAL_CHECKS,
    _collect_binding_findings,
    collect_plan_findings_v2,
)
from asago_artifact_generator.authoring.contracts import _binding_contract, _binding_list_schema
from asago_artifact_generator.bindings import (
    BINDING_SPEC,
    BindingValidationError,
    ConsumerPrefix,
    RuntimeBinding,
    canonical_binding_paths,
    find_stimulus_user_text_consumer_mismatches,
    named_record_facts,
    supplied_binding_values,
    validate_bindings,
)

from .support import world_builders

_inventory, _plan, _runtime_contract = world_builders(
    "ehr", "inventory", "plan", "runtime_contract"
)


def test_binding_uses_exact_setup_selector_and_declared_consumers() -> None:
    binding = RuntimeBinding(
        name="draft_id",
        expected_type="string",
        source_kind="setup_output",
        source_ref="setup:summarize_for_ehr",
        selector="result.draft.id",
        consumers=("stimulus.user_text", "prerequisites.draft", "judge.evidence"),
        on_missing="stop",
    )

    validated = validate_bindings(
        [binding.to_dict()],
        inventory={
            "operations": [
                {
                    "name": "summarize_for_ehr",
                    "result_schema": {
                        "type": "object",
                        "properties": {
                            "draft": {
                                "type": "object",
                                "properties": {"id": {"type": "string"}},
                            }
                        },
                    },
                }
            ]
        },
        runtime_contract={"setup_permissions": ["summarize_for_ehr"]},
    )

    assert validated[0].selector == "result.draft.id"


@pytest.mark.parametrize(
    ("expected_type", "message"),
    [("any", "closed"), ("string", "undocumented selector")],
)
def test_binding_rejects_undocumented_selector(expected_type: str, message: str) -> None:
    with pytest.raises(BindingValidationError, match=message):
        validate_bindings(
            [
                {
                    "name": "draft_id",
                    "expected_type": expected_type,
                    "source_kind": "setup_output",
                    "source_ref": "setup:summarize_for_ehr",
                    "selector": "result.draft.guessed_id",
                    "consumers": ["stimulus.user_text"],
                    "on_missing": "stop",
                }
            ],
            inventory={
                "operations": [
                    {
                        "name": "summarize_for_ehr",
                        "result_schema": {
                            "type": "object",
                            "properties": {"draft": {"type": "object", "properties": {}}},
                        },
                    }
                ]
            },
            runtime_contract={"setup_permissions": ["summarize_for_ehr"]},
        )


def test_binding_rejects_non_string_declaration_fields() -> None:
    with pytest.raises(BindingValidationError, match="source_ref must be a string"):
        RuntimeBinding.from_dict(
            {
                "name": "draft_id",
                "expected_type": "string",
                "source_kind": "setup_output",
                "source_ref": 7,
                "selector": "result.draft.id",
                "consumers": ["stimulus.user_text"],
                "on_missing": "stop",
            }
        )


def _keyed_fact(ref: str, key: str, fields: dict[str, str]) -> dict:
    """Return a fact whose value maps ``key`` to string ``fields``, with its schema."""

    properties = {name: {"type": "string"} for name in fields}
    return {
        "ref": ref,
        "value": {key: fields},
        "schema": {
            "type": "object",
            "properties": {key: {"type": "object", "properties": properties}},
        },
    }


def _records_inventory(ref: str, key: str, fields: dict[str, str]) -> dict:
    """Return a keyed base fact and its ``:records`` companion naming ``key``."""

    return {
        "operations": [],
        "facts": [
            _keyed_fact(ref, key, fields),
            _keyed_fact(f"{ref}:records", key, {"record_key": key}),
        ],
    }


def _keyed_inventory() -> dict:
    return _records_inventory("state:orders", "ORD-1", {"customer_id": "CUST-1", "status": "open"})


def test_keyed_source_shorthands_normalize_to_documented_paths() -> None:
    validated = validate_bindings(
        [
            {
                "name": "customer_id",
                "expected_type": "string",
                "source_kind": "supplied_input",
                "source_ref": "facts:state:orders:ORD-1:customer_id",
                "selector": "value",
                "consumers": ["stimulus.user_text"],
                "on_missing": "stop",
            },
            {
                "name": "record_key",
                "expected_type": "string",
                "source_kind": "supplied_input",
                "source_ref": "facts:state:orders:records:ORD-1.record_key",
                "selector": "value",
                "consumers": ["stimulus.user_text"],
                "on_missing": "stop",
            },
            {
                "name": "record",
                "expected_type": "object",
                "source_kind": "supplied_input",
                "source_ref": "facts:state:orders:ORD-1",
                "selector": "value",
                "consumers": ["judge.record"],
                "on_missing": "stop",
            },
        ],
        inventory=_keyed_inventory(),
        runtime_contract={"setup_permissions": []},
    )

    assert [(item.source_ref, item.selector) for item in validated] == [
        ("facts:state:orders", "value.ORD-1.customer_id"),
        ("facts:state:orders:records", "value.ORD-1.record_key"),
        ("facts:state:orders", "value.ORD-1"),
    ]


@pytest.mark.parametrize(
    ("source_ref", "expected_type", "message"),
    [
        (
            "facts:state:orders:ORD-404:customer_id",
            "string",
            "unknown supplied fact",
        ),
        (
            "facts:state:orders:ORD-1:not_a_field",
            "string",
            "unknown supplied fact",
        ),
        (
            "facts:state:orders:ORD-1:customer_id",
            "object",
            "type mismatch",
        ),
    ],
)
def test_keyed_source_shorthands_reject_unknown_keys_fields_and_types(
    source_ref: str,
    expected_type: str,
    message: str,
) -> None:
    with pytest.raises(BindingValidationError, match=message):
        validate_bindings(
            [
                {
                    "name": "value",
                    "expected_type": expected_type,
                    "source_kind": "supplied_input",
                    "source_ref": source_ref,
                    "selector": "value",
                    "consumers": ["stimulus.user_text"],
                    "on_missing": "stop",
                }
            ],
            inventory=_keyed_inventory(),
            runtime_contract={"setup_permissions": []},
        )


@pytest.mark.parametrize(
    ("source_ref", "selector", "expected"),
    [
        (
            "facts:state:orders:ORD-1",
            "value.ORD-1.customer_id",
            ("facts:state:orders", "value.ORD-1.customer_id"),
        ),
        (
            "facts:state:orders:records:ORD-1",
            "value.ORD-1.record_key",
            ("facts:state:orders:records", "value.ORD-1.record_key"),
        ),
        (
            "facts:state:orders:records:ORD-1",
            "value.ORD-1.customer_id",
            ("facts:state:orders", "value.ORD-1.customer_id"),
        ),
        (
            "facts:state:orders:ORD-1:customer_id",
            "value.ORD-1.customer_id",
            ("facts:state:orders", "value.ORD-1.customer_id"),
        ),
    ],
)
def test_keyed_source_shorthands_accept_a_selector_that_repeats_the_record_key(
    source_ref: str,
    selector: str,
    expected: tuple[str, str],
) -> None:
    transformations: list[dict] = []

    validated = validate_bindings(
        [
            {
                "name": "record_value",
                "expected_type": "string",
                "source_kind": "supplied_input",
                "source_ref": source_ref,
                "selector": selector,
                "consumers": ["judge.record_value"],
                "on_missing": "stop",
            }
        ],
        inventory=_keyed_inventory(),
        runtime_contract={"setup_permissions": []},
        transformations=transformations,
    )

    assert [(item.source_ref, item.selector) for item in validated] == [expected]
    assert transformations == [
        {
            "transformation": "binding_canonicalized",
            "binding": "record_value",
            "original_source_ref": source_ref,
            "original_selector": selector,
            "canonical_source_ref": expected[0],
            "canonical_selector": expected[1],
        }
    ]


@pytest.mark.parametrize(
    ("source_ref", "selector"),
    [
        ("facts:state:orders:ORD-1", "value.ORD-2.customer_id"),
        ("facts:state:orders:ORD-1", "value.ORD-1.not_a_field"),
        ("facts:state:orders:ORD-1:status", "value.ORD-1.customer_id"),
        ("facts:state:orders:ORD-404", "value.ORD-404.customer_id"),
    ],
)
def test_repeated_record_key_selectors_fail_closed_on_conflicts_and_unknown_paths(
    source_ref: str,
    selector: str,
) -> None:
    assert canonical_binding_paths(
        "supplied_input",
        source_ref,
        selector,
        _keyed_inventory(),
    ) == (source_ref, selector)
    with pytest.raises(BindingValidationError, match="unknown supplied fact"):
        validate_bindings(
            [
                {
                    "name": "record_value",
                    "expected_type": "string",
                    "source_kind": "supplied_input",
                    "source_ref": source_ref,
                    "selector": selector,
                    "consumers": ["judge.record_value"],
                    "on_missing": "stop",
                }
            ],
            inventory=_keyed_inventory(),
            runtime_contract={"setup_permissions": []},
        )


def _companion_inventory() -> dict:
    return _records_inventory("catalog:items", "ITEM-A", {"owner": "OWNER-A", "label": "Sample"})


@pytest.mark.parametrize(
    ("source_ref", "selector"),
    [
        ("facts:catalog:items:records:ITEM-A:owner", "value.owner"),
        ("facts:catalog:items:records", "value.ITEM-A.owner"),
    ],
)
def test_records_companion_fields_fall_back_to_base_fact(
    source_ref: str,
    selector: str,
) -> None:
    transformations: list[dict] = []
    declarations = [
        {
            "name": "item_owner",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": source_ref,
            "selector": selector,
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]

    validated = validate_bindings(
        declarations,
        inventory=_companion_inventory(),
        runtime_contract={"setup_permissions": []},
        transformations=transformations,
    )

    assert [(binding.source_ref, binding.selector) for binding in validated] == [
        ("facts:catalog:items", "value.ITEM-A.owner")
    ]
    assert transformations == [
        {
            "transformation": "binding_canonicalized",
            "binding": "item_owner",
            "original_source_ref": source_ref,
            "original_selector": selector,
            "canonical_source_ref": "facts:catalog:items",
            "canonical_selector": "value.ITEM-A.owner",
        }
    ]


def test_record_key_selector_on_a_base_fact_resolves_to_its_records_companion() -> None:
    transformations: list[dict] = []
    declarations = [
        {
            "name": "item_id",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:catalog:items",
            "selector": "value.ITEM-A.record_key",
            "consumers": ["judge.item_id"],
            "on_missing": "stop",
        }
    ]

    validated = validate_bindings(
        declarations,
        inventory=_companion_inventory(),
        runtime_contract={"setup_permissions": []},
        transformations=transformations,
    )

    assert [(binding.source_ref, binding.selector) for binding in validated] == [
        ("facts:catalog:items:records", "value.ITEM-A.record_key")
    ]
    assert transformations == [
        {
            "transformation": "binding_canonicalized",
            "binding": "item_id",
            "original_source_ref": "facts:catalog:items",
            "original_selector": "value.ITEM-A.record_key",
            "canonical_source_ref": "facts:catalog:items:records",
            "canonical_selector": "value.ITEM-A.record_key",
        }
    ]
    assert supplied_binding_values(declarations, _companion_inventory()) == {"item_id": "ITEM-A"}


@pytest.mark.parametrize(
    "selector",
    ["value.ITEM-B.record_key", "value.<record_key>.record_key", "value.ITEM-A.missing"],
)
def test_base_fact_selectors_the_companion_does_not_document_stay_invalid(
    selector: str,
) -> None:
    assert canonical_binding_paths(
        "supplied_input", "facts:catalog:items", selector, _companion_inventory()
    ) == ("facts:catalog:items", selector)
    with pytest.raises(BindingValidationError, match="undocumented selector"):
        validate_bindings(
            [
                {
                    "name": "item_id",
                    "expected_type": "string",
                    "source_kind": "supplied_input",
                    "source_ref": "facts:catalog:items",
                    "selector": selector,
                    "consumers": ["judge.item_id"],
                    "on_missing": "stop",
                }
            ],
            inventory=_companion_inventory(),
            runtime_contract={"setup_permissions": []},
        )


def test_base_fact_that_documents_a_record_key_field_keeps_its_own_selector() -> None:
    inventory = _companion_inventory()
    base = inventory["facts"][0]
    base["value"]["ITEM-A"]["record_key"] = "LEGACY-A"
    base["schema"]["properties"]["ITEM-A"]["properties"]["record_key"] = {"type": "string"}

    assert canonical_binding_paths(
        "supplied_input", "facts:catalog:items", "value.ITEM-A.record_key", inventory
    ) == ("facts:catalog:items", "value.ITEM-A.record_key")


def test_companion_fallback_fails_closed_for_undocumented_fields() -> None:
    source_ref = "facts:catalog:items:records"
    selector = "value.ITEM-A.missing"

    assert canonical_binding_paths(
        "supplied_input",
        source_ref,
        selector,
        _companion_inventory(),
    ) == (source_ref, selector)
    with pytest.raises(BindingValidationError, match="undocumented selector"):
        validate_bindings(
            [
                {
                    "name": "missing_field",
                    "expected_type": "string",
                    "source_kind": "supplied_input",
                    "source_ref": source_ref,
                    "selector": selector,
                    "consumers": ["stimulus.user_text"],
                    "on_missing": "stop",
                }
            ],
            inventory=_companion_inventory(),
            runtime_contract={"setup_permissions": []},
        )


def test_companion_fallback_fails_closed_when_two_targets_are_documented() -> None:
    inventory = _companion_inventory()
    inventory["facts"].append(
        _keyed_fact("catalog:items:records:records", "ITEM-A", {"owner": "OWNER-B"})
    )
    source_ref = "facts:catalog:items:records:ITEM-A:owner"
    selector = "value.owner"

    assert canonical_binding_paths(
        "supplied_input",
        source_ref,
        selector,
        inventory,
    ) == (source_ref, selector)


def test_exact_canonical_duplicates_are_dropped_and_recorded() -> None:
    first = {
        "name": "item_owner",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:catalog:items:records:ITEM-A:owner",
        "selector": "value.owner",
        "consumers": ["stimulus.user_text"],
        "on_missing": "stop",
    }
    declarations = [first, dict(first)]
    transformations: list[dict] = []

    validated = validate_bindings(
        declarations,
        inventory=_companion_inventory(),
        runtime_contract={"setup_permissions": []},
        transformations=transformations,
    )

    assert len(declarations) == 1
    assert len(validated) == 1
    assert transformations == [
        {
            "transformation": "binding_canonicalized",
            "binding": "item_owner",
            "original_source_ref": first["source_ref"],
            "original_selector": first["selector"],
            "canonical_source_ref": "facts:catalog:items",
            "canonical_selector": "value.ITEM-A.owner",
        },
        {
            "transformation": "binding_canonicalized",
            "binding": "item_owner",
            "original_source_ref": first["source_ref"],
            "original_selector": first["selector"],
            "canonical_source_ref": "facts:catalog:items",
            "canonical_selector": "value.ITEM-A.owner",
        },
        {
            "transformation": "binding_duplicate_dropped",
            "binding": "item_owner",
            "original_source_ref": first["source_ref"],
            "original_selector": first["selector"],
            "canonical_source_ref": "facts:catalog:items",
            "canonical_selector": "value.ITEM-A.owner",
            "kept_index": 0,
            "dropped_index": 1,
        },
    ]


def test_same_name_with_different_fields_still_fails_duplicate_check() -> None:
    first = {
        "name": "item_value",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:catalog:items",
        "selector": "value.ITEM-A.owner",
        "consumers": ["stimulus.user_text"],
        "on_missing": "stop",
    }
    second = {**first, "selector": "value.ITEM-A.label"}

    with pytest.raises(BindingValidationError, match="duplicate binding: item_value"):
        validate_bindings(
            [first, second],
            inventory=_companion_inventory(),
            runtime_contract={"setup_permissions": []},
        )


def test_supplied_binding_values_use_canonicalized_deduplicated_declarations() -> None:
    declaration = {
        "name": "item_owner",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:catalog:items:records:ITEM-A:owner",
        "selector": "value.owner",
        "consumers": ["judge.item_owner"],
        "on_missing": "stop",
    }
    declarations = [declaration, dict(declaration)]

    assert supplied_binding_values(declarations, _companion_inventory()) == {
        "item_owner": "OWNER-A"
    }
    assert len(declarations) == 1
    assert declarations[0]["source_ref"] == "facts:catalog:items"
    assert declarations[0]["selector"] == "value.ITEM-A.owner"


_FACT_INVENTORY = {
    "facts": [
        {
            "ref": "f",
            "value": {"a": [1, 2], "s": "x"},
            "schema": {
                "type": "object",
                "properties": {
                    "a": {"type": "array", "items": {"type": "integer"}},
                    "s": {"type": "string"},
                    "bad": "text",
                },
            },
        }
    ]
}


def _declaration(**changes: object) -> dict:
    return {
        "name": "n",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:f",
        "selector": "value.s",
        "consumers": ["judge.n"],
        "on_missing": "stop",
        **changes,
    }


@pytest.mark.parametrize(
    ("declarations", "message"),
    [
        ({}, "runtime_bindings must be a list"),
        ([5], "binding must be an object"),
        ([{"name": "n"}], "binding fields invalid (missing=['consumers', 'expected_type',"),
        ([_declaration(consumers=[""])], "binding consumers must be non-empty strings"),
        ([_declaration(name=" ")], "binding name is blank"),
        ([_declaration(source_kind="other")], "binding source_kind is not closed: n"),
        ([_declaration(selector=" ")], "binding source reference is blank: n"),
        ([_declaration(on_missing="maybe")], "binding on_missing is not closed: n"),
        ([_declaration(selector="value..s")], "undocumented selector for binding n: value..s"),
        ([_declaration(selector="other.s")], "undocumented selector for binding n: other.s"),
        (
            [_declaration(selector="value.bad.x")],
            "undocumented selector for binding n: value.bad.x",
        ),
    ],
)
def test_validate_bindings_names_the_first_invalid_field(
    declarations: object, message: str
) -> None:
    with pytest.raises(BindingValidationError, match=re.escape(message)):
        validate_bindings(declarations, inventory=_FACT_INVENTORY, runtime_contract={})


def test_array_items_selector_is_undocumented() -> None:
    declaration = _declaration(expected_type="integer", selector="value.a.items")

    with pytest.raises(BindingValidationError, match="undocumented selector"):
        validate_bindings([declaration], inventory=_FACT_INVENTORY, runtime_contract={})


_CONTENT_INVENTORY = {
    "facts": [
        {
            "ref": "msg",
            "value": {"content": [{"text": "hi"}], "items": {"text": "kept"}},
            "schema": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    },
                    "items": {"type": "object", "properties": {"text": {"type": "string"}}},
                },
            },
        }
    ]
}


def _selector_findings(selector: str) -> list[tuple[str, str, str]]:
    findings = _collect_binding_findings(
        [_declaration(source_ref="facts:msg", selector=selector)],
        _CONTENT_INVENTORY,
        {},
        finding_code="plan_binding_validation",
    )
    return _coded_findings(findings)


@pytest.mark.parametrize(
    "selector",
    [
        "value.content.items.text",
        "value.content.items",
        "value.content[0].text",
        "value.content[0]",
        "value.content.0.text",
    ],
)
def test_plan_rejects_a_selector_that_steps_into_an_array_with_its_own_code(
    selector: str,
) -> None:
    findings = _selector_findings(selector)

    assert [(code, path) for code, _detail, path in findings] == [
        ("selector_through_array", "runtime_bindings[0].selector")
    ]
    detail = findings[0][1]
    assert selector in detail
    assert "array at value.content" in detail
    assert "undocumented" not in detail


def test_plan_accepts_a_property_named_items_and_the_array_itself() -> None:
    assert _selector_findings("value.items.text") == []
    assert _selector_findings("value.content") == [
        (
            "plan_binding_validation",
            "binding type mismatch for n: expected string, source is array",
            "runtime_bindings[0].selector",
        )
    ]


def test_plan_still_reports_an_undocumented_selector_below_an_array_as_undocumented() -> None:
    findings = _selector_findings("value.content.first")

    assert [code for code, _detail, _path in findings] == ["plan_binding_validation"]
    assert "undocumented selector" in findings[0][1]


def test_supplied_binding_values_skip_unresolvable_sources() -> None:
    declarations = [
        _declaration(name="a", selector="value.a"),
        _declaration(name="i", selector="value.a.items"),
        _declaration(name="m", selector="value.missing"),
        _declaration(name="r", selector="result.s"),
    ]

    assert supplied_binding_values(declarations, _FACT_INVENTORY) == {"a": [1, 2]}
    assert supplied_binding_values("x", _FACT_INVENTORY) == {}
    assert supplied_binding_values(declarations, {"facts": "x"}) == {}


def test_user_text_consumer_mismatches_skip_malformed_declarations() -> None:
    declarations = [
        5,
        {"name": 1, "consumers": []},
        {"name": "n", "consumers": ["stimulus.user_text"]},
    ]

    assert find_stimulus_user_text_consumer_mismatches(declarations, 7) == ()
    assert find_stimulus_user_text_consumer_mismatches(declarations, "hello") == (
        {"binding_index": 2, "consumer_index": 0, "binding_name": "n", "value_available": False},
    )


def test_named_record_facts_searches_every_records_companion() -> None:
    record = {"type": "object", "properties": {"record_key": {"type": "string"}}}
    keyed = {"type": "object", "properties": {"K": record}}
    inventory = {
        "facts": [
            {"ref": ref, "schema": keyed}
            for ref in ("state:a", "state:a:records", "state:b", "state:b:records")
        ]
    }

    assert named_record_facts("supplied_input", "facts:state:b:K", inventory) == (
        "K",
        (("state:b", record), ("state:b:records", record)),
    )


def _owner_user_text_binding(*, consumers: list[str]) -> dict:
    return {
        "name": "owner",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:order",
        "selector": "value.customer_id",
        "consumers": consumers,
        "on_missing": "stop",
    }


def _owner_inventory() -> dict:
    return {
        "facts": [
            {
                "ref": "order",
                "value": {"customer_id": "customer-a"},
                "schema": {
                    "type": "object",
                    "properties": {"customer_id": {"type": "string"}},
                },
            }
        ]
    }


def test_user_text_consumer_fails_when_value_is_absent_and_passes_when_present() -> None:
    binding = _owner_user_text_binding(consumers=["stimulus.user_text"])
    values = supplied_binding_values([binding], _owner_inventory())

    absent = find_stimulus_user_text_consumer_mismatches(
        [binding],
        "Please process the order.",
        resolved_values=values,
    )
    present = find_stimulus_user_text_consumer_mismatches(
        [binding],
        "Please process the order for customer-a.",
        resolved_values=values,
    )
    slot = find_stimulus_user_text_consumer_mismatches(
        [binding],
        "Please process {{owner}}'s order.",
        resolved_values=values,
    )

    assert absent[0]["binding_name"] == "owner"
    assert present == ()
    assert slot == ()


@pytest.mark.parametrize(
    ("consumer", "closed"),
    [
        ("stimulus.user_text", True),
        ("stimulus.history", True),
        ("judge.owner", True),
        ("prerequisites.owner", True),
        ("setup.arguments.loan_id", True),
        ("stimulus.history.extra", False),
        ("judge", False),
        ("detector.owner", False),
        ("observation_claim.owner", False),
    ],
)
def test_binding_spec_closes_consumers_for_parser_and_collector(
    consumer: str, closed: bool
) -> None:
    raw = {
        "name": "owner",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:owner",
        "selector": "value",
        "consumers": [consumer],
        "on_missing": "stop",
    }

    findings = _collect_binding_findings([dict(raw)], {"facts": []}, {})
    consumer_findings = [f for f in findings if f.path == "runtime_bindings[0].consumers[0]"]

    assert BINDING_SPEC.is_closed_consumer(consumer) is closed
    assert (not consumer_findings) is closed
    if closed:
        assert RuntimeBinding.from_dict(raw).consumers == (consumer,)
    else:
        with pytest.raises(BindingValidationError, match="not a closed path"):
            RuntimeBinding.from_dict(raw)


def test_binding_spec_is_the_contract_schema_vocabulary() -> None:
    contract = _binding_contract()
    item = _binding_list_schema()["items"]

    assert contract["required"] == list(BINDING_SPEC.fields) == item["required"]
    assert list(item["properties"]) == list(BINDING_SPEC.fields)
    assert contract["expected_type"]["enum"] == list(BINDING_SPEC.expected_types)
    assert contract["source_kind"]["enum"] == list(BINDING_SPEC.source_kinds)
    assert contract["on_missing"]["enum"] == list(BINDING_SPEC.missing_policies)
    assert BINDING_SPEC.string_fields == tuple(
        name for name, schema in item["properties"].items() if schema["type"] == "string"
    )


def _consumer_rule_destinations(rule: str) -> list[str]:
    """Return the destination entries a consumer rule lists, without their annotations."""

    after_lead = rule.split("Write each entry as exactly one of: ", 1)[1]
    listed = after_lead.split(". Write the actual", 1)[0]
    entries = listed.replace("; or ", "; ").split("; ")
    return [entry.split(", where ", 1)[0] for entry in entries]


def test_consumer_rule_lists_the_spec_destinations_in_spec_order() -> None:
    rule = _binding_contract()["consumer_rule"]

    assert _consumer_rule_destinations(rule) == list(BINDING_SPEC.consumer_destinations)
    assert BINDING_SPEC.consumer_destinations == (
        "stimulus.user_text",
        "stimulus.history",
        "prerequisites.<binding name>",
        "judge.<binding name>",
        "setup.arguments.<argument name>",
    )


def _plan_with_consumer(consumer: str, claim_level: str) -> dict:
    plan = _plan()
    plan["observation_claim"]["claim_level"] = claim_level
    plan["runtime_bindings"] = [
        _declaration(
            name="status",
            source_ref="facts:draft:status",
            selector="value",
            consumers=[consumer],
        )
    ]
    return plan


def _consumer_plan_findings(consumer: str, claim_level: str) -> list[tuple[str, str, str]]:
    findings = collect_plan_findings_v2(
        _plan_with_consumer(consumer, claim_level), _inventory(), _runtime_contract()
    )
    return [
        (f.code, f.detail, f.path)
        for f in findings
        if f.path.startswith("runtime_bindings[0].consumers")
    ]


def test_judge_consumer_is_accepted_on_a_reply_claim() -> None:
    assert _consumer_plan_findings("judge.status", "reply") == []


def test_judge_consumer_on_a_command_attempt_claim_is_a_finding() -> None:
    assert _consumer_plan_findings("judge.status", "command_attempt") == [
        (
            "plan_binding_validation",
            "binding consumer judge.status is valid only when "
            "observation_claim.claim_level is reply",
            "runtime_bindings[0].consumers[0]",
        )
    ]


@pytest.mark.parametrize("claim_level", ["reply", "command_attempt"])
def test_detector_consumer_is_a_finding_without_an_alias(claim_level: str) -> None:
    assert _consumer_plan_findings("detector.status", claim_level) == [
        (
            "plan_binding_validation",
            "binding consumer detector.status is not a closed path; the semantic "
            "judge of a reply claim reads judge.status",
            "runtime_bindings[0].consumers[0]",
        )
    ]


def test_judge_consumer_check_skips_declarations_that_have_no_consumer_list() -> None:
    plan = _plan_with_consumer("judge.status", "command_attempt")
    plan["runtime_bindings"] = [
        "not a declaration",
        {"name": "no_consumers"},
        {"name": "text_consumers", "consumers": "judge.status"},
        {"name": "mixed", "consumers": [3, "judge.status"]},
    ]

    findings = collect_plan_findings_v2(plan, _inventory(), _runtime_contract())

    assert [f.path for f in findings if f.detail.endswith("claim_level is reply")] == [
        "runtime_bindings[3].consumers[1]"
    ]


def test_consumer_without_a_claim_level_leaves_the_claim_finding_alone() -> None:
    plan = _plan_with_consumer("judge.status", "reply")
    plan["observation_claim"]["claim_level"] = "unknown"

    findings = collect_plan_findings_v2(plan, _inventory(), _runtime_contract())

    assert [f.path for f in findings if f.path.startswith("runtime_bindings[0].consumers")] == []


def test_consumer_rule_follows_a_changed_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    extra_family = ConsumerPrefix("extra.", "thing name")
    changed = dataclasses.replace(
        BINDING_SPEC,
        consumer_paths=(*BINDING_SPEC.consumer_paths, "stimulus.extra"),
        consumer_prefixes=(*BINDING_SPEC.consumer_prefixes, extra_family),
    )
    monkeypatch.setattr(contracts, "BINDING_SPEC", changed)

    rule = contracts._binding_contract()["consumer_rule"]

    assert _consumer_rule_destinations(rule) == list(changed.consumer_destinations)
    assert "extra.<thing name>" in rule and "stimulus.extra" in rule


def test_runtime_bindings_review_check_has_one_clause_per_spec_field() -> None:
    check = next(c for c in PLAN_MECHANICAL_CHECKS if c.check_id == "runtime_bindings")
    guarantee = check.guarantee

    assert set(_RUNTIME_BINDING_CLAUSES) == set(BINDING_SPEC.fields)
    assert all(clause in guarantee for clause in _RUNTIME_BINDING_CLAUSES.values())


def test_binding_spec_field_set_drives_both_field_checks() -> None:
    raw = {"name": "owner", "consumers": ["stimulus.user_text"], "extra": 1}

    findings = _collect_binding_findings([raw], {"facts": []}, {})
    missing = sorted(set(BINDING_SPEC.fields) - set(raw))

    assert [f.detail for f in findings[: len(missing) + 1]] == [
        *(f"binding missing field: {name}" for name in missing),
        "binding has unsupported field: extra",
    ]
    with pytest.raises(BindingValidationError, match=r"missing=\['expected_type'"):
        RuntimeBinding.from_dict(raw)


def _coded_findings(findings: list) -> list[tuple[str, str, str]]:
    return [(f.code, f.detail, f.path) for f in findings]


def test_collector_reports_a_duplicate_binding_name_at_the_second_declaration() -> None:
    findings = _collect_binding_findings(
        [_declaration(), _declaration()], _FACT_INVENTORY, {}, finding_code="plan_validation"
    )

    assert _coded_findings(findings) == [
        ("plan_validation", "duplicate binding: n", "runtime_bindings[1]")
    ]


def test_collector_reports_a_non_string_field_beside_the_other_checks() -> None:
    findings = _collect_binding_findings([_declaration(selector=7)], _FACT_INVENTORY, {})

    assert _coded_findings(findings) == [
        (
            "artifact_validation",
            "binding selector must be a string",
            "runtime_bindings[0].selector",
        ),
        (
            "artifact_validation",
            "binding selector must be a string: n",
            "runtime_bindings[0].selector",
        ),
    ]


@pytest.mark.parametrize(
    ("consumers", "detail", "path"),
    [
        ("judge.n", "binding consumers must be a list", "runtime_bindings[0].consumers"),
        ([], "binding consumers must be non-empty strings", "runtime_bindings[0].consumers"),
        (
            ["judge.n", 3],
            "binding consumers must be non-empty strings",
            "runtime_bindings[0].consumers[1]",
        ),
        (
            ["judge.n", " "],
            "binding consumers must be non-empty strings",
            "runtime_bindings[0].consumers[1]",
        ),
        (
            ["judge.n", "elsewhere.n"],
            "binding consumer is not a closed path",
            "runtime_bindings[0].consumers[1]",
        ),
    ],
)
def test_collector_reports_each_malformed_consumer_list(
    consumers: object, detail: str, path: str
) -> None:
    findings = _collect_binding_findings([_declaration(consumers=consumers)], _FACT_INVENTORY, {})

    consumer_findings = [f for f in findings if f.path.startswith("runtime_bindings[0].consumers")]
    assert _coded_findings(consumer_findings) == [("artifact_validation", detail, path)]


def test_collector_falls_back_to_the_closed_validator_for_record_key_companions() -> None:
    schema = {
        "type": "object",
        "properties": {
            "ORD-1": {"type": "object", "properties": {"record_key": {"type": "string"}}}
        },
    }
    inventory = {
        "facts": [
            {
                "ref": "state:orders:records",
                "schema": schema,
                "provenance": {
                    "derived_from": "state:orders",
                    "derivation": "keyed_map_record_key",
                },
            }
        ]
    }
    declaration = _declaration(
        expected_type="object",
        source_ref="facts:state:orders:records",
        selector="value.ORD-1",
    )

    findings = _collect_binding_findings([declaration], inventory, {})

    assert [(f.code, f.path) for f in findings] == [("artifact_validation", "runtime_bindings[0]")]
    assert "value.<key>.record_key" in findings[0].detail
    assert findings[0].detail.startswith("binding n selects value.ORD-1 from facts:state:orders")
