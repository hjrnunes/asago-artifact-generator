"""Offline tests for closed runtime binding declarations."""

from __future__ import annotations

import pytest

from asago_artifact_generator.bindings import (
    BindingValidationError,
    RuntimeBinding,
    substitute_slots,
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


def test_binding_rejects_undocumented_selector_and_open_type() -> None:
    with pytest.raises(BindingValidationError, match="closed"):
        validate_bindings(
            [
                {
                    "name": "draft_id",
                    "expected_type": "any",
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


def test_binding_rejects_unknown_selector_with_closed_type() -> None:
    with pytest.raises(BindingValidationError, match="undocumented selector"):
        validate_bindings(
            [
                {
                    "name": "draft_id",
                    "expected_type": "string",
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


def test_substitution_accepts_declared_slots_only_and_never_evaluates_text() -> None:
    binding = RuntimeBinding(
        name="draft_id",
        expected_type="string",
        source_kind="supplied_input",
        source_ref="facts:draft",
        selector="value.id",
        consumers=("stimulus.user_text",),
        on_missing="stop",
    )
    assert substitute_slots("Commit {{draft_id}}.", {"draft_id": "d-1"}, [binding]) == (
        "Commit d-1."
    )
    with pytest.raises(BindingValidationError, match="undeclared"):
        substitute_slots("{{__import__('os').getcwd()}}", {}, [binding])


def _keyed_inventory() -> dict:
    return {
        "operations": [],
        "facts": [
            {
                "ref": "state:orders",
                "value": {
                    "ORD-1": {"customer_id": "CUST-1", "status": "open"},
                },
                "schema": {
                    "type": "object",
                    "properties": {
                        "ORD-1": {
                            "type": "object",
                            "properties": {
                                "customer_id": {"type": "string"},
                                "status": {"type": "string"},
                            },
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
        ],
    }


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
