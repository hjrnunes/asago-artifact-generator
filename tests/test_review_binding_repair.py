"""Review findings about runtime bindings must reach the correction with documented choices."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from asago_artifact_generator.authoring.core import (
    CORRECTION_PROMPT_VERSION_V27,
    PLAN_REVIEW_PROMPT_VERSION_V17,
    Finding,
)
from asago_artifact_generator.authoring.correction import (
    _render_correction_packet,
    build_correction_context,
)
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import AuthoringPolicy
from asago_artifact_generator.authoring.prompt_context import build_plan_author_context
from asago_artifact_generator.authoring.review import (
    _review_finding_to_finding,
    build_plan_review_packet,
    build_plan_reviewer_context,
)

from . import test_versioned_authoring_wire as wire
from .support import ScriptedAuthoringTransport
from .test_binding_repair_options import (
    _NAMED_ORDER_SOURCES,
    _keyed_orders_inventory,
    _named_record_binding,
)
from .test_versioned_prompt_roles import _plan, _runtime_contract, _view

_RECORD_KEY_CHANGE = (
    "Change the selector for 'target_order_id' to 'value.ORD-201.record_key' so the "
    "binding holds the record identifier."
)


def _review_record(location: str, required_change: str = _RECORD_KEY_CHANGE) -> dict:
    return {
        "question": "value_meaning",
        "location": location,
        "problem": "The binding selects another field instead of the record identifier.",
        "basis": "The resolved value is the owner field, not the identifier.",
        "required_change": required_change,
    }


def _field_binding_plan() -> dict:
    plan = copy.deepcopy(_plan())
    plan["runtime_bindings"] = [
        _named_record_binding("facts:state:orders", "value.ORD-201.customer_id")
    ]
    return plan


def _correction_packet(plan: dict, findings: list[Finding], inventory: dict | None = None):
    inventory = inventory if inventory is not None else _keyed_orders_inventory()
    context = build_correction_context(
        failed_stage="call1",
        original_context=build_plan_author_context(_view(), inventory, _runtime_contract()),
        current_output=json.dumps(plan),
        findings=findings,
    )
    return _render_correction_packet(context)


def _section(packet, title: str) -> str:
    start = packet.user.index(f"{title}\n")
    end = packet.user.find("\n\n", start)
    return packet.user[start : end if end != -1 else None]


def test_reviewer_sees_the_record_key_source_of_a_record_field_binding() -> None:
    context = build_plan_reviewer_context(
        _view(), _field_binding_plan(), _keyed_orders_inventory(), _runtime_contract()
    )

    resolved = context["resolved_supplied_binding_values"]
    assert resolved["values"] == [
        {
            "name": "target_order_id",
            "source_ref": "facts:state:orders",
            "selector": "value.ORD-201.customer_id",
            "resolved_value": "ORD-201-customer_id",
            "record_key_source": {
                "source_ref": "facts:state:orders:records",
                "selector": "value.ORD-201.record_key",
                "resolved_value": "ORD-201",
            },
        }
    ]
    assert "record_key_source" in resolved["reviewer_instruction"]
    assert "source_ref and selector" in resolved["reviewer_instruction"]


def test_record_key_source_is_omitted_when_the_binding_already_selects_the_key() -> None:
    plan = _field_binding_plan()
    plan["runtime_bindings"] = [
        _named_record_binding("facts:state:orders:records", "value.ORD-201.record_key")
    ]

    context = build_plan_reviewer_context(
        _view(), plan, _keyed_orders_inventory(), _runtime_contract()
    )

    (value,) = context["resolved_supplied_binding_values"]["values"]
    assert value["resolved_value"] == "ORD-201"
    assert "record_key_source" not in value


def test_plan_review_packet_uses_the_new_version() -> None:
    packet = build_plan_review_packet(
        _view(), _field_binding_plan(), _keyed_orders_inventory(), _runtime_contract()
    )

    assert packet.version == PLAN_REVIEW_PROMPT_VERSION_V17
    assert '"record_key_source"' in packet.user


def test_plan_review_finding_keeps_its_location_and_required_change() -> None:
    record = _review_record("candidate_plan.runtime_bindings[0]")

    finding = _review_finding_to_finding(record, "plan")

    assert finding.code == "semantic_review"
    assert finding.path == "plan"
    assert finding.details == {
        "review_location": "candidate_plan.runtime_bindings[0]",
        "review_required_change": _RECORD_KEY_CHANGE,
    }


def test_first_correction_after_a_binding_review_lists_documented_record_sources() -> None:
    finding = _review_finding_to_finding(
        _review_record("candidate_plan.runtime_bindings[0]"), "plan"
    )

    packet = _correction_packet(_field_binding_plan(), [finding])

    assert packet.version == CORRECTION_PROMPT_VERSION_V27
    assert "BINDING REPAIR OPTIONS\n" in packet.user
    (option,) = packet.payload["binding_repair_options"]["options"]
    assert option["kind"] == "review_binding"
    assert option["code"] == "semantic_review"
    assert option["path"] == "runtime_bindings[0]"
    assert option["binding_name"] == "target_order_id"
    assert option["source_ref"] == "facts:state:orders"
    assert option["selector"] == "value.ORD-201.customer_id"
    assert option["named_record_key"] == "ORD-201"
    assert option["named_record_sources"] == _NAMED_ORDER_SOURCES
    assert "documented_selectors" not in option
    assert "truncated" not in option
    assert option["review_selector_checks"] == [
        {
            "selector": "value.ORD-201.record_key",
            "documented_on_binding_source": False,
            "documented_source_refs": ["facts:state:orders:records"],
        }
    ]
    descriptions = packet.payload["binding_repair_options"]["field_descriptions"]
    assert descriptions["review_selector_checks"]
    assert descriptions["selector"]
    assert "review_binding" in descriptions["kind"]


def test_correction_findings_do_not_repeat_review_details() -> None:
    finding = _review_finding_to_finding(
        _review_record("candidate_plan.runtime_bindings[0]"), "plan"
    )

    packet = _correction_packet(_field_binding_plan(), [finding])

    findings_section = _section(packet, "CURRENT FINDINGS")
    assert "review_location" not in findings_section
    assert "review_required_change" not in findings_section
    assert "Required change: " + _RECORD_KEY_CHANGE in findings_section


def test_review_naming_a_declared_binding_outside_its_location_gets_an_option() -> None:
    finding = _review_finding_to_finding(
        _review_record("candidate_plan.observation_claim.violation"), "plan"
    )

    packet = _correction_packet(_field_binding_plan(), [finding])

    (option,) = packet.payload["binding_repair_options"]["options"]
    assert option["kind"] == "review_binding"
    assert option["path"] == "runtime_bindings[0]"


def test_review_about_other_plan_fields_gets_no_binding_option() -> None:
    finding = _review_finding_to_finding(
        _review_record(
            "candidate_plan.observation_claim.violation",
            "State the violation with the supplied condition.",
        ),
        "plan",
    )

    packet = _correction_packet(_field_binding_plan(), [finding])

    assert "binding_repair_options" not in packet.payload
    assert "BINDING REPAIR OPTIONS" not in packet.user


def test_selector_finding_on_a_keyed_fact_lists_the_selected_record_sources() -> None:
    plan = _field_binding_plan()
    plan["runtime_bindings"] = [
        _named_record_binding("facts:state:orders", "value.ORD-201.record_key")
    ]
    finding = Finding(
        "plan_binding_validation",
        "undocumented selector for binding target_order_id: value.ORD-201.record_key",
        "runtime_bindings[0].selector",
    )

    packet = _correction_packet(plan, [finding])

    (option,) = packet.payload["binding_repair_options"]["options"]
    assert option["kind"] == "selector"
    assert option["named_record_key"] == "ORD-201"
    assert option["named_record_sources"] == _NAMED_ORDER_SOURCES


def test_orchestrated_binding_review_revision_carries_repair_options(tmp_path: Path) -> None:
    inventory = wire._inventory()
    inventory["facts"].extend(
        fact
        for fact in _keyed_orders_inventory()["facts"]
        if fact["ref"] in {"state:orders", "state:orders:records"}
    )
    plan = wire._plan()
    plan["runtime_bindings"].append(
        _named_record_binding("facts:state:orders", "value.ORD-201.customer_id")
    )
    review = json.dumps(
        {
            "decision": "revise",
            "summary": "The identifier binding selects another field.",
            "findings": [_review_record("candidate_plan.runtime_bindings[1]")],
        }
    ).encode()
    transport = ScriptedAuthoringTransport([json.dumps(plan), review, json.dumps(plan)])
    orchestrator = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="review-binding",
        policy=AuthoringPolicy(),
    )

    orchestrator.run(wire._view(), inventory, wire._runtime_contract())

    correction = transport.requests[2]
    assert correction["stage"] == "correction"
    assert "BINDING REPAIR OPTIONS\n" in correction["user"]
    assert '"kind":"review_binding"' in correction["user"]
    assert '"source_ref":"facts:state:orders:records"' in correction["user"]
