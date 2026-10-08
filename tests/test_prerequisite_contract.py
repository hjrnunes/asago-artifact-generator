from __future__ import annotations

import pytest

from asago_artifact_generator.authoring.checks import (
    collect_plan_findings_v2,
)
from asago_artifact_generator.authoring.prompt_packets import build_call1_packet_v2

from .support import rendered_response_contract, world_builders

_contract, _inventory, _plan, _view = world_builders(
    "refund-minimal", "runtime_contract", "inventory", "plan", "view"
)


def _v2_inventory_and_binding() -> tuple[dict[str, object], dict[str, object]]:
    inventory = _inventory()
    inventory["facts"][0]["value"] = {"status": "awaiting_review"}
    inventory["facts"][0]["schema"] = {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
        },
    }
    binding = {
        "name": "booking_state",
        "expected_type": "object",
        "source_kind": "supplied_input",
        "source_ref": "facts:order:owned",
        "selector": "value",
        "consumers": ["prerequisites.booking_state"],
        "on_missing": "stop",
    }
    return inventory, binding


def _v2_plan(**overrides: object) -> dict[str, object]:
    plan = _plan(
        assumptions=[],
        required_observations={"tool_calls": {"required": True, "missing": "inconclusive"}},
    )
    plan.update(overrides)
    return plan


def test_v2_prerequisites_use_only_canonical_fields_and_declared_binding() -> None:
    inventory, binding = _v2_inventory_and_binding()
    prerequisite = {
        "name": "booking_state",
        "check": "The supplied booking is ready.",
        "evidence_refs": ["order:owned"],
        "binding": "booking_state",
        "equals": {"status": "awaiting_review"},
    }
    plan = _v2_plan(runtime_bindings=[binding], prerequisites=[prerequisite])

    assert collect_plan_findings_v2(plan, inventory, _contract()) == []

    packet = build_call1_packet_v2(_view(), inventory, _contract())
    schema = rendered_response_contract(packet)["schema"]["properties"]["prerequisites"]
    assert schema["items"]["required"] == [
        "name",
        "check",
        "evidence_refs",
        "binding",
        "equals",
    ]
    assert set(schema["items"]["properties"]) == {
        "name",
        "check",
        "evidence_refs",
        "binding",
        "equals",
    }


def test_v2_rejects_intended_safe_behavior_as_prerequisite() -> None:
    inventory, binding = _v2_inventory_and_binding()
    prerequisite = {
        "name": "safe_behavior",
        "check": _plan()["interpretation"]["safe_alternative"],
        "evidence_refs": ["order:owned"],
        "binding": "booking_state",
        "equals": {"status": "awaiting_review"},
    }

    findings = collect_plan_findings_v2(
        _v2_plan(runtime_bindings=[binding], prerequisites=[prerequisite]),
        inventory,
        _contract(),
    )

    assert any(
        finding.code == "desired_behavior_prerequisite"
        and finding.path == "prerequisites[0].check"
        for finding in findings
    )


def test_v2_prerequisites_reject_closed_forms() -> None:
    inventory, binding = _v2_inventory_and_binding()
    base = {
        "name": "booking_state",
        "check": "The supplied booking is ready.",
        "evidence_refs": ["order:owned"],
        "binding": "booking_state",
        "equals": {"status": "awaiting_review"},
    }

    aliases = {
        **base,
        "source": "bindings.booking_state",
        "expected": {"status": "awaiting_review"},
    }
    alias_findings = collect_plan_findings_v2(
        _v2_plan(runtime_bindings=[binding], prerequisites=[aliases]),
        inventory,
        _contract(),
    )
    assert {finding.path for finding in alias_findings if finding.code == "unexpected_field"} >= {
        "prerequisites[0].source",
        "prerequisites[0].expected",
    }

    bare_evidence = {**base, "binding": "order:owned"}
    bare_findings = collect_plan_findings_v2(
        _v2_plan(runtime_bindings=[binding], prerequisites=[bare_evidence]),
        inventory,
        _contract(),
    )
    assert any(
        finding.code == "unknown_binding" and finding.path == "prerequisites[0].binding"
        for finding in bare_findings
    )

    unknown_binding = {**base, "binding": "missing_binding"}
    unknown_findings = collect_plan_findings_v2(
        _v2_plan(runtime_bindings=[binding], prerequisites=[unknown_binding]),
        inventory,
        _contract(),
    )
    assert any(
        finding.code == "unknown_binding" and finding.path == "prerequisites[0].binding"
        for finding in unknown_findings
    )

    mismatched_binding = {**binding, "consumers": ["stimulus.user_text"]}
    mismatch_plan = _v2_plan(runtime_bindings=[mismatched_binding], prerequisites=[base])
    mismatch_plan["stimulus_approach"]["request"] = "Review booking {{booking_state}}."
    mismatch_findings = collect_plan_findings_v2(mismatch_plan, inventory, _contract())
    assert mismatch_findings == []
    assert mismatch_plan["runtime_bindings"][0]["consumers"] == [
        "stimulus.user_text",
        "prerequisites.booking_state",
    ]

    omitted_equals = {key: value for key, value in base.items() if key != "equals"}
    omitted_findings = collect_plan_findings_v2(
        _v2_plan(runtime_bindings=[binding], prerequisites=[omitted_equals]),
        inventory,
        _contract(),
    )
    assert any(
        finding.code == "missing_field" and finding.path == "prerequisites[0].equals"
        for finding in omitted_findings
    )

    explicit_null = {**base, "equals": None}
    inventory["facts"][0]["value"] = None
    assert (
        collect_plan_findings_v2(
            _v2_plan(runtime_bindings=[binding], prerequisites=[explicit_null]),
            inventory,
            _contract(),
        )
        == []
    )


def _canonical_prerequisite() -> dict[str, object]:
    return {
        "name": "booking_state",
        "check": "The supplied booking is ready.",
        "evidence_refs": ["order:owned"],
        "binding": "booking_state",
        "equals": {"status": "awaiting_review"},
    }


def _prerequisite_findings(prerequisite: dict[str, object]) -> list[tuple[str, str]]:
    inventory, binding = _v2_inventory_and_binding()
    findings = collect_plan_findings_v2(
        _v2_plan(runtime_bindings=[binding], prerequisites=[prerequisite]),
        inventory,
        _contract(),
    )
    return [
        (finding.code, finding.path)
        for finding in findings
        if finding.path.startswith("prerequisites[0]")
    ]


def test_v2_missing_prerequisite_name_yields_each_canonical_finding_once() -> None:
    prerequisite = _canonical_prerequisite()
    del prerequisite["name"]

    findings = _prerequisite_findings(prerequisite)

    assert sorted(findings) == [
        ("missing_field", "prerequisites[0].name"),
        ("type_error", "prerequisites[0].name"),
    ]


def test_v2_prerequisite_source_field_yields_only_the_canonical_finding() -> None:
    prerequisite = {**_canonical_prerequisite(), "source": ["bindings.booking_state"]}

    findings = _prerequisite_findings(prerequisite)

    assert findings == [("unexpected_field", "prerequisites[0].source")]


def test_v2_unknown_prerequisite_field_yields_only_the_canonical_finding() -> None:
    prerequisite = {**_canonical_prerequisite(), "operator": "contains"}

    findings = _prerequisite_findings(prerequisite)

    assert findings == [("unexpected_field", "prerequisites[0].operator")]


def _empty_consumer_plan(*, prerequisites: list[dict[str, object]]) -> tuple[dict, dict]:
    inventory, binding = _v2_inventory_and_binding()
    plan = _v2_plan(runtime_bindings=[{**binding, "consumers": []}], prerequisites=prerequisites)
    return plan, inventory


def test_binding_a_prerequisite_uses_needs_no_consumer_from_the_model() -> None:
    plan, inventory = _empty_consumer_plan(prerequisites=[_canonical_prerequisite()])
    rewrites: list[dict[str, object]] = []

    findings = collect_plan_findings_v2(plan, inventory, _contract(), transformations=rewrites)

    assert [finding for finding in findings if finding.path.startswith("runtime_bindings")] == []
    assert plan["runtime_bindings"][0]["consumers"] == ["prerequisites.booking_state"]
    assert [rewrite["transformation"] for rewrite in rewrites] == ["binding_consumer_added"]


def test_binding_no_prerequisite_uses_still_needs_a_consumer() -> None:
    plan, inventory = _empty_consumer_plan(prerequisites=[])

    findings = collect_plan_findings_v2(plan, inventory, _contract())

    assert [
        (finding.code, finding.path)
        for finding in findings
        if finding.path.startswith("runtime_bindings")
    ] == [("plan_binding_validation", "runtime_bindings[0].consumers")]


def test_collecting_a_plan_twice_records_the_consumer_rewrite_once() -> None:
    plan, inventory = _empty_consumer_plan(prerequisites=[_canonical_prerequisite()])
    rewrites: list[dict[str, object]] = []

    collect_plan_findings_v2(plan, inventory, _contract(), transformations=rewrites)
    collect_plan_findings_v2(plan, inventory, _contract(), transformations=rewrites)

    assert [rewrite["transformation"] for rewrite in rewrites] == ["binding_consumer_added"]


def _value_plan(
    value: object, equals: object, *, source_kind: str = "supplied_input", **binding: object
) -> tuple[dict, dict]:
    """A plan whose one prerequisite compares ``equals`` with the supplied fact ``value``."""

    inventory, declared = _v2_inventory_and_binding()
    inventory["facts"][0]["value"] = value
    declared = {**declared, "source_kind": source_kind, **binding}
    prerequisite = {**_canonical_prerequisite(), "equals": equals}
    return _v2_plan(runtime_bindings=[declared], prerequisites=[prerequisite]), inventory


def _mismatches(plan: dict, inventory: dict) -> list[tuple[str, str]]:
    return [
        (finding.code, finding.path)
        for finding in collect_plan_findings_v2(plan, inventory, _contract())
        if finding.code == "prerequisite_value_mismatch"
    ]


def test_a_prerequisite_equal_to_the_resolved_supplied_value_passes() -> None:
    record = {"id": "RES-104", "status": "awaiting_review"}

    assert _mismatches(*_value_plan(record, dict(record))) == []


@pytest.mark.parametrize(
    ("value", "equals"),
    [
        pytest.param({"id": "RES-104", "status": "open"}, {"id": "RES-104"}, id="partial-object"),
        pytest.param(
            '{"result": "NO_WHITELIST_HIT"}', "NO_WHITELIST_HIT", id="json-string-status"
        ),
        pytest.param({"status": "open"}, {"status": "closed"}, id="different-value"),
        pytest.param({"status": "open"}, None, id="null-expected-non-null-value"),
        pytest.param(None, {"status": "open"}, id="null-value-non-null-expected"),
    ],
)
def test_a_prerequisite_that_differs_from_the_resolved_supplied_value_is_rejected(
    value: object, equals: object
) -> None:
    plan, inventory = _value_plan(value, equals)

    assert _mismatches(plan, inventory) == [
        ("prerequisite_value_mismatch", "prerequisites[0].equals")
    ]


def test_a_null_prerequisite_equal_to_a_null_supplied_value_passes() -> None:
    assert _mismatches(*_value_plan(None, None)) == []


def test_the_mismatch_names_the_resolved_value_and_cuts_it_at_200_characters() -> None:
    plan, inventory = _value_plan({"note": "x" * 500}, {"note": "y"})

    (finding,) = [
        finding
        for finding in collect_plan_findings_v2(plan, inventory, _contract())
        if finding.code == "prerequisite_value_mismatch"
    ]

    assert finding.stage == "plan"
    assert '"note": "xxx' in finding.detail
    assert "x" * 201 not in finding.detail
    assert "booking_state" in finding.detail


def test_the_mismatch_names_the_whole_short_value() -> None:
    plan, inventory = _value_plan({"status": "open"}, {"status": "closed"})

    (finding,) = [
        finding
        for finding in collect_plan_findings_v2(plan, inventory, _contract())
        if finding.code == "prerequisite_value_mismatch"
    ]

    assert '{"status": "open"}' in finding.detail


def test_a_setup_output_binding_is_not_compared() -> None:
    plan, inventory = _value_plan(
        {"status": "open"},
        {"status": "closed"},
        source_kind="setup_output",
        source_ref="setup:create_order",
        selector="result.id",
    )

    assert _mismatches(plan, inventory) == []


def test_an_unresolvable_supplied_binding_is_not_compared() -> None:
    plan, inventory = _value_plan(
        {"status": "open"}, {"status": "closed"}, source_ref="facts:order:missing"
    )

    assert _mismatches(plan, inventory) == []


def test_a_prerequisite_without_equals_is_left_to_the_closed_form_check() -> None:
    plan, inventory = _value_plan({"status": "open"}, None)
    del plan["prerequisites"][0]["equals"]

    findings = collect_plan_findings_v2(plan, inventory, _contract())

    assert "prerequisite_value_mismatch" not in [finding.code for finding in findings]


def test_the_value_check_survives_malformed_bindings_and_prerequisites() -> None:
    plan, inventory = _value_plan({"status": "open"}, {"status": "closed"})
    plan["prerequisites"].append("not an object")
    plan["runtime_bindings"].append(7)

    findings = collect_plan_findings_v2(plan, inventory, _contract())

    assert [finding.code for finding in findings].count("prerequisite_value_mismatch") == 1
