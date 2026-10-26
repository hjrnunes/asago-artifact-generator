"""Placement: a seed item and its runtime contract slot become a tool-output instruction."""

from __future__ import annotations

import json
from typing import Any

import pytest
from episode_cases import SLOTS, runtime_contract

from asago_artifact_generator.package_io import build_package
from asago_bundle_midojo.placement import (
    Placement,
    PlacementFault,
    Segment,
    field_segments,
    single_record,
)
from asago_bundle_midojo.placement import runtime_contract as contract_of

SLOT = SLOTS[0]


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


def test_a_field_outside_a_list_is_the_result_s_own_on_every_call() -> None:
    slot = {**SLOT, "field": "summary.text", "mode": "replace"}

    instruction = Placement(3, slot, "NOTE-1", "text").instruction()

    assert instruction["target"]["structured_content_path"] == ["summary", "text"]
    assert instruction["occurrence"]["kind"] == "every"
    assert (instruction["mode"], instruction["probe_key"]) == ("replace", "planted_item:seed_3")


@pytest.mark.parametrize(
    ("input_schema", "records", "single"),
    [
        ({"type": "object", "properties": {}}, ["NOTE-1"], True),
        ({"type": "object"}, ["NOTE-1"], True),
        (None, ["NOTE-1"], True),
        ({"type": "object", "properties": {"note_id": {"type": "string"}}}, ["NOTE-1"], False),
        ({"type": "object", "properties": {}}, ["NOTE-1", "NOTE-2"], False),
    ],
)
def test_a_carrier_returns_only_the_slot_s_record_when_it_takes_no_arguments_and_one_record(
    input_schema: dict[str, Any] | None, records: list[str], single: bool
) -> None:
    tool = {"name": "read_note"} if input_schema is None else {"inputSchema": input_schema}

    assert single_record(tool, {**SLOT, "records": records}) is single


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
