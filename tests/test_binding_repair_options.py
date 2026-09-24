from __future__ import annotations

import copy
import json

from asago_artifact_generator.authoring import (
    CORRECTION_PROMPT_VERSION_V6,
    Finding,
    _render_correction_packet,
    build_correction_context,
    build_plan_author_context,
)

from .test_versioned_prompt_roles import _inventory, _runtime_contract, _view


def _context(
    candidate: dict,
    inventory: dict,
    runtime_contract: dict,
    findings: list[Finding],
    *,
    legacy: bool = False,
):
    return build_correction_context(
        failed_stage="call1",
        original_context=build_plan_author_context(
            _view(),
            inventory,
            runtime_contract,
            legacy_interface=legacy,
        ),
        current_output=json.dumps(candidate),
        findings=findings,
        legacy_interface=legacy,
    )


def _candidate() -> dict:
    from .test_versioned_prompt_roles import _plan

    return copy.deepcopy(_plan())


def _option(packet):
    return packet.payload["binding_repair_options"]["options"][0]


def _unknown_binding_packet(binding_name: str):
    candidate = _candidate()
    candidate["prerequisites"] = [
        {
            "name": "record_ready",
            "check": "The record is ready.",
            "evidence_refs": ["reservation:RES-201"],
            "binding": binding_name,
            "equals": "ready",
        }
    ]
    return _render_correction_packet(
        _context(
            candidate,
            _inventory(),
            _runtime_contract(),
            [Finding("unknown_binding", "binding is not declared", "prerequisites[0].binding")],
        )
    )


def _section(packet, title: str, next_title: str) -> str:
    start = packet.user.index(f"{title}\n")
    end = packet.user.index(f"\n\n{next_title}", start)
    return packet.user[start:end]


def test_selector_repair_lists_documented_paths_and_compatible_types() -> None:
    inventory = _inventory()
    inventory["facts"][0]["schema"] = {
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "identifier": {"type": "string"},
            "items": {"type": "array", "items": {"type": "boolean"}},
        },
    }
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": "record_value",
            "expected_type": "number",
            "source_kind": "supplied_input",
            "source_ref": f"facts:{inventory['facts'][0]['ref']}",
            "selector": "value.missing",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    packet = _render_correction_packet(
        _context(
            candidate,
            inventory,
            _runtime_contract(),
            [Finding("plan_binding_validation", "bad selector", "runtime_bindings[0].selector")],
        )
    )

    option = _option(packet)
    assert option["documented_selectors"] == {
        "value": "object",
        "value.count": "integer",
        "value.identifier": "string",
        "value.items": "array",
        "value.items.items": "boolean",
    }
    assert option["matching_expected_type"] == ["value.count"]
    assert option["resolved_source"] is True
    assert option["truncated"] is False


def test_selector_repair_explicitly_reports_no_matching_type() -> None:
    inventory = _inventory()
    inventory["facts"][0]["schema"] = {"type": "object"}
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": "record_value",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": f"facts:{inventory['facts'][0]['ref']}",
            "selector": "value.identifier",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    packet = _render_correction_packet(
        _context(
            candidate,
            inventory,
            _runtime_contract(),
            [Finding("plan_binding_validation", "bad selector", "runtime_bindings[0].selector")],
        )
    )

    option = _option(packet)
    assert option["matching_expected_type"] == []
    assert option["no_matching_selector_note"] == (
        f"No documented selector of source facts:{inventory['facts'][0]['ref']} "
        "yields expected_type string."
    )


def test_unresolved_selector_source_lists_facts_and_permitted_setup_forms() -> None:
    inventory = _inventory()
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": "record_value",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:not-present",
            "selector": "value",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    runtime_contract = _runtime_contract()
    packet = _render_correction_packet(
        _context(
            candidate,
            inventory,
            runtime_contract,
            [Finding("plan_binding_validation", "unknown source", "runtime_bindings[0].selector")],
        )
    )

    option = _option(packet)
    assert option["resolved_source"] is False
    assert option["available_source_ref_forms"] == [
        "facts:draft:status",
        "facts:reservation:RES-201",
        "facts:session:actor",
        "setup:summarize_for_ehr",
    ]


def test_unknown_binding_lists_names_rule_and_fact_selector_sources() -> None:
    inventory = _inventory()
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": "existing",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:session:actor",
            "selector": "value",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    candidate["prerequisites"] = [
        {
            "name": "record_ready",
            "check": "The record is ready.",
            "evidence_refs": ["reservation:RES-201", "source:case"],
            "binding": "missing_binding",
            "equals": "ready",
        }
    ]
    packet = _render_correction_packet(
        _context(
            candidate,
            inventory,
            _runtime_contract(),
            [
                Finding(
                    "unknown_binding",
                    "binding is not declared",
                    "prerequisites[0].binding",
                )
            ],
        )
    )

    option = _option(packet)
    assert option["declared_binding_names"] == ["existing"]
    assert option["declaration_requirement"] == (
        'Add a runtime_bindings entry with name "missing_binding" whose consumers '
        'include "prerequisites.missing_binding".'
    )
    assert option["evidence_ref_sources"] == [
        {
            "documented_selectors": {"value": "object"},
            "evidence_ref": "reservation:RES-201",
            "source_kind": "supplied_input",
            "source_ref": "facts:reservation:RES-201",
            "source_schema_type": "object",
            "truncated": False,
        }
    ]


def test_consumer_mismatch_lists_the_exact_required_consumer() -> None:
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": "existing",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:session:actor",
            "selector": "value",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    candidate["prerequisites"] = [
        {
            "name": "record_ready",
            "check": "The record is ready.",
            "evidence_refs": ["reservation:RES-201"],
            "binding": "existing",
            "equals": "GST001",
        }
    ]
    packet = _render_correction_packet(
        _context(
            candidate,
            _inventory(),
            _runtime_contract(),
            [
                Finding(
                    "consumer_mismatch",
                    "consumer is missing",
                    "prerequisites[0].binding",
                )
            ],
        )
    )

    option = _option(packet)
    assert option["required_consumer"] == "prerequisites.existing"
    assert option["consumer_requirement"] == (
        'Add "prerequisites.existing" to the consumers list of the "existing" runtime binding.'
    )


def test_selector_enumeration_is_capped_with_an_explicit_note() -> None:
    inventory = _inventory()
    inventory["facts"][0]["schema"] = {
        "type": "object",
        "properties": {f"field_{index:02d}": {"type": "string"} for index in range(45)},
    }
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": "record_value",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": f"facts:{inventory['facts'][0]['ref']}",
            "selector": "value.unknown",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    packet = _render_correction_packet(
        _context(
            candidate,
            inventory,
            _runtime_contract(),
            [Finding("plan_binding_validation", "bad selector", "runtime_bindings[0].selector")],
        )
    )

    option = _option(packet)
    assert len(option["documented_selectors"]) == 40
    assert option["truncated"] is True
    assert "truncated after 40 selectors" in option["truncation_note"]


def test_legacy_plan_correction_does_not_render_binding_options() -> None:
    packet = _render_correction_packet(
        _context(
            _candidate(),
            _inventory(),
            _runtime_contract(),
            [Finding("unknown_binding", "bad binding", "prerequisites[0].binding")],
            legacy=True,
        )
    )

    assert packet.version == CORRECTION_PROMPT_VERSION_V6
    assert "BINDING REPAIR OPTIONS" not in packet.user
    assert "binding_repair_options" not in packet.payload


def test_static_repair_fields_are_case_independent_and_separate_from_options() -> None:
    a03_packet = _unknown_binding_packet("a03_binding")
    g07_packet = _unknown_binding_packet("g07_binding")

    a03_fields = _section(a03_packet, "BINDING REPAIR OPTION FIELDS", "BINDING REPAIR OPTIONS")
    g07_fields = _section(g07_packet, "BINDING REPAIR OPTION FIELDS", "BINDING REPAIR OPTIONS")
    assert a03_fields == g07_fields
    assert "a03_binding" not in a03_fields
    assert "g07_binding" not in g07_fields

    a03_options = _section(a03_packet, "BINDING REPAIR OPTIONS", "CORRECTION INSTRUCTIONS")
    assert '"description"' not in a03_options
    assert '"field_descriptions"' not in a03_options


def test_duplicate_findings_produce_one_option_per_kind_and_path() -> None:
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": "record_value",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:session:actor",
            "selector": "value.missing",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    finding = Finding("plan_binding_validation", "bad selector", "runtime_bindings[0].selector")
    packet = _render_correction_packet(
        _context(candidate, _inventory(), _runtime_contract(), [finding, finding])
    )

    options = packet.payload["binding_repair_options"]["options"]
    assert len(options) == 1
    assert (options[0]["kind"], options[0]["path"]) == (
        "selector",
        "runtime_bindings[0].selector",
    )


def test_alternatives_text_points_to_response_contract_guidance() -> None:
    packet = _unknown_binding_packet("missing_binding")
    fields = packet.payload["binding_repair_options"]

    assert fields["description"].startswith("binding_repair_options is deterministic")
    assert "If no listed option fits the scenario" in fields["description"]
    assert "RESPONSE CONTRACT empty_value_guidance entries still apply." in fields["description"]
    assert "prerequisites: []" not in fields["description"]
    assert "BINDING REPAIR OPTION FIELDS\n" in packet.user
    assert "BINDING REPAIR OPTIONS\n" in packet.user
