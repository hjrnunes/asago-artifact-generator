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
