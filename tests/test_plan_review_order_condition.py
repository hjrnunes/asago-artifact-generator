"""The plan reviewer is shown which operation an order comparison requires first."""

from __future__ import annotations

from asago_artifact_generator.authoring.review import (
    build_artifact_review_packet,
    build_plan_review_packet,
)
from asago_artifact_generator.input_adapter import load_input

from .support import (
    HANDOFF_V3_KIT,
    NO_CONDITION_HANDOFF,
    NOT_CALLED_HANDOFF,
    OBSERVED_HANDOFF,
    world_builders,
)

_inventory, _metadata, _plan, _runtime_contract = world_builders(
    "ehr", "inventory", "metadata", "plan", "runtime_contract"
)

_ORDER_HANDOFF = HANDOFF_V3_KIT / "valid" / "functional-record-unavailable.json"


def _rules(source) -> dict:
    packet = build_plan_review_packet(
        load_input(source), _plan(), _inventory(), _runtime_contract()
    )
    return packet.payload["binding_and_setup_rules"]


def test_the_plan_reviewer_sees_the_operation_an_order_comparison_requires_first() -> None:
    rules = _rules(_ORDER_HANDOFF)

    order = rules["order_comparisons"]
    assert order["comparisons"] == [
        {
            "operation": "transfer_funds",
            "requires_prior": "verify_identity",
            "same_argument": "account_id",
        }
    ]
    assert "no earlier captured call to its requires_prior operation" in order["meaning"]
    assert "the producer's condition, not the plan, names it" in order["meaning"]


def test_the_order_comparison_is_rendered_in_the_plan_review_prompt() -> None:
    packet = build_plan_review_packet(
        load_input(_ORDER_HANDOFF), _plan(), _inventory(), _runtime_contract()
    )

    assert packet.user.count('"requires_prior": "verify_identity"') == 2
    assert packet.user.count("order_comparisons") == 1


def test_the_artifact_reviewer_prompt_has_no_order_comparison_section() -> None:
    packet = build_artifact_review_packet(
        load_input(_ORDER_HANDOFF), _plan(), _metadata(), _inventory(), _runtime_contract()
    )

    assert "order_comparisons" not in packet.user


def test_a_condition_without_an_order_comparison_adds_no_order_rule() -> None:
    for source in (OBSERVED_HANDOFF, NOT_CALLED_HANDOFF, NO_CONDITION_HANDOFF):
        assert "order_comparisons" not in _rules(source), source.name


def test_an_order_comparison_without_same_argument_lists_only_its_two_operations() -> None:
    view = load_input(_ORDER_HANDOFF)
    comparison = view.payload["discriminating_condition"]["comparisons"][1]
    comparison.pop("same_argument")

    packet = build_plan_review_packet(view, _plan(), _inventory(), _runtime_contract())

    assert packet.payload["binding_and_setup_rules"]["order_comparisons"]["comparisons"] == [
        {"operation": "transfer_funds", "requires_prior": "verify_identity"}
    ]
