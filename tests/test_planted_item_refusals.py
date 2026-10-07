"""An indirect shape stops before any model request unless the target can carry it."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.planted_item import PlantedTarget, choose_planted_target
from asago_artifact_generator.authoring.shape_gate import shape_refusal_findings
from asago_artifact_generator.input_adapter import load_input

from .support import ScriptedAuthoringTransport, load_failure_evidence, stage_local_orchestrator

_KIT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff" / "handoff-v4"
LISTING = "adversarial-indirect-listing.json"
POLICY = "adversarial-indirect-policy-operator.json"

LISTING_SLOT = {
    "id": "airbnb.listing.title",
    "carrier_operation": "get_listing",
    "field": "title",
    "controller": "counterparty",
    "content_kind": "listing_content",
    "records": ["LST-101", "LST-104"],
}
POLICY_SLOT = {
    "id": "airbnb.policy_doc.body",
    "carrier_operation": "retrieve_policy",
    "field": "documents[].body",
    "controller": "operator_insider",
    "content_kind": "policy_document",
    "records": ["POL-MOD", "POL-OTHER"],
}
RUNTIME: dict[str, Any] = {
    "delivery": ["direct_user_message", "sequential_user_turns", "planted_item"],
    "setup_permissions": [],
    "observation": {"tool_calls": {"availability": "captured_or_unavailable"}},
    "limits": {"max_turns": 8, "max_planned_turns": 4},
    "seed_slots": [LISTING_SLOT, POLICY_SLOT],
}
INVENTORY: dict[str, Any] = {
    "facts": [],
    "operations": [{"name": "get_listing"}, {"name": "retrieve_policy"}],
}


def shape_of(name: str) -> dict[str, Any]:
    return deepcopy(load_input(_KIT / "valid" / name).attack_shape)


def _run(tmp_path: Path, name: str, inventory: dict[str, Any], runtime: dict[str, Any]):
    transport = ScriptedAuthoringTransport([])
    orchestrator = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    )
    return orchestrator.run(load_input(_KIT / "valid" / name), inventory, runtime), transport


def _assert_refused(result: Any, transport: Any, code: str) -> dict[str, Any]:
    assert transport.requests == []
    assert result.status == "failed"
    assert result.package_path is None
    assert [finding.code for finding in result.findings] == [code]
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["attempts"] == []
    assert evidence["terminal"] == {"stage": "plan", "attempt_index": None, "reason": code}
    [finding] = evidence["findings"]
    assert finding["path"] == "attack_shape"
    return finding


def with_slots(*slots: dict[str, Any]) -> dict[str, Any]:
    return {**RUNTIME, "seed_slots": list(slots)}


# --- shape_carrier_not_exposed --------------------------------------------------


def test_a_carrier_the_inventory_does_not_expose_is_refused(tmp_path: Path) -> None:
    inventory = {"facts": [], "operations": [{"name": "retrieve_policy"}]}

    result, transport = _run(tmp_path, LISTING, inventory, RUNTIME)

    finding = _assert_refused(result, transport, "shape_carrier_not_exposed")
    assert finding["details"] == {
        "carrier_operation": "get_listing",
        "inventory_operations": ["retrieve_policy"],
    }


def test_an_inventory_with_no_operations_exposes_no_carrier(tmp_path: Path) -> None:
    result, transport = _run(tmp_path, POLICY, {"facts": [], "operations": []}, RUNTIME)

    finding = _assert_refused(result, transport, "shape_carrier_not_exposed")
    assert finding["details"]["inventory_operations"] == []


# --- shape_seed_slot_unavailable ------------------------------------------------


def test_a_runtime_that_lists_no_slots_is_refused_as_slot_unavailable(tmp_path: Path) -> None:
    runtime = {key: value for key, value in RUNTIME.items() if key != "seed_slots"}

    result, transport = _run(tmp_path, LISTING, INVENTORY, runtime)

    finding = _assert_refused(result, transport, "shape_seed_slot_unavailable")
    assert finding["details"] == {
        "carrier_operation": "get_listing",
        "content_kind": "listing_content",
        "record_ref": "LST-104",
        "reason": "no_slot",
        "slots_for_carrier": [],
    }


def test_a_slot_for_the_carrier_with_another_content_kind_does_not_match(tmp_path: Path) -> None:
    other_kind = {**LISTING_SLOT, "content_kind": "review_content"}

    result, transport = _run(tmp_path, LISTING, INVENTORY, with_slots(other_kind, POLICY_SLOT))

    finding = _assert_refused(result, transport, "shape_seed_slot_unavailable")
    assert finding["details"]["reason"] == "no_slot"
    assert finding["details"]["slots_for_carrier"] == ["airbnb.listing.title"]


def test_a_record_the_slot_does_not_list_is_refused(tmp_path: Path) -> None:
    unlisted = {**LISTING_SLOT, "records": ["LST-101"]}

    result, transport = _run(tmp_path, LISTING, INVENTORY, with_slots(unlisted, POLICY_SLOT))

    finding = _assert_refused(result, transport, "shape_seed_slot_unavailable")
    assert finding["details"] == {
        "carrier_operation": "get_listing",
        "content_kind": "listing_content",
        "record_ref": "LST-104",
        "reason": "record_not_listed",
        "slot": "airbnb.listing.title",
        "listed_records": ["LST-101"],
    }


def test_a_malformed_slot_list_is_read_as_no_slots(tmp_path: Path) -> None:
    runtime = {**RUNTIME, "seed_slots": ["airbnb.listing.title", {"id": 3}, None]}

    result, transport = _run(tmp_path, LISTING, INVENTORY, runtime)

    _assert_refused(result, transport, "shape_seed_slot_unavailable")


# --- precedence and the shapes that pass ----------------------------------------


def test_a_runtime_without_the_delivery_reports_only_the_channel(tmp_path: Path) -> None:
    runtime = {**RUNTIME, "delivery": ["direct_user_message", "sequential_user_turns"]}

    result, transport = _run(tmp_path, LISTING, {"facts": [], "operations": []}, runtime)

    _assert_refused(result, transport, "shape_channel_unsupported")


def test_both_refusals_are_reported_when_both_apply(tmp_path: Path) -> None:
    runtime = with_slots(POLICY_SLOT)

    result, transport = _run(tmp_path, LISTING, {"facts": [], "operations": []}, runtime)

    assert transport.requests == []
    assert [finding.code for finding in result.findings] == [
        "shape_carrier_not_exposed",
        "shape_seed_slot_unavailable",
    ]


@pytest.mark.parametrize("name", [LISTING, POLICY])
def test_an_indirect_shape_the_target_can_carry_is_not_refused(name: str) -> None:
    view = load_input(_KIT / "valid" / name)

    assert shape_refusal_findings(view, RUNTIME, INVENTORY) == []


# --- slot choice ----------------------------------------------------------------


def test_the_shapes_record_is_used_when_the_slot_lists_it() -> None:
    target = choose_planted_target(shape_of(LISTING), RUNTIME)

    assert target == PlantedTarget(
        slot="airbnb.listing.title",
        carrier_operation="get_listing",
        field="title",
        content_kind="listing_content",
        controller="counterparty",
        record_ref="LST-104",
        mode="replace",
    )


def test_the_first_listed_record_is_used_when_the_shape_names_none() -> None:
    target = choose_planted_target(shape_of(POLICY), RUNTIME)

    assert target.record_ref == "POL-MOD"
    assert target.slot == "airbnb.policy_doc.body"


def test_a_policy_document_is_appended_so_the_original_text_stays_retrievable() -> None:
    assert choose_planted_target(shape_of(POLICY), RUNTIME).mode == "append"
    assert choose_planted_target(shape_of(LISTING), RUNTIME).mode == "replace"


def test_the_first_matching_slot_wins_and_the_choice_is_stable() -> None:
    twin = {**LISTING_SLOT, "id": "airbnb.listing.twin", "records": ["LST-104"]}
    runtime = with_slots(twin, LISTING_SLOT)

    chosen = [choose_planted_target(shape_of(LISTING), runtime) for _ in range(3)]

    assert {target.slot for target in chosen} == {"airbnb.listing.twin"}


def test_a_shape_with_no_carrier_slot_has_no_target() -> None:
    assert choose_planted_target(shape_of(LISTING), with_slots(POLICY_SLOT)) is None
    assert choose_planted_target(shape_of("adversarial-direct-single.json"), RUNTIME) is None
