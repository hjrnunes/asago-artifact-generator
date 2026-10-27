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
# A result that does not name its record: only a call's argument can.
BODY_ONLY = {"type": "object", "properties": {"body": TEXT}}
# A single-record carrier: it returns the one note its required note_id argument names.
LOOKUP = {
    "type": "object",
    "properties": {"note_id": {"type": "string"}},
    "required": ["note_id"],
}
ONE_NOTE = {**SLOT, "carrier_operation": "read_note", "field": "body", "records": ["NOTE-2"]}


def _located(
    input_schema: Any, slot: dict[str, Any] = ONE_NOTE, output_schema: Any = NOTE
) -> tuple[Placement, DeclaredTools]:
    tool: dict[str, Any] = {"name": "read_note", "outputSchema": output_schema}
    if input_schema is not None:
        tool["inputSchema"] = input_schema
    declared = DeclaredTools({"read_note": tool}, frozenset())
    return locate(Placement(0, slot, "NOTE-2", "text"), declared), declared


def _reason(
    input_schema: Any, slot: dict[str, Any] = ONE_NOTE, output_schema: Any = NOTE
) -> str | None:
    return gap_reason(*_located(input_schema, slot, output_schema))


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
    placement, _ = _located(LOOKUP)
    target = placement.instruction()["target"]
    assert not placement.by_result
    assert target["call_arguments"] == {"note_id": "NOTE-2"}
    assert "result_record" not in target


ONE = "seed item 0: 'read_note'"
NO_RESULT_KEY = (
    "; 'read_note''s result object declares no property 'note_id' (slot "
    "piggybank.note.body's record_key) either, by which MiDojo would match the result"
)
# Input schemas whose record_key is not a required text argument, and why.
NOT_A_REQUIRED_TEXT_ARGUMENT = [
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
]
NOT_A_REQUIRED_TEXT_ARGUMENT_IDS = [
    "no-arguments",
    "no-input-schema",
    "not-text",
    "untyped",
    "not-required",
]


@pytest.mark.parametrize(
    ("input_schema", "reason"),
    NOT_A_REQUIRED_TEXT_ARGUMENT,
    ids=NOT_A_REQUIRED_TEXT_ARGUMENT_IDS,
)
def test_a_field_outside_a_list_whose_record_key_neither_the_call_nor_the_result_names_is_a_gap(
    input_schema: Any, reason: str
) -> None:
    placement, declared = _located(input_schema, output_schema=BODY_ONLY)

    assert not placement.by_result
    assert gap_reason(placement, declared) == reason + NO_RESULT_KEY


@pytest.mark.parametrize(
    "input_schema",
    [schema for schema, _ in NOT_A_REQUIRED_TEXT_ARGUMENT],
    ids=NOT_A_REQUIRED_TEXT_ARGUMENT_IDS,
)
def test_a_field_outside_a_list_whose_result_names_its_record_is_matched_by_the_result(
    input_schema: Any,
) -> None:
    placement, declared = _located(input_schema)

    assert placement.by_result
    assert gap_reason(placement, declared) is None
    target = placement.instruction()["target"]
    assert target["result_record"] == {"field": "note_id", "equals": "NOTE-2"}
    assert target["structured_content_path"] == ["body"]
    assert "call_arguments" not in target


@pytest.mark.parametrize(
    ("declared_key", "named"),
    [
        ({"type": "integer"}, "'integer'"),
        ({}, "'untyped'"),
        ({"type": ["string", "null"]}, "['string', 'null']"),
    ],
    ids=["integer", "untyped", "nullable"],
)
def test_a_result_property_not_declared_text_does_not_name_the_record(
    declared_key: dict[str, Any], named: str
) -> None:
    output = {"type": "object", "properties": {"note_id": declared_key, "body": TEXT}}
    placement, declared = _located({"type": "object", "properties": {}}, output_schema=output)

    assert not placement.by_result
    assert gap_reason(placement, declared) == (
        f"{ONE} declares no argument 'note_id' (slot piggybank.note.body's record_key), "
        "so no call names the record whose 'body' it returns; 'read_note''s result declares "
        f"'note_id' (slot piggybank.note.body's record_key) as {named}, not text"
    )


def test_a_record_key_below_the_result_object_does_not_name_the_record() -> None:
    """MiDojo matches the result object's own property, not one of a nested object."""

    output = {
        "type": "object",
        "properties": {"note": {"type": "object", "properties": {"note_id": TEXT, "body": TEXT}}},
    }
    slot = {**ONE_NOTE, "field": "note.body"}
    placement, declared = _located({"type": "object", "properties": {}}, slot, output)

    assert not placement.by_result
    assert gap_reason(placement, declared) == (
        f"{ONE} declares no argument 'note_id' (slot piggybank.note.body's record_key), "
        "so no call names the record whose 'note.body' it returns" + NO_RESULT_KEY
    )


def test_a_record_key_beside_a_nested_field_matches_the_result_by_the_plain_path() -> None:
    output = {
        "type": "object",
        "properties": {"note_id": TEXT, "note": {"type": "object", "properties": {"body": TEXT}}},
    }
    slot = {**ONE_NOTE, "field": "note.body"}
    placement, declared = _located({"type": "object", "properties": {}}, slot, output)

    assert gap_reason(placement, declared) is None
    target = placement.instruction()["target"]
    assert target["structured_content_path"] == ["note", "body"]
    assert target["result_record"] == {"field": "note_id", "equals": "NOTE-2"}


def test_a_list_field_is_never_matched_by_the_result() -> None:
    tool = {
        "name": "list_notes",
        "outputSchema": {
            "type": "object",
            "properties": {
                "note_id": TEXT,
                "notes": {"type": "array", "items": NOTE},
            },
        },
    }
    declared = DeclaredTools({"list_notes": tool}, frozenset())

    placement = locate(Placement(0, SLOT, "NOTE-1", "text"), declared)

    assert not placement.by_result
    assert placement.result_record is None
    assert "result_record" not in placement.instruction()["target"]


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
