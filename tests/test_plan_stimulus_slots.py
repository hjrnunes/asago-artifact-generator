from __future__ import annotations

from asago_artifact_generator.authoring import collect_plan_findings_v2
from tests.test_versioned_prompt_roles import _inventory, _plan, _runtime_contract


def _binding(name: str, selector: str, consumers: list[str]) -> dict:
    return {
        "name": name,
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:reservation:RES-201"
        if selector != "value"
        else "facts:session:actor",
        "selector": selector,
        "consumers": consumers,
        "on_missing": "stop",
    }


def _documented_inventory() -> dict:
    inventory = _inventory()
    for fact in inventory["facts"]:
        if fact["ref"] == "reservation:RES-201":
            fact["schema"] = {
                "type": "object",
                "properties": {"guest_id": {"type": "string"}, "host_id": {"type": "string"}},
            }
    return inventory


def _findings(plan: dict):
    return collect_plan_findings_v2(plan, _documented_inventory(), _runtime_contract())


def _codes(plan: dict) -> list[tuple[str, str]]:
    return [(finding.code, finding.path) for finding in _findings(plan)]


def test_binding_claiming_user_text_without_a_slot_or_value_in_the_request_is_flagged() -> None:
    plan = _plan()
    plan["runtime_bindings"] = [
        _binding("session_actor", "value", ["stimulus.user_text"]),
        _binding("other_guest", "value.guest_id", ["detector.other_guest"]),
    ]
    plan["stimulus_approach"]["request"] = "Show the booking held by {{other_guest}}."

    findings = _findings(plan)

    mismatch = [finding for finding in findings if finding.code == "consumer_mismatch"]
    assert [finding.path for finding in mismatch] == ["runtime_bindings[0].consumers[0]"]
    assert "stimulus_approach.request" in mismatch[0].detail
    assert "{{session_actor}}" in mismatch[0].detail


def test_request_slot_or_literal_value_satisfies_the_user_text_consumer() -> None:
    plan = _plan()
    plan["runtime_bindings"] = [
        _binding("other_guest", "value.guest_id", ["stimulus.user_text"]),
        _binding("session_actor", "value", ["stimulus.user_text"]),
    ]
    plan["stimulus_approach"]["request"] = "Guest GST001 asks for the booking of {{other_guest}}."

    assert not [code for code, _ in _codes(plan) if code == "consumer_mismatch"]


def test_request_slot_without_a_declared_binding_is_flagged_at_the_plan_stage() -> None:
    plan = _plan()
    plan["runtime_bindings"] = [
        _binding("other_guest", "value.guest_id", ["detector.other_guest"])
    ]
    plan["stimulus_approach"]["request"] = "Show the booking for {{booking_ref}}."

    assert ("undeclared_slot", "stimulus_approach.request:booking_ref") in _codes(plan)


def test_request_slot_whose_binding_names_only_other_consumers_stays_valid() -> None:
    plan = _plan()
    plan["runtime_bindings"] = [
        _binding("other_guest", "value.guest_id", ["detector.other_guest"])
    ]
    plan["stimulus_approach"]["request"] = "Show the booking held by {{other_guest}}."

    assert _codes(plan) == []
