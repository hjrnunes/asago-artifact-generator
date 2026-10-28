"""Offline tests for the single, pure normalization step that runs before the checks."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import checks
from asago_artifact_generator.authoring.checks import (
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
    normalize_artifact_v2,
    normalize_plan_v2,
)
from asago_artifact_generator.authoring.core import _canonical_json, _sha256
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator

from .policy_support import NO_REVIEW
from .support import ScriptedAuthoringTransport, world_builders

_framed, _inventory, _metadata, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "metadata", "plan", "runtime_contract", "view"
)

_ORDERS = [
    {
        "ref": "state:orders",
        "value": {"ORD-1": {"customer_id": "CUST-1"}},
        "schema": {
            "type": "object",
            "properties": {
                "ORD-1": {"type": "object", "properties": {"customer_id": {"type": "string"}}}
            },
        },
    },
    {
        "ref": "state:orders:records",
        "value": {"ORD-1": {"record_key": "ORD-1"}},
        "schema": {
            "type": "object",
            "properties": {
                "ORD-1": {"type": "object", "properties": {"record_key": {"type": "string"}}}
            },
        },
    },
]


def _orders_inventory() -> dict:
    inventory = _inventory()
    inventory["facts"] = [*inventory["facts"], *copy.deepcopy(_ORDERS)]
    return inventory


def _plan_needing_every_rewrite() -> dict:
    """Return a plan with a keyed-map shorthand, an exact duplicate and a missing consumer."""

    plan = copy.deepcopy(_plan())
    owned = plan["runtime_bindings"][0]
    owned["consumers"] = ["setup.arguments.owned_order"]
    customer = {
        "name": "customer_id",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:state:orders:ORD-1:customer_id",
        "selector": "value",
        "consumers": ["stimulus.user_text"],
        "on_missing": "stop",
    }
    plan["runtime_bindings"] = [owned, customer, copy.deepcopy(owned)]
    plan["stimulus_approach"]["request"] = (
        "Ask to refund customer {{customer_id}} more than the order balance."
    )
    return plan


def _metadata_needing_slots() -> dict:
    metadata = copy.deepcopy(_metadata())
    metadata["stimulus"]["user_text"] = "Refund order-1 for 11 for customer {{customer_id}}."
    metadata["stimulus"]["slots"] = []
    return metadata


def _kinds(transformations: list[dict]) -> list[str]:
    return [item["transformation"] for item in transformations]


def test_plan_normalization_is_pure() -> None:
    plan = _plan_needing_every_rewrite()
    written = copy.deepcopy(plan)

    first = normalize_plan_v2(plan, _orders_inventory())
    second = normalize_plan_v2(plan, _orders_inventory())

    assert plan == written
    assert first == second
    normalized, transformations = first
    assert normalized is not plan
    assert normalized["runtime_bindings"] is not plan["runtime_bindings"]
    assert _kinds(transformations) == [
        "binding_canonicalized",
        "binding_duplicate_dropped",
        "binding_consumer_added",
    ]
    assert [(item["source_ref"], item["selector"]) for item in normalized["runtime_bindings"]] == [
        ("facts:order:owned", "value"),
        ("facts:state:orders", "value.ORD-1.customer_id"),
    ]
    assert normalized["runtime_bindings"][0]["consumers"] == [
        "setup.arguments.owned_order",
        "prerequisites.owned_order",
    ]


def test_plan_normalization_keeps_a_plan_that_is_not_an_object() -> None:
    assert normalize_plan_v2(["plan"], _orders_inventory()) == (["plan"], [])


def test_artifact_normalization_is_pure() -> None:
    plan, _ = normalize_plan_v2(_plan_needing_every_rewrite(), _orders_inventory())
    metadata = _metadata_needing_slots()
    written = copy.deepcopy((metadata, plan))

    first = normalize_artifact_v2(metadata, plan)
    second = normalize_artifact_v2(metadata, plan)

    assert (metadata, plan) == written
    assert first == second
    normalized, transformations = first
    assert normalized is not metadata
    assert normalized["stimulus"]["slots"] == ["customer_id"]
    assert _kinds(transformations) == ["stimulus_slots_derived"]


def test_artifact_normalization_keeps_metadata_with_shape_findings_as_written() -> None:
    plan, _ = normalize_plan_v2(_plan_needing_every_rewrite(), _orders_inventory())
    metadata = {**_metadata_needing_slots(), "junk": "x"}

    normalized, transformations = normalize_artifact_v2(metadata, plan)

    assert normalized == metadata
    assert normalized["stimulus"]["slots"] == []
    assert transformations == []


def test_the_checks_read_the_normalized_candidates_without_normalizing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _orders_inventory()
    plan, _ = normalize_plan_v2(_plan_needing_every_rewrite(), inventory)
    metadata, _ = normalize_artifact_v2(_metadata_needing_slots(), plan)
    raw_plan = _plan_needing_every_rewrite()
    checked = copy.deepcopy((plan, metadata, raw_plan))

    def normalizing(*_: object, **__: object) -> None:
        raise AssertionError("a check normalized its input")

    for name in (
        "normalize_binding_declarations",
        "_normalize_prerequisite_binding_consumers",
        "_normalize_stimulus_slots",
    ):
        monkeypatch.setattr(checks, name, normalizing)

    assert collect_plan_findings_v2(plan, inventory, _runtime_contract()) == []
    assert collect_artifact_findings_v2(metadata, plan, inventory, _runtime_contract()) == []
    # A plan that skipped normalization is reported, not repaired.
    assert collect_plan_findings_v2(raw_plan, inventory, _runtime_contract())
    assert (plan, metadata, raw_plan) == checked


def test_the_orchestrator_records_the_rewrites_of_its_one_normalization(tmp_path: Path) -> None:
    inventory = _orders_inventory()
    plan = _plan_needing_every_rewrite()
    metadata = _metadata_needing_slots()
    normalized_plan, plan_rewrites = normalize_plan_v2(plan, inventory)
    normalized_metadata, artifact_rewrites = normalize_artifact_v2(metadata, normalized_plan)
    transport = ScriptedAuthoringTransport([json.dumps(plan), _framed(metadata)])

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="normalize",
        policy=NO_REVIEW,
    ).run(_view(), inventory, _runtime_contract())

    assert result.status == "accepted"
    plan_record, artifact_record = result.ledger
    assert plan_record["transformations"] == plan_rewrites
    assert artifact_record["transformations"] == artifact_rewrites
    assert result.transformations == plan_rewrites + artifact_rewrites
    assert plan_record["decoded_output"] == normalized_plan == result.plan
    assert artifact_record["decoded_output"] == normalized_metadata
    # The digest pins the response as decoded, before normalization.
    assert plan_record["candidate_sha256"] == _sha256(_canonical_json(plan).encode("utf-8"))
