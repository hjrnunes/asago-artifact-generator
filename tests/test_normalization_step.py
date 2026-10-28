"""Offline tests for the single, pure normalization step that runs before the checks."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator import bindings
from asago_artifact_generator.authoring import checks
from asago_artifact_generator.authoring.checks import (
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
    normalize_artifact_v2,
    normalize_plan_v2,
)
from asago_artifact_generator.authoring.core import _canonical_json, _sha256
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator

from .policy_support import NO_REVIEW, ONE_PLAN_CORRECTION
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
    normalized, transformations, _ = first
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
    assert normalize_plan_v2(["plan"], _orders_inventory()) == (["plan"], [], None)


def test_artifact_normalization_is_pure() -> None:
    plan, _, _ = normalize_plan_v2(_plan_needing_every_rewrite(), _orders_inventory())
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
    plan, _, _ = normalize_plan_v2(_plan_needing_every_rewrite(), _orders_inventory())
    metadata = {**_metadata_needing_slots(), "junk": "x"}

    normalized, transformations = normalize_artifact_v2(metadata, plan)

    assert normalized == metadata
    assert normalized["stimulus"]["slots"] == []
    assert transformations == []


def _count_normalizing_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace every normalizing rewrite, wherever a module binds it, with a call counter."""

    calls: list[str] = []
    rewrites = {
        name: getattr(module, name)
        for module, name in (
            (bindings, "normalize_binding_declarations"),
            (checks, "_normalize_prerequisite_binding_consumers"),
            (checks, "_normalize_stimulus_slots"),
            (checks, "_canonicalize_binding_source"),
        )
    }
    for module in list(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith("asago_artifact_generator"):
            continue
        for name, rewrite in rewrites.items():
            if getattr(module, name, None) is rewrite:

                def counted(*_: object, _name: str = name, **__: object) -> list[Any]:
                    calls.append(_name)
                    return []

                monkeypatch.setattr(module, name, counted)
    return calls


def test_the_checks_read_the_normalized_candidates_without_normalizing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = _orders_inventory()
    plan, _, declarations = normalize_plan_v2(_plan_needing_every_rewrite(), inventory)
    metadata, _ = normalize_artifact_v2(_metadata_needing_slots(), plan)
    raw_plan = _plan_needing_every_rewrite()
    checked = copy.deepcopy((plan, metadata, raw_plan))
    calls = _count_normalizing_calls(monkeypatch)

    assert (
        collect_plan_findings_v2(plan, inventory, _runtime_contract(), declarations=declarations)
        == []
    )
    assert collect_artifact_findings_v2(metadata, plan, inventory, _runtime_contract()) == []
    # A plan that skipped normalization is reported, not repaired.
    assert collect_plan_findings_v2(raw_plan, inventory, _runtime_contract())
    assert calls == []
    assert (plan, metadata, raw_plan) == checked


def test_the_counter_sees_the_helpers_that_normalize_for_other_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declarations = _plan_needing_every_rewrite()["runtime_bindings"]
    calls = _count_normalizing_calls(monkeypatch)

    bindings.supplied_binding_values(declarations, _orders_inventory())
    bindings.validate_bindings(
        declarations, inventory=_orders_inventory(), runtime_contract=_runtime_contract()
    )

    assert calls == ["normalize_binding_declarations", "normalize_binding_declarations"]


def _closure_duplicate_plan() -> dict:
    """Two owned_order declarations that differ only in the consumer the closure adds."""

    plan = copy.deepcopy(_plan())
    owned = plan["runtime_bindings"][0]
    plan["runtime_bindings"] = [
        {**copy.deepcopy(owned), "consumers": ["stimulus.user_text", "prerequisites.owned_order"]},
        {**copy.deepcopy(owned), "consumers": ["stimulus.user_text"]},
    ]
    return plan


def test_a_declaration_the_consumer_closure_made_a_duplicate_is_reported_once() -> None:
    inventory = _inventory()
    normalized = normalize_plan_v2(_closure_duplicate_plan(), inventory)

    assert normalized.plan["runtime_bindings"] == [
        {
            **_plan()["runtime_bindings"][0],
            "consumers": ["stimulus.user_text", "prerequisites.owned_order"],
        }
    ]
    assert _kinds(normalized.transformations) == ["binding_consumer_added"]
    assert len(normalized.declarations) == 2
    findings = collect_plan_findings_v2(
        normalized.plan, inventory, _runtime_contract(), declarations=normalized.declarations
    )
    assert [(finding.code, finding.path, finding.detail) for finding in findings] == [
        ("plan_binding_validation", "runtime_bindings[1]", "duplicate binding: owned_order"),
        (
            "consumer_mismatch",
            "runtime_bindings[0].consumers[0]",
            findings[1].detail,
        ),
    ]
    # Normalizing the normalized plan again changes and records nothing.
    again = normalize_plan_v2(normalized.plan, inventory)
    assert again.plan == normalized.plan
    assert again.transformations == []
    assert again.declarations == normalized.plan["runtime_bindings"]


def test_the_orchestrator_records_one_binding_and_one_mismatch_for_a_closure_duplicate(
    tmp_path: Path,
) -> None:
    plan = _closure_duplicate_plan()
    transport = ScriptedAuthoringTransport([json.dumps(plan), json.dumps(plan)])

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="closure-duplicate",
        policy=ONE_PLAN_CORRECTION,
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    evidence = json.loads(Path(result.failure_evidence_path).read_text())
    for attempt in evidence["attempts"]:
        assert len(attempt["decoded_output"]["runtime_bindings"]) == 1
        assert [(finding["code"], finding["path"]) for finding in attempt["findings"]] == [
            ("plan_binding_validation", "runtime_bindings[1]"),
            ("consumer_mismatch", "runtime_bindings[0].consumers[0]"),
        ]
        assert _kinds(attempt["transformations"]) == ["binding_consumer_added"]
    # The correction reports no mismatch for the dropped declaration.
    correction = json.dumps(transport.requests[1], default=str)
    assert "duplicate binding: owned_order" in correction
    assert "runtime_bindings[0].consumers[0]" in correction
    assert "runtime_bindings[1].consumers[0]" not in correction


def test_the_orchestrator_records_the_rewrites_of_its_one_normalization(tmp_path: Path) -> None:
    inventory = _orders_inventory()
    plan = _plan_needing_every_rewrite()
    metadata = _metadata_needing_slots()
    normalized_plan, plan_rewrites, _ = normalize_plan_v2(plan, inventory)
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
