"""One declaration-validation engine: the package and authoring policies differ only by option."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from asago_artifact_generator.binding_rules import (
    PACKAGE_POLICY,
    BindingError,
    check_declarations,
    validate_binding_declarations,
)
from asago_artifact_generator.bindings import (
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


def _binding(**overrides: Any) -> dict[str, Any]:
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


def _kinds(policy: Any, declarations: list[Any], contract: dict[str, Any]) -> list[str]:
    checked = check_declarations(
        declarations, inventory=_INVENTORY, runtime_contract=contract, policy=policy
    )
    return [violation.kind for violation in checked.violations]


@pytest.mark.parametrize(
    ("declaration", "contract", "package", "authoring"),
    [
        (_binding(consumers=[]), {"setup_permissions": ["prepare"]}, ["consumers_empty"], []),
        (
            _binding(),
            {"setup_permissions": "prepare"},
            ["operation_not_permitted"],
            [],
        ),
        (
            _binding(source_kind="supplied_input", source_ref="customer", selector="value"),
            {},
            [],
            ["source_ref_form"],
        ),
        (
            _binding(source_kind="supplied_input", source_ref="facts:other", selector="value"),
            {},
            ["schema_missing"],
            ["unknown_fact"],
        ),
        (_binding(name=" "), {"setup_permissions": ["prepare"]}, [], ["name_blank"]),
        (_binding(source_ref=7), {"setup_permissions": ["prepare"]}, None, ["string_field"]),
    ],
)
def test_each_difference_is_a_policy_option_of_the_same_engine(
    declaration: dict[str, Any],
    contract: dict[str, Any],
    package: list[str] | None,
    authoring: list[str],
) -> None:
    if package is None:
        # A package declaration is not type-checked field by field: reading a
        # non-string source reference fails as it does at execution.
        with pytest.raises(AttributeError):
            _kinds(PACKAGE_POLICY, [declaration], contract)
    else:
        assert _kinds(PACKAGE_POLICY, [declaration], contract) == package
    assert _kinds(AUTHORING_POLICY, [declaration], contract) == authoring


def test_each_caller_words_the_violation_its_own_way() -> None:
    declarations = [_binding(), _binding()]
    contract = {"setup_permissions": ["prepare"]}

    with pytest.raises(BindingError, match=r"^duplicate or blank binding: 'bound'$"):
        validate_binding_declarations(
            declarations, inventory=_INVENTORY, runtime_contract=contract
        )
    with pytest.raises(BindingValidationError, match=r"^duplicate binding: bound$"):
        validate_bindings(
            [_binding(), _binding(selector="result")],
            inventory=_INVENTORY,
            runtime_contract=contract,
        )


def test_without_stop_at_first_the_engine_reports_every_chain() -> None:
    policy = replace(
        PACKAGE_POLICY,
        phases=((("expected_type",), ("on_missing",)),),
        stop_at_first=False,
    )

    kinds = _kinds(policy, [_binding(expected_type="date", on_missing="x")], {})

    assert kinds == ["expected_type_not_closed", "on_missing_not_closed"]
