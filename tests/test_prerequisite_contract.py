from __future__ import annotations

from asago_artifact_generator.authoring import (
    build_call1_packet,
    build_call2_packet,
    collect_artifact_findings,
    collect_plan_findings,
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
