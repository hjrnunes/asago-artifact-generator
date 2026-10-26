"""Placement: a seed item and its runtime contract slot become a tool-output instruction."""

from __future__ import annotations

import json
from typing import Any

import pytest
from episode_cases import SLOTS, runtime_contract

from asago_artifact_generator.package_io import build_package
from asago_bundle_midojo.declared import DeclaredTools
from asago_bundle_midojo.placement import (
    Placement,
    PlacementFault,
    Segment,
    field_segments,
    gap_reason,
    locate,
)
from asago_bundle_midojo.placement import runtime_contract as contract_of

SLOT = SLOTS[0]
TEXT = {"type": "string"}


def test_a_field_splits_into_keys_marking_the_list_of_records() -> None:
    assert field_segments("notes[].body") == (Segment("notes", True), Segment("body", False))
    assert field_segments("item") == (Segment("item", False),)


@pytest.mark.parametrize("field", ["", "notes..body", "notes[0].body", "notes[]x.body"])
def test_a_field_that_is_not_keys_joined_by_dots_is_refused(field: str) -> None:
    with pytest.raises(PlacementFault, match="is not keys joined by"):
        field_segments(field)


def test_a_list_field_is_reached_through_the_list_by_a_record_selector_on_every_call() -> None:
    instruction = Placement(0, SLOT, "NOTE-1", "text", record_key="note_id").instruction()

    assert instruction["target"]["tool"] == "list_notes"
    assert instruction["target"]["structured_content_path"] == [
        "notes",
        {"field": "note_id", "equals": "NOTE-1"},
        "body",
    ]
    assert instruction["occurrence"]["kind"] == "every"
    assert (instruction["mode"], instruction["size_limit"]) == ("append", 280)


def test_a_list_field_without_its_record_key_has_no_path() -> None:
    with pytest.raises(PlacementFault, match="no record key to select by"):
        Placement(0, SLOT, "NOTE-1", "text").instruction()


NOTE = {"type": "object", "properties": {"note_id": {"type": "string"}, "body": TEXT}}


@pytest.mark.parametrize(
    "input_schema",
    [
        {"type": "object", "properties": {}},
        {"type": "object"},
        None,
        {"type": "object", "properties": {"note_id": {"type": "string"}}},
    ],
    ids=["no-properties", "no-properties-key", "no-input-schema", "an-argument"],
)
@pytest.mark.parametrize("records", [["NOTE-1"], ["NOTE-1", "NOTE-2"]], ids=["one", "two"])
def test_a_field_outside_a_list_is_refused_whatever_the_carrier_takes(
    input_schema: dict[str, Any] | None, records: list[str]
) -> None:
    tool: dict[str, Any] = {"name": "pinned_note", "outputSchema": NOTE}
    if input_schema is not None:
        tool["inputSchema"] = input_schema
    slot = {**SLOT, "carrier_operation": "pinned_note", "field": "body", "records": records}
    declared = DeclaredTools({"pinned_note": tool}, frozenset())

    reason = gap_reason(locate(Placement(0, slot, "NOTE-1", "text"), declared), declared)

    assert reason == (
        "seed item 0: MiDojo selects a record only as an item of a list in the result, and "
        "'body' is outside a list: no declaration says a result of 'pinned_note' is record "
        "'NOTE-1'"
    )


def test_the_record_key_is_the_contract_s_else_the_target_config_s() -> None:
    bare = {key: value for key, value in SLOT.items() if key != "record_key"}
    placement = Placement(0, bare, "NOTE-1", "text")

    from_target = locate(placement, DeclaredTools({}, frozenset(), {SLOT["id"]: "note_id"}))
    from_contract = locate(Placement(0, SLOT, "NOTE-1", "text"), DeclaredTools({}, frozenset()))
    undeclared = locate(placement, DeclaredTools({}, frozenset()))

    assert (from_target.record_key, from_contract.record_key) == ("note_id", "note_id")
    assert undeclared.record_key is None


def test_a_record_key_the_contract_and_the_target_state_differently_is_refused() -> None:
    declared = DeclaredTools({}, frozenset(), {SLOT["id"]: "body"})

    with pytest.raises(PlacementFault, match="record_key is 'note_id' in the runtime contract"):
        locate(Placement(0, SLOT, "NOTE-1", "text"), declared)


def _package(members: dict[str, object], manifest: dict[str, object] | None = None) -> Any:
    encoded = {name: json.dumps(value).encode("utf-8") for name, value in members.items()}
    return build_package(
        package_id="SCN-001-SCN-001",
        scenario_id="SCN-001",
        input_kind="scenario-handoff-v5",
        source_digests={"handoff": "0" * 64},
        members={"stimulus.json": b"{}", **encoded},
        runtime_capabilities=manifest,
    )


def test_the_runtime_contract_is_read_from_inputs_then_the_plan_then_the_manifest() -> None:
    contracts = {
        place: runtime_contract([{**SLOT, "id": place}])
        for place in ("inputs", "plan", "manifest")
    }
    plan = {"observation_claim": {"claim_level": "reply"}}

    found = [
        contract_of(package)["seed_slots"][0]["id"]
        for package in (
            _package(
                {
                    "inputs.json": {"runtime_contract": contracts["inputs"]},
                    "plan.json": {**plan, "runtime_contract": contracts["plan"]},
                },
                contracts["manifest"],
            ),
            _package(
                {"plan.json": {**plan, "runtime_contract": contracts["plan"]}},
                contracts["manifest"],
            ),
            _package({"plan.json": plan}, contracts["manifest"]),
        )
    ]

    assert found == ["inputs", "plan", "manifest"]
