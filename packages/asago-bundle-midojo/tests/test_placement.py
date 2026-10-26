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


def test_a_list_field_is_reached_through_the_list_by_the_record_on_every_call() -> None:
    instruction = Placement(0, SLOT, "NOTE-1", "text").instruction()

    assert instruction["target"]["tool"] == "list_notes"
    assert instruction["target"]["structured_content_path"] == ["notes", "NOTE-1", "body"]
    assert instruction["occurrence"]["kind"] == "every"
    assert (instruction["mode"], instruction["size_limit"]) == ("append", 280)


def test_a_field_outside_a_list_is_the_result_s_own_on_the_first_call() -> None:
    slot = {**SLOT, "field": "summary.text", "mode": "replace"}

    instruction = Placement(3, slot, "NOTE-1", "text").instruction()

    assert instruction["target"]["structured_content_path"] == ["summary", "text"]
    assert instruction["occurrence"]["kind"] == "first"
    assert (instruction["mode"], instruction["probe_key"]) == ("replace", "planted_item:seed_3")


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
