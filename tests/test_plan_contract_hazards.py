from __future__ import annotations

import copy
import hashlib

from asago_artifact_generator.authoring.checks import (
    _is_blocked_plan,
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
)
from asago_artifact_generator.authoring.core import (
    CALL1_PROMPT_VERSION_V18,
    CALL2_PROMPT_VERSION_V21,
    CORRECTION_PROMPT_VERSION_V27,
    PromptPacket,
)
from asago_artifact_generator.authoring.correction import (
    _render_correction_packet,
    build_correction_context,
)
from asago_artifact_generator.authoring.prompt_context import (
    build_artifact_author_context,
    build_plan_author_context,
)
from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
    build_call2_packet_v2,
)
from asago_artifact_generator.authoring.review import build_plan_review_packet

from .test_versioned_prompt_roles import (
    _framed,
    _inventory,
    _metadata,
    _plan,
    _runtime_contract,
    _view,
)

_CURRENT_PROMPT_DIGESTS = {
    "call1": "360badaeb0bf63f97616a7486056e1bb13260c149808a9cca8b0072337638058",
    "plan_correction": "c2ff111ae0e5db1f43e0f208fbcafdae511032b84c1daa70da09e59f33835b52",
    "plan_review": "4320f809299d88271625131f6f2b02a6ae129eaf104557968e4074e95304cf10",
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

    assert packet.version == CORRECTION_PROMPT_VERSION_V27
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
        build_call1_packet_v2(view, inventory, runtime_contract).version
        == CALL1_PROMPT_VERSION_V18
    )
    assert (
        build_call2_packet_v2(view, _plan(), inventory, runtime_contract).version
        == CALL2_PROMPT_VERSION_V21
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


def _hazard_inventory() -> dict:
    from .test_versioned_prompt_roles import _inventory

    inventory = _inventory()
    inventory["operations"].append(
        {
            "name": "typed_lookup",
            "description": "Look up a record with typed arguments.",
            "arguments": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "active": {"type": "boolean"},
                    "count": {"type": "integer"},
                    "ratio": {"type": "number"},
                    "filters": {"type": "object"},
                    "ids": {"type": "array"},
                    "free": {"type": "custom"},
                    "untyped": {},
                },
                "required": [],
            },
            "result_schema": {"type": "string"},
        }
    )
    inventory["operations"].append(
        {"name": "locked", "arguments": {}, "result_schema": {"type": "string"}}
    )
    return inventory


def _hazard_runtime_contract() -> dict:
    from .test_versioned_prompt_roles import _runtime_contract

    contract = _runtime_contract()
    contract["setup_permissions"] = ["summarize_for_ehr", "typed_lookup"]
    return contract


def _setup_findings(step: object) -> list[dict]:
    from .test_versioned_prompt_roles import _plan

    plan = copy.deepcopy(_plan())
    plan["setup_recipe"] = [step]
    return [
        finding.to_dict()
        for finding in collect_plan_findings_v2(
            plan, _hazard_inventory(), _hazard_runtime_contract()
        )
        if finding.path.startswith("setup_recipe")
    ]


def test_setup_step_shape_errors_are_reported_at_the_step() -> None:
    cases = {
        "x": "setup_recipe[0] must name an operation",
        "{}": "setup_recipe[0] must name an operation",
        "extra": "setup_recipe[0] has unsupported fields: ['extra']",
        "no_arguments": "setup_recipe[0] must include arguments",
        "unknown": "unknown setup operation: other",
        "list_arguments": "setup_recipe[0].arguments must be an object",
        "missing": "missing setup argument: patient_id",
        "unknown_argument": "unknown setup argument: x",
    }
    steps = {
        "x": "x",
        "{}": {},
        "extra": {"operation": "summarize_for_ehr", "arguments": {}, "extra": 1},
        "no_arguments": {"operation": "summarize_for_ehr"},
        "unknown": {"operation": "other", "arguments": {}},
        "list_arguments": {"operation": "summarize_for_ehr", "arguments": []},
        "missing": {"operation": "summarize_for_ehr", "arguments": {}},
        "unknown_argument": {
            "operation": "summarize_for_ehr",
            "arguments": {"patient_id": "P1", "x": 1},
        },
    }

    for name, detail in cases.items():
        assert _setup_findings(steps[name]) == [
            {"code": "plan_validation", "detail": detail, "path": "setup_recipe[0]"}
        ], name


def test_unpermitted_setup_step_is_an_unpermitted_setup_finding() -> None:
    assert _setup_findings({"operation": "locked", "arguments": {}}) == [
        {
            "code": "unpermitted_setup",
            "detail": "setup operation is not permitted: locked",
            "path": "setup_recipe[0]",
        }
    ]


def test_setup_arguments_matching_their_schema_types_are_accepted() -> None:
    step = {
        "operation": "typed_lookup",
        "arguments": {
            "label": "{{record_label}}",
            "active": True,
            "count": 2,
            "ratio": 0.5,
            "filters": {},
            "ids": [],
            "free": None,
            "untyped": 1,
        },
    }

    assert _setup_findings(step) == []


def test_setup_argument_type_mismatches_name_the_expected_type() -> None:
    mismatches = {
        "label": (3, "string"),
        "active": ("yes", "boolean"),
        "count": (True, "integer"),
        "ratio": (False, "number"),
        "filters": ([], "object"),
        "ids": ({}, "array"),
    }

    for argument, (value, expected) in mismatches.items():
        step = {"operation": "typed_lookup", "arguments": {argument: value}}
        assert _setup_findings(step) == [
            {
                "code": "schema_type_mismatch",
                "detail": (f"schema_type_mismatch: setup argument {argument} expects {expected}"),
                "path": "setup_recipe[0]",
            }
        ], argument


def test_plan_root_type_errors_for_assumptions_and_required_observations() -> None:
    from .test_versioned_prompt_roles import _inventory, _plan, _runtime_contract

    plan = copy.deepcopy(_plan())
    plan["assumptions"] = "none"
    plan["required_observations"] = []

    findings = collect_plan_findings_v2(plan, _inventory(), _runtime_contract())

    assert [finding.to_dict() for finding in findings] == [
        {"code": "type_error", "detail": "assumptions must be a list", "path": "assumptions"},
        {
            "code": "type_error",
            "detail": "required_observations must be an object",
            "path": "required_observations",
        },
    ]


def test_each_malformed_assumption_is_reported_in_order() -> None:
    from .test_versioned_prompt_roles import _inventory, _plan, _runtime_contract

    plan = copy.deepcopy(_plan())
    plan["assumptions"] = [
        "x",
        {"ref": 3, "reason": 4, "extra": 1},
        {"ref": "nope", "reason": "r"},
    ]

    findings = collect_plan_findings_v2(plan, _inventory(), _runtime_contract())

    assert [finding.to_dict() for finding in findings] == [
        {
            "code": "shape_error",
            "detail": "assumption must be an object",
            "path": "assumptions[0]",
        },
        {
            "code": "unexpected_field",
            "detail": "unexpected assumption field: extra",
            "path": "assumptions[1].extra",
        },
        {
            "code": "type_error",
            "detail": "assumption.ref must be a string",
            "path": "assumptions[1].ref",
        },
        {
            "code": "type_error",
            "detail": "assumption.reason must be a string",
            "path": "assumptions[1].reason",
        },
        {
            "code": "unknown_reference",
            "detail": "unknown_reference: nope",
            "path": "assumptions[2].ref",
        },
    ]


def test_binding_sources_report_unknown_facts_and_unpermitted_setup() -> None:
    from .test_versioned_prompt_roles import _plan

    def binding(name: str, kind: str, ref: str, selector: str) -> dict:
        return {
            "name": name,
            "expected_type": "string",
            "source_kind": kind,
            "source_ref": ref,
            "selector": selector,
            "consumers": ["detector.value"],
            "on_missing": "stop",
        }

    plan = copy.deepcopy(_plan())
    plan["runtime_bindings"] = [
        binding("a", "supplied_input", "facts:missing", "value"),
        binding("b", "setup_output", "setup:locked", "result"),
    ]

    findings = collect_plan_findings_v2(plan, _hazard_inventory(), _hazard_runtime_contract())

    assert [finding.to_dict() for finding in findings if finding.path.endswith(".source_ref")] == [
        {
            "code": "plan_binding_validation",
            "detail": "unknown supplied fact: missing",
            "path": "runtime_bindings[0].source_ref",
        },
        {
            "code": "plan_binding_validation",
            "detail": "setup operation is not permitted: locked",
            "path": "runtime_bindings[1].source_ref",
        },
    ]
