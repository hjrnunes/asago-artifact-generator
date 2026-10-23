"""Validating, resolving and substituting a package's bindings, and the codes they carry."""

from __future__ import annotations

from typing import Any

import pytest

from asago_bundle_core.bindings import (
    BindingError,
    RuntimeBinding,
    resolve_bindings,
    select_value,
    substitute_slots,
    validate_binding_declarations,
)
from asago_bundle_core.preparation import _binding_reason

_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "order": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "total": {"type": "integer"},
                "lines": {"type": "array", "items": {"type": "number"}},
                "records": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"sku": {"type": "string"}},
                    },
                },
                "broken": {"type": "object", "properties": "not-a-dict"},
            },
        },
        "tags": {"type": "array", "items": "not-a-schema"},
        "label": {"type": "string"},
    },
}
_INVENTORY = {
    "operations": [{"name": "create_order", "result_schema": _RESULT_SCHEMA}],
    "facts": [{"ref": "customer", "schema": {"type": "string"}}],
}
_CONTRACT = {"setup_permissions": ["create_order"]}


def _binding(selector: str, expected_type: str, **overrides: Any) -> dict[str, Any]:
    binding = {
        "name": "bound",
        "expected_type": expected_type,
        "source_kind": "setup_output",
        "source_ref": "setup:create_order",
        "selector": selector,
        "consumers": ["judge.bound"],
        "on_missing": "stop",
    }
    binding.update(overrides)
    return binding


def _validate(binding: dict[str, Any]) -> Any:
    return validate_binding_declarations(
        [binding], inventory=_INVENTORY, runtime_contract=_CONTRACT
    )


@pytest.mark.parametrize(
    ("selector", "expected_type"),
    [
        ("result.order.id", "string"),
        ("result.order.total", "number"),
        ("result.order.lines", "array"),
        ("result.label", "string"),
        ("result", "object"),
    ],
)
def test_documented_selectors_resolve_to_their_schema_type(
    selector: str, expected_type: str
) -> None:
    (binding,) = _validate(_binding(selector, expected_type))

    assert binding.selector == selector


def test_supplied_fact_selector_uses_value_root() -> None:
    (binding,) = _validate(
        _binding(
            "value",
            "string",
            source_kind="supplied_input",
            source_ref="facts:customer",
        )
    )

    assert binding.source_ref == "facts:customer"


@pytest.mark.parametrize(
    "selector",
    [
        "output.order.id",
        "result.missing",
        "result.order.broken.id",
        "result.order.id.deeper",
        "result.order.lines.first",
        "result.label.length",
    ],
)
def test_undocumented_selectors_are_rejected(selector: str) -> None:
    with pytest.raises(BindingError) as error:
        _validate(_binding(selector, "string"))

    assert str(error.value) == f"undocumented selector for binding bound: {selector}"


@pytest.mark.parametrize(
    ("selector", "array_path"),
    [
        ("result.order.lines.items", "result.order.lines"),
        ("result.order.lines.0", "result.order.lines"),
        ("result.order.lines[0]", "result.order.lines"),
        ("result.order.records.items.sku", "result.order.records"),
        ("result.tags.items", "result.tags"),
        ("result.tags.items.more", "result.tags"),
    ],
)
def test_a_selector_that_steps_into_an_array_is_rejected(selector: str, array_path: str) -> None:
    # select_value reads object keys only, so it cannot execute such a selector.
    with pytest.raises(BindingError) as error:
        _validate(_binding(selector, "number"))

    assert str(error.value) == (
        f"selector for binding bound steps into the array at {array_path}: {selector}"
    )
    assert error.value.code == "binding_invalid"


def test_a_selector_rooted_elsewhere_is_undocumented_before_any_array_step() -> None:
    with pytest.raises(BindingError) as error:
        _validate(_binding("output.order.lines.items", "number"))

    assert str(error.value) == (
        "undocumented selector for binding bound: output.order.lines.items"
    )


@pytest.mark.parametrize(
    ("overrides", "selector", "message"),
    [
        (
            {},
            "value.order.id",
            "must be rooted at result for setup_output, not value: value.order.id",
        ),
        (
            {"source_kind": "supplied_input", "source_ref": "facts:customer"},
            "result",
            "must be rooted at value for supplied_input, not result: result",
        ),
    ],
)
def test_a_selector_rooted_for_the_other_source_kind_is_rejected(
    overrides: dict[str, Any], selector: str, message: str
) -> None:
    # resolve_bindings reads only the root of the binding's own source kind.
    with pytest.raises(BindingError) as error:
        _validate(_binding(selector, "string", **overrides))

    assert str(error.value) == f"selector for binding bound {message}"
    assert error.value.code == "binding_invalid"


def test_selector_type_must_match_the_expected_type() -> None:
    with pytest.raises(BindingError) as error:
        _validate(_binding("result.order.id", "integer"))

    assert str(error.value) == (
        "binding type mismatch for bound: expected integer, source is string"
    )


@pytest.mark.parametrize(
    ("declarations", "message"),
    [
        ({}, "runtime_bindings must be a list"),
        ([_binding("result", "object", name="")], "duplicate or blank binding: ''"),
        (
            [_binding("result", "object"), _binding("result", "object")],
            "duplicate or blank binding: 'bound'",
        ),
        ([_binding("result", "date")], "expected_type is not closed: bound"),
        ([_binding("result", "object", source_kind="x")], "source_kind is not closed"),
        ([_binding("result", "object", on_missing="x")], "on_missing is not closed"),
        (
            [_binding("result", "object", source_ref="setup:delete_order")],
            "unknown setup operation: delete_order",
        ),
        (
            [
                _binding(
                    "value",
                    "string",
                    source_kind="supplied_input",
                    source_ref="facts:unknown",
                )
            ],
            "missing source schema for binding: bound",
        ),
    ],
)
def test_invalid_declarations_are_rejected(declarations: Any, message: str) -> None:
    with pytest.raises(BindingError, match=message):
        validate_binding_declarations(
            declarations, inventory=_INVENTORY, runtime_contract=_CONTRACT
        )


def test_non_string_expected_type_is_rejected_as_not_closed() -> None:
    with pytest.raises(BindingError) as error:
        _validate(_binding("result", ["list"]))

    assert str(error.value) == "binding expected_type is not closed: bound"


def test_non_string_source_kind_is_rejected_as_not_closed() -> None:
    with pytest.raises(BindingError) as error:
        _validate(_binding("result", "object", source_kind=["list"]))

    assert str(error.value) == "binding source_kind is not closed: bound"


def test_non_string_on_missing_is_rejected_as_not_closed() -> None:
    with pytest.raises(BindingError) as error:
        _validate(_binding("result", "object", on_missing=["list"]))

    assert str(error.value) == "binding on_missing is not closed: bound"
    assert error.value.code == "binding_missing"


def test_binding_consumers_must_not_be_empty() -> None:
    with pytest.raises(BindingError) as error:
        _validate(_binding("result", "object", consumers=[]))

    assert str(error.value) == "binding consumers must be non-empty strings: bound"
    assert error.value.code == "binding_invalid"


def test_binding_consumer_must_be_a_closed_destination() -> None:
    with pytest.raises(BindingError) as error:
        _validate(_binding("result", "object", consumers=["somewhere.else"]))

    assert str(error.value) == ("binding consumer is not a closed path for bound: somewhere.else")
    assert error.value.code == "binding_invalid"


def test_setup_binding_requires_a_permitted_operation() -> None:
    with pytest.raises(BindingError, match="not permitted: create_order"):
        validate_binding_declarations(
            [_binding("result", "object")],
            inventory=_INVENTORY,
            runtime_contract={"setup_permissions": []},
        )


@pytest.mark.parametrize(
    "permissions",
    [
        "create_order",
        "create_order_v2",
        "x create_order y",
        {"create_order": True},
        {},
        "",
        None,
        7,
    ],
)
def test_a_setup_permissions_value_that_is_not_a_list_permits_nothing(
    permissions: Any,
) -> None:
    with pytest.raises(BindingError, match="not permitted: create_order") as error:
        validate_binding_declarations(
            [_binding("result", "object")],
            inventory=_INVENTORY,
            runtime_contract={"setup_permissions": permissions},
        )

    assert error.value.code == "binding_invalid"


def test_a_longer_listed_name_does_not_permit_a_shorter_operation() -> None:
    with pytest.raises(BindingError, match="not permitted: create_order"):
        validate_binding_declarations(
            [_binding("result", "object")],
            inventory=_INVENTORY,
            runtime_contract={"setup_permissions": ["create_order_v2"]},
        )


def test_a_listed_operation_is_permitted() -> None:
    result = validate_binding_declarations(
        [_binding("result", "object")],
        inventory=_INVENTORY,
        runtime_contract={"setup_permissions": ["x", "create_order"]},
    )

    assert [item.name for item in result] == ["bound"]


def _declaration_error(declarations: Any) -> BindingError:
    with pytest.raises(BindingError) as error:
        validate_binding_declarations(
            declarations, inventory=_INVENTORY, runtime_contract=_CONTRACT
        )
    return error.value


def _raised(call: Any) -> BindingError:
    with pytest.raises(BindingError) as error:
        call()
    return error.value


_DECLARED = (RuntimeBinding.parse(_binding("result.order.id", "string")),)


@pytest.mark.parametrize(
    ("raise_error", "code"),
    [
        (lambda: _declaration_error({}), "binding_invalid"),
        (lambda: _declaration_error(["x"]), "binding_invalid"),
        (lambda: _declaration_error([{"name": "bound"}]), "binding_missing"),
        (
            lambda: _declaration_error([_binding("result", "object", consumers=[""])]),
            "binding_invalid",
        ),
        (
            lambda: _declaration_error([_binding("result.nope", "string")]),
            "binding_missing",
        ),
        (
            lambda: _declaration_error([_binding("result.label", "integer")]),
            "binding_invalid",
        ),
        (lambda: _declaration_error([_binding("result", "date")]), "binding_invalid"),
        (
            lambda: _declaration_error([_binding("result", "object", on_missing="x")]),
            "binding_missing",
        ),
        (
            lambda: _declaration_error([_binding("result", "object", source_ref="x")]),
            "binding_invalid",
        ),
        (
            lambda: _declaration_error(
                [
                    _binding(
                        "value",
                        "string",
                        source_kind="supplied_input",
                        source_ref="facts:x",
                    )
                ]
            ),
            "binding_missing",
        ),
        (lambda: _raised(lambda: select_value({}, "a..b")), "binding_missing"),
        (lambda: _raised(lambda: select_value({}, "a")), "binding_missing"),
        (
            lambda: _raised(lambda: substitute_slots("{{bound}}", {}, _DECLARED)),
            "binding_missing",
        ),
        (
            lambda: _raised(lambda: substitute_slots("{{bound}}", {"bound": 1}, _DECLARED)),
            "binding_mistyped",
        ),
        (
            lambda: _raised(lambda: substitute_slots("{{other}}", {}, _DECLARED)),
            "binding_invalid",
        ),
        (
            lambda: _raised(lambda: resolve_bindings(_DECLARED, setup_outputs={})),
            "binding_missing",
        ),
        (
            lambda: _raised(
                lambda: resolve_bindings(
                    _DECLARED, setup_outputs={"create_order": {"order": {"id": 7}}}
                )
            ),
            "binding_mistyped",
        ),
    ],
)
def test_binding_errors_carry_the_code_preparation_reports(raise_error: Any, code: str) -> None:
    error = raise_error()

    assert error.code == code
    assert _binding_reason(error) == code


@pytest.mark.parametrize("word", ["mistyped", "missing", "not found", "selector"])
def test_an_interpolated_name_does_not_change_the_recorded_reason(word: str) -> None:
    error = _raised(lambda: substitute_slots("{{other}}", {}, _DECLARED))
    named = BindingError(f"undeclared slot: {word}", code="binding_invalid")

    assert _binding_reason(named) == _binding_reason(error) == "binding_invalid"


def test_a_code_the_receipt_does_not_name_is_recorded_as_binding_invalid() -> None:
    error = BindingError("stimulus history must be a list", code="stimulus_invalid")

    assert _binding_reason(error) == "binding_invalid"


def test_a_binding_code_is_recorded_whatever_the_message_says() -> None:
    assert _binding_reason(BindingError("odd", code="binding_missing")) == "binding_missing"
    assert _binding_reason(BindingError("odd", code="binding_mistyped")) == "binding_mistyped"
