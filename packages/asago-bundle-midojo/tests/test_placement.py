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
# A single-record carrier: it returns the one note its required note_id argument names.
LOOKUP = {
    "type": "object",
    "properties": {"note_id": {"type": "string"}},
    "required": ["note_id"],
}
ONE_NOTE = {**SLOT, "carrier_operation": "read_note", "field": "body", "records": ["NOTE-2"]}


def _reason(input_schema: Any, slot: dict[str, Any] = ONE_NOTE) -> str | None:
    tool: dict[str, Any] = {"name": "read_note", "outputSchema": NOTE}
    if input_schema is not None:
        tool["inputSchema"] = input_schema
    declared = DeclaredTools({"read_note": tool}, frozenset())
    return gap_reason(locate(Placement(0, slot, "NOTE-2", "text"), declared), declared)


def test_a_list_field_matches_no_call_argument() -> None:
    instruction = Placement(0, SLOT, "NOTE-1", "text", record_key="note_id").instruction()

    assert "call_arguments" not in instruction["target"]


def test_a_field_outside_a_list_is_the_plain_path_on_the_calls_naming_the_record() -> None:
    instruction = Placement(0, ONE_NOTE, "NOTE-2", "text", record_key="note_id").instruction()

    assert instruction["target"]["tool"] == "read_note"
    assert instruction["target"]["structured_content_path"] == ["body"]
    assert instruction["target"]["call_arguments"] == {"note_id": "NOTE-2"}
    assert instruction["occurrence"]["kind"] == "every"


def test_a_field_outside_a_list_without_its_record_key_has_no_call_arguments() -> None:
    with pytest.raises(PlacementFault, match="no record key to match the call by"):
        Placement(0, ONE_NOTE, "NOTE-2", "text").instruction()


def test_a_single_record_carrier_whose_required_text_argument_is_the_record_key_is_placed() -> (
    None
):
    assert _reason(LOOKUP) is None
    assert _reason({**LOOKUP, "required": ["note_id", "other"]}) is None


ONE = "seed item 0: 'read_note'"


@pytest.mark.parametrize(
    ("input_schema", "reason"),
    [
        (
            {"type": "object", "properties": {}},
            f"{ONE} declares no argument 'note_id' (slot piggybank.note.body's record_key), "
            "so no call names the record whose 'body' it returns",
        ),
        (
            None,
            f"{ONE} declares no argument 'note_id' (slot piggybank.note.body's record_key), "
            "so no call names the record whose 'body' it returns",
        ),
        (
            {**LOOKUP, "properties": {"note_id": {"type": "integer"}}},
            f"{ONE} declares its argument 'note_id' (slot piggybank.note.body's record_key) "
            "as 'integer', not text",
        ),
        (
            {**LOOKUP, "properties": {"note_id": {}}},
            f"{ONE} declares its argument 'note_id' (slot piggybank.note.body's record_key) "
            "as 'untyped', not text",
        ),
        (
            {"type": "object", "properties": {"note_id": {"type": "string"}}},
            f"{ONE} does not require its argument 'note_id' (slot piggybank.note.body's "
            "record_key), so a call need not name the record whose 'body' it returns",
        ),
    ],
    ids=["no-arguments", "no-input-schema", "not-text", "untyped", "not-required"],
)
def test_a_field_outside_a_list_whose_record_key_is_no_required_text_argument_is_a_gap(
    input_schema: Any, reason: str
) -> None:
    assert _reason(input_schema) == reason


def test_a_field_outside_a_list_without_a_record_key_is_a_gap() -> None:
    bare = {key: value for key, value in ONE_NOTE.items() if key != "record_key"}

    assert _reason(LOOKUP, bare) == (
        "seed item 0: 'body' is outside a list, and slot piggybank.note.body declares no "
        "record_key, the argument of 'read_note' that names the one record its result "
        "holds, which MiDojo matches the call by"
    )


def test_a_field_outside_a_list_the_result_does_not_declare_as_text_is_a_gap() -> None:
    assert _reason(LOOKUP, {**ONE_NOTE, "field": "title"}) == (
        "seed item 0: 'read_note''s result declares no text field 'title'"
    )


@pytest.mark.parametrize(
    "parent",
    [
        {"type": "array", "properties": {"body": TEXT}},
        {"type": "string", "properties": {"body": TEXT}},
        {"type": ["object", "null"], "properties": {"body": TEXT}},
        {"properties": {"body": TEXT}},
    ],
    ids=["array", "scalar", "nullable-object", "untyped"],
)
def test_a_field_outside_a_list_below_a_key_not_exactly_an_object_is_a_gap(
    parent: dict[str, Any],
) -> None:
    tool = {
        "name": "read_note",
        "inputSchema": LOOKUP,
        "outputSchema": {"type": "object", "properties": {"note": parent}},
    }
    declared = DeclaredTools({"read_note": tool}, frozenset())
    slot = {**ONE_NOTE, "field": "note.body"}

    reason = gap_reason(locate(Placement(0, slot, "NOTE-2", "text"), declared), declared)

    assert reason == (
        "seed item 0: 'read_note''s result does not declare every key above 'note.body' as "
        'an object ("type": "object"), so its path is not sure to reach the field'
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
