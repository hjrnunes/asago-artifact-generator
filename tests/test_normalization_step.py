"""Offline tests for the single, pure normalization step that runs before the checks."""

from __future__ import annotations

import copy
import dataclasses
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
from asago_artifact_generator.binding_rules import BindingPolicy

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
    normalized, transformations, _, _ = first
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
    assert normalize_plan_v2(["plan"], _orders_inventory()) == (["plan"], [], None, {})


def test_artifact_normalization_is_pure() -> None:
    plan, _, _, _ = normalize_plan_v2(_plan_needing_every_rewrite(), _orders_inventory())
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
    plan, _, _, _ = normalize_plan_v2(_plan_needing_every_rewrite(), _orders_inventory())
    metadata = {**_metadata_needing_slots(), "junk": "x"}

    normalized, transformations = normalize_artifact_v2(metadata, plan)

    assert normalized == metadata
    assert normalized["stimulus"]["slots"] == []
    assert transformations == []


# Every entry point that normalizes or canonicalizes a plan, a declaration or a path.
_NORMALIZING = (
    (bindings, "canonical_binding_paths"),
    (bindings, "normalize_binding_declarations"),
    (bindings, "root_source_refs"),
    (bindings, "supplied_binding_values"),
    (bindings, "validate_bindings"),
    (checks, "normalize_plan_v2"),
    (checks, "normalize_artifact_v2"),
    (checks, "_canonicalize_binding_source"),
    (checks, "_normalize_prerequisite_binding_consumers"),
    (checks, "_normalize_stimulus_slots"),
)


def _count_normalizing_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count each call of a normalizing entry point, wherever a module or a policy holds it.

    A module attribute naming one is replaced, and so is a binding policy whose
    ``canonical_paths`` callback is one.  Each replacement records its name and
    then calls the original.
    """

    calls: list[str] = []
    names = {id(getattr(module, name)): name for module, name in _NORMALIZING}

    def counting(original: Any, name: str) -> Any:
        def counted(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return original(*args, **kwargs)

        return counted

    for module in list(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith("asago_artifact_generator"):
            continue
        for attribute, value in list(vars(module).items()):
            if isinstance(value, BindingPolicy) and id(value.canonical_paths) in names:
                callback = counting(value.canonical_paths, f"{attribute}.canonical_paths")
                monkeypatch.setattr(
                    module, attribute, dataclasses.replace(value, canonical_paths=callback)
                )
            elif callable(value) and id(value) in names:
                monkeypatch.setattr(module, attribute, counting(value, names[id(value)]))
    return calls


def _unresolved_shorthand_plan() -> dict:
    """A keyed-map shorthand whose declared selector the keyed record does not document."""

    plan = copy.deepcopy(_plan())
    plan["runtime_bindings"].append(
        {
            "name": "customer_id",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:state:orders:ORD-1:customer_id",
            "selector": "value.foo",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    )
    plan["stimulus_approach"]["request"] = "Refund customer {{customer_id}} more than the balance."
    return plan


@pytest.mark.parametrize(
    ("plan", "inventory"),
    [
        pytest.param(_plan_needing_every_rewrite, _orders_inventory, id="every-rewrite"),
        pytest.param(_unresolved_shorthand_plan, _orders_inventory, id="unresolved-shorthand"),
        pytest.param(lambda: _competing_plan("type"), _inventory, id="competing-declarations"),
    ],
)
def test_the_checks_read_the_normalized_candidates_without_normalizing_them(
    monkeypatch: pytest.MonkeyPatch, plan: Any, inventory: Any
) -> None:
    inventory = inventory()
    normalized = normalize_plan_v2(plan(), inventory)
    metadata, _ = normalize_artifact_v2(_metadata_needing_slots(), normalized.plan)
    raw_plan = plan()
    checked = copy.deepcopy((normalized, metadata, raw_plan))
    calls = _count_normalizing_calls(monkeypatch)

    collect_plan_findings_v2(
        normalized.plan,
        inventory,
        _runtime_contract(),
        declarations=normalized.declarations,
        source_refs=normalized.source_refs,
    )
    collect_artifact_findings_v2(metadata, normalized.plan, inventory, _runtime_contract())
    # A plan that skipped normalization is reported, not repaired.
    collect_plan_findings_v2(raw_plan, inventory, _runtime_contract())

    assert calls == []
    assert (normalized, metadata, raw_plan) == checked


def test_a_normalized_plan_with_every_rewrite_passes_the_checks() -> None:
    inventory = _orders_inventory()
    normalized = normalize_plan_v2(_plan_needing_every_rewrite(), inventory)
    metadata, _ = normalize_artifact_v2(_metadata_needing_slots(), normalized.plan)

    assert (
        collect_plan_findings_v2(
            normalized.plan,
            inventory,
            _runtime_contract(),
            declarations=normalized.declarations,
            source_refs=normalized.source_refs,
        )
        == []
    )
    assert (
        collect_artifact_findings_v2(metadata, normalized.plan, inventory, _runtime_contract())
        == []
    )
    assert collect_plan_findings_v2(_plan_needing_every_rewrite(), inventory, _runtime_contract())


def test_the_counter_sees_the_helpers_that_normalize_for_other_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declarations = _plan_needing_every_rewrite()["runtime_bindings"]
    calls = _count_normalizing_calls(monkeypatch)

    bindings.supplied_binding_values(declarations, _orders_inventory())
    bindings.validate_bindings(
        copy.deepcopy(declarations),
        inventory=_orders_inventory(),
        runtime_contract=_runtime_contract(),
    )
    checks._binding_source_schema(
        "supplied_input",
        "facts:state:orders:ORD-1:customer_id",
        _orders_inventory(),
        _runtime_contract(),
        "customer_id",
    )

    assert {
        "supplied_binding_values",
        "validate_bindings",
        "normalize_binding_declarations",
        "canonical_binding_paths",
        "AUTHORING_POLICY.canonical_paths",
        "_FINDINGS_POLICY.canonical_paths",
    } <= set(calls)


def test_an_unresolved_shorthand_is_looked_up_by_the_normalization_step() -> None:
    inventory = _orders_inventory()
    normalized = normalize_plan_v2(_unresolved_shorthand_plan(), inventory)

    assert normalized.plan["runtime_bindings"][1]["source_ref"] == (
        "facts:state:orders:ORD-1:customer_id"
    )
    assert normalized.source_refs == {
        ("supplied_input", "facts:state:orders:ORD-1:customer_id"): "facts:state:orders"
    }
    findings = collect_plan_findings_v2(
        normalized.plan,
        inventory,
        _runtime_contract(),
        declarations=normalized.declarations,
        source_refs=normalized.source_refs,
    )
    # The finding names the fact the shorthand reads, so it reports the selector.
    assert [(finding.code, finding.path, finding.detail) for finding in findings] == [
        (
            "plan_binding_validation",
            "runtime_bindings[1].selector",
            "undocumented selector for binding customer_id: value.foo",
        )
    ]


@pytest.mark.parametrize(
    ("source_ref", "selector"),
    [
        ("facts:state:orders:ORD-1:customer_id", "value"),
        ("facts:state:orders:ORD-1:customer_id", "value.foo"),
        ("facts:state:orders:ORD-1", "value.customer_id"),
        ("facts:state:orders:ORD-1", "value"),
        ("facts:state:orders", "value.ORD-1.customer_id"),
        ("facts:state:orders:records:ORD-1", "value.record_key"),
        ("facts:state:orders:records", "value.ORD-1.record_key"),
        ("facts:state:orders:ORD-2:customer_id", "value"),
        ("facts:order:owned", "value"),
    ],
)
def test_canonical_paths_are_their_own_canonical_paths(source_ref: str, selector: str) -> None:
    """The checks read normalized paths as written (``paths_as_declared``)."""

    inventory = _orders_inventory()
    once = bindings.canonical_binding_paths("supplied_input", source_ref, selector, inventory)

    assert bindings.canonical_binding_paths("supplied_input", *once, inventory) == once


def test_supplied_binding_values_still_normalizes_the_list_it_is_given() -> None:
    owned = _plan()["runtime_bindings"][0]
    declarations = [copy.deepcopy(owned), copy.deepcopy(owned)]
    written = (copy.deepcopy(owned), copy.deepcopy(owned))

    values = bindings.supplied_binding_values(declarations, _inventory())

    assert list(values) == ["owned_order"]
    assert declarations == [owned]
    # A tuple cannot be rewritten in place.
    assert bindings.supplied_binding_values(written, _inventory()) == values
    assert written == (owned, owned)


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


_FULL = ["stimulus.user_text", "prerequisites.owned_order"]
_COMPETING = {
    # The middle declaration's expected type and consumers.
    "type": ("string", ["stimulus.user_text"]),
    "setup": ("object", ["setup.arguments.owned_order"]),
}
_MISMATCH = (
    "binding 'owned_order' declares stimulus.user_text, but stimulus_approach.request "
    "contains neither its {{owned_order}} slot nor its supplied value; the artifact stage "
    "rejects that consumer and cannot change the plan. Either write {{owned_order}} where "
    "the request uses the value, or remove stimulus.user_text from its consumers. A slot "
    "must name the binding whose value it carries; session prerequisites and judge-only "
    "values do not belong in stimulus.user_text."
)
_COMPETING_FINDINGS = {
    "type": [
        ("plan_binding_validation", "runtime_bindings[1]", "duplicate binding: owned_order"),
        (
            "plan_binding_validation",
            "runtime_bindings[1].selector",
            "binding type mismatch for owned_order: expected string, source is object",
        ),
        ("plan_binding_validation", "runtime_bindings[2]", "duplicate binding: owned_order"),
        ("consumer_mismatch", "runtime_bindings[0].consumers[0]", _MISMATCH),
        ("consumer_mismatch", "runtime_bindings[1].consumers[0]", _MISMATCH),
    ],
    "setup": [
        ("plan_binding_validation", "runtime_bindings[1]", "duplicate binding: owned_order"),
        ("plan_binding_validation", "runtime_bindings[2]", "duplicate binding: owned_order"),
        ("consumer_mismatch", "runtime_bindings[0].consumers[0]", _MISMATCH),
    ],
}


def _competing_plan(case: str) -> dict:
    """Three owned_order declarations; the closure makes the last equal to the first."""

    plan = copy.deepcopy(_plan())
    owned = plan["runtime_bindings"][0]
    expected_type, consumers = _COMPETING[case]
    plan["runtime_bindings"] = [
        {**copy.deepcopy(owned), "consumers": list(_FULL)},
        {**copy.deepcopy(owned), "expected_type": expected_type, "consumers": list(consumers)},
        {**copy.deepcopy(owned), "consumers": ["stimulus.user_text"]},
    ]
    return plan


@pytest.mark.parametrize("case", sorted(_COMPETING))
def test_competing_declarations_keep_their_findings(case: str) -> None:
    inventory = _inventory()
    authored = _competing_plan(case)["runtime_bindings"]
    normalized = normalize_plan_v2(_competing_plan(case), inventory)

    assert normalized.plan["runtime_bindings"] == authored[:2]
    assert _kinds(normalized.transformations) == ["binding_consumer_added"]
    findings = collect_plan_findings_v2(
        normalized.plan,
        inventory,
        _runtime_contract(),
        declarations=normalized.declarations,
        source_refs=normalized.source_refs,
    )
    # The prerequisite checks read all three declarations, so the last one's
    # object type matches the prerequisite and no type mismatch is reported.
    assert [(finding.code, finding.path, finding.detail) for finding in findings] == (
        _COMPETING_FINDINGS[case]
    )


@pytest.mark.parametrize("case", sorted(_COMPETING))
def test_competing_declarations_keep_their_correction(case: str, tmp_path: Path) -> None:
    plan = _competing_plan(case)
    transport = ScriptedAuthoringTransport([json.dumps(plan), json.dumps(plan)])

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="competing",
        policy=ONE_PLAN_CORRECTION,
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    evidence = json.loads(Path(result.failure_evidence_path).read_text())
    for attempt in evidence["attempts"]:
        assert attempt["decoded_output"]["runtime_bindings"] == plan["runtime_bindings"][:2]
        assert [
            (finding["code"], finding["path"], finding["detail"])
            for finding in attempt["findings"]
        ] == _COMPETING_FINDINGS[case]
    assert "prerequisite_type_mismatch" not in json.dumps(transport.requests[1], default=str)


@pytest.mark.parametrize("case", sorted(_COMPETING))
def test_normalizing_competing_declarations_again_closes_the_one_left_last(case: str) -> None:
    """The plan checks did the same before normalization became one step."""

    inventory = _inventory()
    normalized = normalize_plan_v2(_competing_plan(case), inventory)
    again = normalize_plan_v2(normalized.plan, inventory)

    first, middle = normalized.plan["runtime_bindings"]
    assert again.plan["runtime_bindings"] == [
        first,
        {**middle, "consumers": [*middle["consumers"], "prerequisites.owned_order"]},
    ]
    assert [(item["transformation"], item["binding"]) for item in again.transformations] == [
        ("binding_consumer_added", "owned_order")
    ]


@pytest.mark.parametrize(
    ("plan", "inventory"),
    [
        pytest.param(_plan, _inventory, id="refund"),
        pytest.param(_plan_needing_every_rewrite, _orders_inventory, id="every-rewrite"),
        pytest.param(_closure_duplicate_plan, _inventory, id="closure-duplicate"),
        pytest.param(_unresolved_shorthand_plan, _orders_inventory, id="unresolved-shorthand"),
    ],
)
def test_normalization_is_idempotent_without_competing_declarations(
    plan: Any, inventory: Any
) -> None:
    normalized = normalize_plan_v2(plan(), inventory())
    again = normalize_plan_v2(normalized.plan, inventory())

    assert again.plan == normalized.plan
    assert again.transformations == []
    assert again.source_refs == normalized.source_refs


def test_the_orchestrator_records_the_rewrites_of_its_one_normalization(tmp_path: Path) -> None:
    inventory = _orders_inventory()
    plan = _plan_needing_every_rewrite()
    metadata = _metadata_needing_slots()
    normalized_plan, plan_rewrites, _, _ = normalize_plan_v2(plan, inventory)
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
