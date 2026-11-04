"""The library owns the binding and slot rules package preparation validates with.

Preparation runs downstream of authoring, so the rules it uses live in the member that
declares only pydantic, jsonschema and ``asago-contracts-core``. These cases read the rules
through the member's own import path, with hand-built values, so the member's session covers
them without the authoring application.
"""

from __future__ import annotations

from typing import Any

import pytest

from asago_package.binding_rules import (
    BindingError,
    RuntimeBinding,
    slot_names,
    validate_binding_declarations,
    validate_slot_references,
)
from asago_package.bindings import (
    AUTHORING_POLICY,
    BindingValidationError,
    validate_bindings,
)

_INVENTORY = {
    "operations": [
        {
            "name": "prepare",
            "result_schema": {"type": "object", "properties": {"id": {"type": "string"}}},
        }
    ],
    "facts": [{"ref": "customer", "schema": {"type": "string"}}],
}
_CONTRACT = {"setup_permissions": ["prepare"]}


def _declaration(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "bound",
        "expected_type": "string",
        "source_kind": "setup_output",
        "source_ref": "setup:prepare",
        "selector": "result.id",
        "consumers": ["judge.bound"],
        "on_missing": "stop",
        **overrides,
    }


def _declarations(*declarations: dict[str, Any]) -> tuple[RuntimeBinding, ...]:
    return validate_binding_declarations(
        list(declarations), inventory=_INVENTORY, runtime_contract=_CONTRACT
    )


def test_the_member_checks_a_hand_built_package_declaration() -> None:
    assert _declarations(_declaration()) == (
        RuntimeBinding(
            name="bound",
            expected_type="string",
            source_kind="setup_output",
            source_ref="setup:prepare",
            selector="result.id",
            consumers=("judge.bound",),
            on_missing="stop",
        ),
    )


def test_the_member_reports_an_unclosed_package_declaration() -> None:
    with pytest.raises(BindingError, match=r"^binding consumers must be non-empty strings"):
        _declarations(_declaration(consumers=[]))


def test_the_member_checks_a_slot_against_the_declarations() -> None:
    bindings = _declarations(_declaration())

    validate_slot_references("the order for {{bound}}", bindings)
    with pytest.raises(BindingError, match=r"^undeclared slot: other$"):
        validate_slot_references("the order for {{other}}", bindings)


def test_the_member_reads_the_slot_names_in_first_appearance_order() -> None:
    assert slot_names("a {{bound}} b {{other}} c {{bound}}") == ["bound", "other"]


def test_the_member_keeps_authorings_policy_and_wording_beside_the_rules() -> None:
    assert AUTHORING_POLICY.phases

    with pytest.raises(BindingValidationError, match=r"^duplicate binding: bound$"):
        validate_bindings(
            [_declaration(), _declaration(selector="result")],
            inventory=_INVENTORY,
            runtime_contract=_CONTRACT,
        )
