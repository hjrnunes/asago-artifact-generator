from __future__ import annotations

import copy
import hashlib

from asago_artifact_generator.authoring import (
    CALL1_PROMPT_VERSION_V5,
    CALL2_PROMPT_VERSION_V8,
    CORRECTION_PROMPT_VERSION_V9,
    PromptPacket,
    _is_blocked_plan,
    _render_correction_packet,
    build_artifact_author_context,
    build_call1_packet_v2,
    build_call2_packet_v2,
    build_correction_context,
    build_plan_author_context,
    build_plan_review_packet,
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
)

from .test_versioned_prompt_roles import (
    _framed,
    _inventory,
    _metadata,
    _plan,
    _runtime_contract,
    _view,
)

_CURRENT_PROMPT_DIGESTS = {
    "call1": "d938d5e37faa45d14e73e68a5b9c6e68238e04cd1d6af33a7c98d5e83d62f033",
    "plan_correction": "a1c968d5373de5631301c8c44ea92879f05b41f3cbd0c25dcc8fa820453a7706",
    "plan_review": "638bdd7828a07f5953b12b64312dacd5476e3221a4dc7d9a1de33a08c375cdb1",
}


def _typed_inventory(expected_type: str) -> dict:
    inventory = copy.deepcopy(_inventory())
    inventory["facts"][0]["schema"] = {"type": expected_type}
    inventory["facts"][0]["value"] = {
        "string": "owned",
        "number": 1.5,
        "integer": 1,
        "boolean": True,
        "object": {"order_id": "ord-1"},
        "array": ["ord-1"],
    }[expected_type]
    return inventory


def _typed_plan(expected_type: str, equals: object) -> dict:
    plan = copy.deepcopy(_plan())
    plan["runtime_bindings"] = [
        {
            "name": "owned_order",
            "expected_type": expected_type,
            "source_kind": "supplied_input",
            "source_ref": "facts:order:owned",
            "selector": "value",
            "consumers": ["prerequisites.owned_order"],
            "on_missing": "stop",
        }
    ]
    plan["prerequisites"] = [
        {
            "name": "owned_order",
            "check": "The supplied order is available.",
            "evidence_refs": ["order:owned"],
            "binding": "owned_order",
            "equals": equals,
        }
    ]
    plan["runtime_bindings"][0]["expected_type"] = expected_type
    plan["prerequisites"][0]["equals"] = equals
    return plan


def _guidance_schema_fields(contract: dict) -> tuple[str, ...]:
    schema = contract["schema"]
    fields = []
    for entry in contract["empty_value_guidance"]:
        current = schema
        for part in entry["field"].split("."):
            properties = current.get("properties", {})
            assert part in properties
            current = properties[part]
        fields.append(entry["field"])
    return tuple(fields)


def _prompt_digest(packet: PromptPacket) -> str:
    return hashlib.sha256((packet.system + "\x00" + packet.user).encode()).hexdigest()


def test_current_call1_replaces_pseudo_empty_shapes_with_schema_guidance() -> None:
    view = _view()
    inventory = _inventory()
    runtime_contract = _runtime_contract()
    plan = _plan()
    plan_correction = _render_correction_packet(
        build_correction_context(
            failed_stage="call1",
            original_context=build_plan_author_context(view, inventory, runtime_contract),
            current_output="{}",
            findings=[],
        )
    )
    artifact_correction = _render_correction_packet(
        build_correction_context(
            failed_stage="call2",
            original_context=build_artifact_author_context(
                view, plan, inventory, runtime_contract
            ),
            current_output=_framed(),
            findings=[],
        )
    )
    packets = (
        build_call1_packet_v2(view, inventory, runtime_contract),
        build_call2_packet_v2(view, plan, inventory, runtime_contract),
        plan_correction,
        artifact_correction,
    )

    assert "empty_shapes" not in packets[0].user
    assert "empty_shapes" not in packets[1].user
    assert "runtime_bindings_for_static_concrete_stimulus" not in packets[0].user
    assert "prerequisites_when_none_are_required" not in packets[0].user

    call1_contract = packets[0].payload["response_contract"]
    call2_contract = packets[1].payload["response_contract"]
    assert _guidance_schema_fields(call1_contract) == (
        "setup_recipe",
        "runtime_bindings",
        "runtime_bindings",
        "prerequisites",
        "unresolved_requirements",
    )
    assert (
        "Each entry names an existing response field and the value to use in the stated "
        "situation. The entries are not additional response fields." in packets[0].user
    )
    assert "empty_value_guidance" not in call2_contract


def test_current_contract_guidance_fields_resolve_to_response_schema_properties() -> None:
    view = _view()
    inventory = _inventory()
    runtime_contract = _runtime_contract()
    call1_contract = build_call1_packet_v2(view, inventory, runtime_contract).payload[
        "response_contract"
    ]
    call2_contract = build_call2_packet_v2(view, _plan(), inventory, runtime_contract).payload[
        "response_contract"
    ]

    assert _guidance_schema_fields(call1_contract)
    assert "empty_value_guidance" not in call2_contract


def test_prerequisite_equals_type_matches_declared_binding() -> None:
    cases = (
        ("object", "text", True),
        ("string", "text", False),
        ("integer", 1.5, True),
        ("number", True, True),
        ("object", None, False),
        ("string", None, False),
        ("integer", None, False),
        ("number", None, False),
        ("boolean", None, False),
        ("array", None, False),
    )
    for expected_type, equals, mismatch in cases:
        inventory = _typed_inventory(expected_type)
        plan = _typed_plan(expected_type, equals)
        findings = collect_plan_findings_v2(plan, inventory, _runtime_contract())
        type_findings = [
            finding for finding in findings if finding.code == "prerequisite_type_mismatch"
        ]
        assert bool(type_findings) is mismatch
        if mismatch:
            assert type_findings[0].path == "prerequisites[0].equals"
            assert plan["runtime_bindings"][0]["name"] in type_findings[0].detail
            assert f"expected_type {expected_type}" in type_findings[0].detail
        artifact_type_findings = [
            finding.code == "prerequisite_type_mismatch"
            for finding in collect_artifact_findings_v2(
                _metadata(), plan, inventory, _runtime_contract()
            )
        ]
        assert any(artifact_type_findings) is mismatch


def test_prerequisite_type_finding_is_rendered_in_plan_correction() -> None:
    inventory = _typed_inventory("object")
    plan = _typed_plan("object", "text")
    finding = next(
        finding
        for finding in collect_plan_findings_v2(plan, inventory, _runtime_contract())
        if finding.code == "prerequisite_type_mismatch"
    )
    context = build_correction_context(
        failed_stage="call1",
        original_context=build_plan_author_context(_view(), inventory, _runtime_contract()),
        current_output="{}",
        findings=[finding],
    )
    packet = _render_correction_packet(context)

    assert packet.version == CORRECTION_PROMPT_VERSION_V9
    assert finding.detail in packet.user


def test_current_prerequisite_schema_explains_check_as_non_executable_text() -> None:
    contract = build_call1_packet_v2(_view(), _inventory(), _runtime_contract()).payload[
        "response_contract"
    ]
    description = contract["schema"]["properties"]["prerequisites"]["items"]["properties"][
        "check"
    ]["description"]

    assert "starting condition" in description
    assert "declared binding's resolved value" in description
    assert "equals" in description
    assert "check is not evaluated" in description


def test_unobtainable_essential_setup_requirement_is_a_current_plan_finding() -> None:
    plan = copy.deepcopy(_plan())
    plan["unresolved_requirements"] = [
        {
            "name": "setup_binding_availability",
            "essential": True,
            "obtainable_via_setup": False,
            "source_kind": "setup_output",
            "reason": "The concrete stimulus does not need setup-derived values.",
        }
    ]

    findings = collect_plan_findings_v2(plan, _inventory(), _runtime_contract())

    finding = next(
        finding for finding in findings if finding.code == "unobtainable_essential_requirement"
    )
    assert finding.path == "unresolved_requirements[0]"
    assert "an essential requirement that cannot be obtained blocks the plan" in finding.detail
    assert "not needed for the experiment is not essential" in finding.detail
    assert not _is_blocked_plan(plan)


def test_unresolved_requirement_blocking_rules_remain_narrow() -> None:
    fact_plan = copy.deepcopy(_plan())
    fact_plan["unresolved_requirements"] = [
        {
            "name": "supplied_fact",
            "essential": True,
            "obtainable_via_setup": False,
            "source_kind": "fact",
            "reason": "The fact is unavailable.",
        }
    ]
    optional_plan = copy.deepcopy(_plan())
    optional_plan["unresolved_requirements"] = [
        {
            "name": "optional_setup",
            "essential": False,
            "obtainable_via_setup": False,
            "source_kind": "setup_output",
            "reason": "The experiment does not need it.",
        }
    ]

    assert not any(
        finding.code == "unobtainable_essential_requirement"
        for finding in collect_plan_findings_v2(fact_plan, _inventory(), _runtime_contract())
    )
    assert _is_blocked_plan(fact_plan)
    assert not any(
        finding.code == "unobtainable_essential_requirement"
        for finding in collect_plan_findings_v2(optional_plan, _inventory(), _runtime_contract())
    )
    assert not _is_blocked_plan(optional_plan)


def test_current_prompt_versions_cover_contract_changes() -> None:
    view = _view()
    inventory = _inventory()
    runtime_contract = _runtime_contract()
    assert (
        build_call1_packet_v2(view, inventory, runtime_contract).version == CALL1_PROMPT_VERSION_V5
    )
    assert (
        build_call2_packet_v2(view, _plan(), inventory, runtime_contract).version
        == CALL2_PROMPT_VERSION_V8
    )


def test_current_prompt_digests_pin_rendered_contract_evidence() -> None:
    view = _view()
    inventory = _inventory()
    runtime_contract = _runtime_contract()
    plan_correction = _render_correction_packet(
        build_correction_context(
            failed_stage="call1",
            original_context=build_plan_author_context(view, inventory, runtime_contract),
            current_output="{}",
            findings=[],
        )
    )

    packets = {
        "call1": build_call1_packet_v2(view, inventory, runtime_contract),
        "plan_correction": plan_correction,
        "plan_review": build_plan_review_packet(
            view,
            _plan(),
            inventory,
            runtime_contract,
        ),
    }

    assert {name: _prompt_digest(packet) for name, packet in packets.items()} == (
        _CURRENT_PROMPT_DIGESTS
    )
