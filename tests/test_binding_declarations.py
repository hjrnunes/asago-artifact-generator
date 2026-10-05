"""Offline tests for closed runtime binding declarations."""

from __future__ import annotations

import re

import pytest

from asago_artifact_generator.bindings import (
    BindingValidationError,
    RuntimeBinding,
    canonical_binding_paths,
    find_stimulus_user_text_consumer_mismatches,
    named_record_facts,
    supplied_binding_values,
    validate_bindings,
)


def test_binding_uses_exact_setup_selector_and_declared_consumers() -> None:
    binding = RuntimeBinding(
        name="draft_id",
        expected_type="string",
        source_kind="setup_output",
        source_ref="setup:summarize_for_ehr",
        selector="result.draft.id",
        consumers=("stimulus.user_text", "prerequisites.draft", "detector.evidence"),
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
                "consumers": ["detector.record"],
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
                "consumers": ["detector.record_value"],
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
                    "consumers": ["detector.record_value"],
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
            "consumers": ["detector.item_id"],
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
                    "consumers": ["detector.item_id"],
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
        "consumers": ["detector.item_owner"],
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
        "consumers": ["detector.n"],
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


def test_array_items_selector_documents_the_item_type() -> None:
    declaration = _declaration(expected_type="integer", selector="value.a.items")

    validated = validate_bindings([declaration], inventory=_FACT_INVENTORY, runtime_contract={})

    assert validated[0].selector == "value.a.items"


def test_supplied_binding_values_skip_unresolvable_sources() -> None:
    declarations = [
        _declaration(name="a", selector="value.a.items"),
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
