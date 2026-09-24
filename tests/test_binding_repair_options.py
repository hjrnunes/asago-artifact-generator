from __future__ import annotations

import copy
import hashlib
import json

from asago_artifact_generator.authoring import (
    CORRECTION_PROMPT_VERSION_V6,
    CORRECTION_PROMPT_VERSION_V9,
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


def test_legacy_v9_render_matches_head_2097438() -> None:
    inventory = _inventory()
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": "record_value",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:not-present",
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
            [
                Finding(
                    "plan_binding_validation",
                    "bad source",
                    "runtime_bindings[0].source_ref",
                ),
                Finding(
                    "plan_binding_validation",
                    "bad selector",
                    "runtime_bindings[0].selector",
                ),
            ],
        ),
        legacy_v9=True,
    )

    assert packet.version == CORRECTION_PROMPT_VERSION_V9
    assert hashlib.sha256((packet.system + "\x00" + packet.user).encode()).hexdigest() == (
        "4c5a4c9590ee4d3a3b8e5e870b8f29978cec300f946a4e3b99fc494be11a44ec"
    )
    assert packet.sha256 == "e6138aa08a8ec5b868cdd08c9f6d025db9b54fdadac96ef50cab4588c69be4be"


def test_legacy_v10_without_binding_options_preserves_v9_prompt_bytes() -> None:
    context = _context(_candidate(), _inventory(), _runtime_contract(), [])

    v9 = _render_correction_packet(context, legacy_v9=True)
    v10 = _render_correction_packet(context, legacy_v10=True)

    assert v9.version == CORRECTION_PROMPT_VERSION_V9
    assert v10.version != v9.version
    assert (v10.system, v10.user) == (v9.system, v9.user)


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
    assert option["kind"] == "selector"
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


def test_unpermitted_setup_source_lists_referenced_facts_and_no_setup_sources() -> None:
    inventory = _inventory()
    inventory["facts"][1]["schema"] = {
        "type": "object",
        "properties": {
            "guest_id": {"type": "string"},
            "host_id": {"type": "string"},
        },
    }
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": "patient_id",
            "expected_type": "string",
            "source_kind": "setup_output",
            "source_ref": "setup:summarize_for_ehr",
            "selector": "result.patient_id",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    runtime_contract = _runtime_contract()
    runtime_contract["setup_permissions"] = []
    packet = _render_correction_packet(
        _context(
            candidate,
            inventory,
            runtime_contract,
            [
                Finding(
                    "plan_binding_validation",
                    "setup operation is not permitted",
                    "runtime_bindings[0].source_ref",
                )
            ],
        )
    )

    option = _option(packet)
    assert option["kind"] == "source"
    assert option["findings"] == [
        {
            "code": "plan_binding_validation",
            "path": "runtime_bindings[0].source_ref",
        }
    ]
    assert option["permitted_setup_sources"] == []
    assert option["permitted_setup_sources_truncated"] is False
    assert option["referenced_fact_sources"] == [
        {
            "documented_selectors": {
                "value": "object",
                "value.guest_id": "string",
                "value.host_id": "string",
            },
            "matching_expected_type": ["value.guest_id", "value.host_id"],
            "source_kind": "supplied_input",
            "source_ref": "facts:reservation:RES-201",
            "source_schema_type": "object",
            "truncated": False,
        }
    ]
    assert option["other_fact_source_refs"] == [
        "facts:draft:status",
        "facts:session:actor",
    ]


def test_source_and_selector_findings_merge_into_one_source_option() -> None:
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": "record_value",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:not-present",
            "selector": "value.missing",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    packet = _render_correction_packet(
        _context(
            candidate,
            _inventory(),
            _runtime_contract(),
            [
                Finding(
                    "plan_binding_validation",
                    "bad source",
                    "runtime_bindings[0].source_ref",
                ),
                Finding(
                    "plan_binding_validation",
                    "bad selector",
                    "runtime_bindings[0].selector",
                ),
            ],
        )
    )

    options = packet.payload["binding_repair_options"]["options"]
    assert len(options) == 1
    assert options[0]["kind"] == "source"
    assert options[0]["findings"] == [
        {
            "code": "plan_binding_validation",
            "path": "runtime_bindings[0].source_ref",
        },
        {
            "code": "plan_binding_validation",
            "path": "runtime_bindings[0].selector",
        },
    ]


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


def test_selector_only_finding_on_resolving_source_keeps_selector_option_shape() -> None:
    inventory = _inventory()
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
    packet = _render_correction_packet(
        _context(
            candidate,
            inventory,
            _runtime_contract(),
            [Finding("plan_binding_validation", "bad selector", "runtime_bindings[0].selector")],
        )
    )

    option = _option(packet)
    assert option["kind"] == "selector"
    assert option["path"] == "runtime_bindings[0].selector"
    assert "findings" not in option
    assert option["resolved_source"] is True


def test_source_kind_finding_triggers_a_source_option() -> None:
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": "record_value",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:session:actor",
            "selector": "value",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    packet = _render_correction_packet(
        _context(
            candidate,
            _inventory(),
            _runtime_contract(),
            [
                Finding(
                    "plan_binding_validation",
                    "bad source kind",
                    "runtime_bindings[0].source_kind",
                )
            ],
        )
    )

    option = _option(packet)
    assert option["kind"] == "source"
    assert option["findings"] == [
        {
            "code": "plan_binding_validation",
            "path": "runtime_bindings[0].source_kind",
        }
    ]


def test_unresolved_selector_source_lists_facts_and_permitted_setup_sources() -> None:
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
    assert option["kind"] == "source"
    assert option["resolved_source"] is False
    assert option["referenced_fact_sources"] == [
        {
            "documented_selectors": {"value": "object"},
            "matching_expected_type": [],
            "source_kind": "supplied_input",
            "source_ref": "facts:reservation:RES-201",
            "source_schema_type": "object",
            "truncated": False,
        }
    ]
    assert option["permitted_setup_sources"][0]["source_ref"] == ("setup:summarize_for_ehr")
    assert option["permitted_setup_sources"][0]["documented_selectors"] == {
        "result": "object",
        "result.draft_id": "string",
        "result.patient_id": "string",
        "result.status": "string",
    }
    assert option["other_fact_source_refs"] == [
        "facts:draft:status",
        "facts:session:actor",
    ]


def test_substring_fact_reference_is_not_considered_a_citation() -> None:
    inventory = _inventory()
    inventory["facts"].append(
        {
            "ref": "session:actor:extended",
            "value": "GST001-extra",
            "schema": {"type": "string"},
            "provenance": "extended actor state",
        }
    )
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
    candidate["interpretation"]["failure"] = "facts:session:actor:extended is not exact"
    packet = _render_correction_packet(
        _context(
            candidate,
            inventory,
            _runtime_contract(),
            [Finding("plan_binding_validation", "bad source", "runtime_bindings[0].source_ref")],
        )
    )

    option = _option(packet)
    assert "facts:session:actor:extended" in option["other_fact_source_refs"]
    assert not any(
        item["source_ref"] == "facts:session:actor:extended"
        for item in option["referenced_fact_sources"]
    )


def test_source_lists_cap_referenced_setup_and_other_fact_sources() -> None:
    inventory = {
        "facts": [
            {
                "ref": f"referenced:{index:02d}",
                "value": f"value-{index}",
                "schema": {"type": "string"},
                "provenance": "test",
            }
            for index in range(41)
        ]
        + [
            {
                "ref": f"other:{index:02d}",
                "value": f"value-{index}",
                "schema": {"type": "string"},
                "provenance": "test",
            }
            for index in range(41)
        ],
        "operations": [
            {
                "name": f"operation_{index:02d}",
                "result_schema": {"type": "string"},
                "description": "test operation",
            }
            for index in range(41)
        ],
    }
    candidate = _candidate()
    candidate["selected_evidence"] = []
    candidate["interpretation"]["source_refs"] = [f"referenced:{index:02d}" for index in range(41)]
    candidate["runtime_bindings"] = [
        {
            "name": "record_value",
            "expected_type": "string",
            "source_kind": "setup_output",
            "source_ref": "setup:not-present",
            "selector": "result",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
    ]
    runtime_contract = {"setup_permissions": [f"operation_{index:02d}" for index in range(41)]}
    packet = _render_correction_packet(
        _context(
            candidate,
            inventory,
            runtime_contract,
            [Finding("plan_binding_validation", "bad source", "runtime_bindings[0].source_ref")],
        )
    )

    option = _option(packet)
    assert len(option["referenced_fact_sources"]) == 40
    assert option["referenced_fact_sources_truncated"] is True
    assert len(option["permitted_setup_sources"]) == 40
    assert option["permitted_setup_sources_truncated"] is True
    assert len(option["other_fact_source_refs"]) == 40
    assert option["other_fact_source_refs_truncated"] is True
    assert (
        "referenced fact source and permitted setup source and other fact source"
        in option["truncation_note"]
    )


def test_source_option_fields_describe_new_fields() -> None:
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
    packet = _render_correction_packet(
        _context(
            candidate,
            _inventory(),
            _runtime_contract(),
            [Finding("plan_binding_validation", "bad source", "runtime_bindings[0].source_ref")],
        )
    )

    descriptions = packet.payload["binding_repair_options"]["field_descriptions"]
    for name in (
        "findings",
        "referenced_fact_sources",
        "referenced_fact_sources_truncated",
        "permitted_setup_sources",
        "permitted_setup_sources_truncated",
        "other_fact_source_refs",
        "other_fact_source_refs_truncated",
    ):
        assert name in descriptions
        assert descriptions[name]
    assert "source" in descriptions["kind"]
    assert "available_source_ref_forms" not in descriptions
    assert "available_source_ref_forms" not in packet.user


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
