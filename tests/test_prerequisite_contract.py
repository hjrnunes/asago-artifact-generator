from __future__ import annotations

from asago_artifact_generator.authoring import (
    build_call1_packet,
    build_call1_packet_v2,
    build_call2_packet,
    collect_artifact_findings,
    collect_plan_findings,
    collect_plan_findings_v2,
)

from .test_authoring_orchestration import _artifact, _contract, _inventory, _plan, _view


def _executable_prerequisite(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "name": "booking_state",
        "source": "bindings.booking_state",
        "equals": "awaiting_review",
    }
    value.update(overrides)
    return value


def test_executable_prerequisites_are_admitted_by_both_validators() -> None:
    plan = _plan(prerequisites=[_executable_prerequisite()])
    artifact = _artifact(prerequisites=[_executable_prerequisite()])

    assert collect_plan_findings(plan, _inventory(), _contract()) == []
    assert collect_artifact_findings(artifact, plan, _inventory(), _contract()) == []


def test_binding_and_expected_prerequisite_fields_are_admitted() -> None:
    prerequisite = {
        "name": "booking_state",
        "binding": "booking_state",
        "expected": {"status": "awaiting_review", "attempts": 0},
    }
    plan = _plan(prerequisites=[prerequisite])
    artifact = _artifact(prerequisites=[prerequisite])

    assert collect_plan_findings(plan, _inventory(), _contract()) == []
    assert collect_artifact_findings(artifact, plan, _inventory(), _contract()) == []


def test_descriptive_prerequisites_remain_admitted_and_non_executable() -> None:
    prerequisite = {
        "name": "documented_policy",
        "evidence_refs": ["order:owned"],
        "check": "The supplied order is eligible.",
    }
    plan = _plan(prerequisites=[prerequisite])

    assert collect_plan_findings(plan, _inventory(), _contract()) == []


def test_prerequisite_contract_rejects_mistyped_executable_fields() -> None:
    prerequisite = _executable_prerequisite(source=["bindings.booking_state"])
    findings = collect_plan_findings(
        _plan(prerequisites=[prerequisite]), _inventory(), _contract()
    )

    assert any(
        finding.path == "prerequisites[0].source" and finding.code == "type_error"
        for finding in findings
    )


def test_prerequisite_contract_rejects_unknown_fields() -> None:
    prerequisite = _executable_prerequisite(operator="contains")
    findings = collect_plan_findings(
        _plan(prerequisites=[prerequisite]), _inventory(), _contract()
    )

    assert any(
        finding.path == "prerequisites[0].operator" and finding.code == "unexpected_field"
        for finding in findings
    )


def test_rendered_prerequisite_contract_matches_executable_union() -> None:
    packets = (
        build_call1_packet(_view(), _inventory(), _contract()),
        build_call2_packet(_view(), _plan(), _inventory(), _contract()),
    )

    for packet in packets:
        schema = packet.payload["response_contract"]["schema"]["properties"]["prerequisites"]
        prerequisite_schema = schema["items"]
        assert prerequisite_schema["required"] == ["name"]
        assert set(prerequisite_schema["properties"]) == {
            "name",
            "evidence_refs",
            "check",
            "source",
            "binding",
            "equals",
            "expected",
        }
        assert prerequisite_schema["properties"]["source"]["type"] == "string"
        assert prerequisite_schema["properties"]["binding"]["type"] == "string"
        assert prerequisite_schema["properties"]["equals"]["description"]
        assert prerequisite_schema["properties"]["expected"]["description"]


def _v2_inventory_and_binding() -> tuple[dict[str, object], dict[str, object]]:
    inventory = _inventory()
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
    schema = packet.payload["response_contract"]["schema"]["properties"]["prerequisites"]
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
    mismatch_findings = collect_plan_findings_v2(
        _v2_plan(runtime_bindings=[mismatched_binding], prerequisites=[base]),
        inventory,
        _contract(),
    )
    assert any(
        finding.code == "consumer_mismatch" and finding.path == "prerequisites[0].binding"
        for finding in mismatch_findings
    )

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
    assert (
        collect_plan_findings_v2(
            _v2_plan(runtime_bindings=[binding], prerequisites=[explicit_null]),
            inventory,
            _contract(),
        )
        == []
    )
