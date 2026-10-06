from __future__ import annotations

import copy
import json

import pytest

from asago_artifact_generator.authoring.binding_repair import (
    CorrectionRepairInputs,
    _prerequisite_binding_repair_options,
    _repair_review_binding_option,
    _repair_selector_option,
    _review_binding_indices,
    _review_documented_sources,
    _supplied_value_empty_fields,
    correction_repair_inputs,
)
from asago_artifact_generator.authoring.checks import _binding_selector_type
from asago_artifact_generator.authoring.core import Finding
from asago_artifact_generator.authoring.correction import (
    _render_correction_packet,
    build_correction_context,
)
from asago_artifact_generator.authoring.prompt_context import build_plan_author_context

from .test_versioned_prompt_roles import _inventory, _runtime_contract, _view


def _context(
    candidate: dict,
    inventory: dict,
    runtime_contract: dict,
    findings: list[Finding],
) -> tuple[dict, CorrectionRepairInputs]:
    context = build_correction_context(
        failed_stage="call1",
        original_context=build_plan_author_context(_view(), inventory, runtime_contract),
        current_output=json.dumps(candidate),
        findings=findings,
    )
    return context, correction_repair_inputs(_view(), inventory, runtime_contract, plan=True)


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
        *_context(
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
        *_context(
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
        *_context(
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
        *_context(
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
        *_context(
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


def _empty_list_inventory() -> dict:
    inventory = _inventory()
    inventory["facts"].append(
        {
            "ref": "state:inbox",
            "meaning": "Captured target state at discovery time.",
            "schema": {"type": "array", "items": {}},
            "value": [],
        }
    )
    return inventory


def _empty_list_binding() -> dict:
    return {
        "name": "inbox_text",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:state:inbox",
        "selector": "value",
        "consumers": ["stimulus.user_text"],
        "on_missing": "stop",
    }


_EMPTY_LIST_NOTE = (
    "The supplied value of source facts:state:inbox is an empty list; "
    "it contains no element or field to bind."
)


def test_selector_repair_marks_a_source_whose_supplied_value_is_empty() -> None:
    candidate = _candidate()
    candidate["runtime_bindings"] = [_empty_list_binding()]
    packet = _render_correction_packet(
        *_context(
            candidate,
            _empty_list_inventory(),
            _runtime_contract(),
            [
                Finding(
                    "plan_binding_validation",
                    "binding type mismatch for inbox_text: expected string, source is array",
                    "runtime_bindings[0].selector",
                )
            ],
        )
    )

    option = _option(packet)
    assert option["kind"] == "selector"
    assert option["documented_selectors"] == {"value": "array"}
    assert option["supplied_value_empty"] is True
    assert option["supplied_value_empty_note"] == _EMPTY_LIST_NOTE
    descriptions = packet.payload["binding_repair_options"]["field_descriptions"]
    assert descriptions["supplied_value_empty"]
    assert descriptions["supplied_value_empty_note"]


def test_source_repair_marks_referenced_facts_whose_supplied_value_is_empty() -> None:
    candidate = _candidate()
    binding = _empty_list_binding()
    binding["source_ref"] = "state:inbox"
    candidate["runtime_bindings"] = [binding]
    packet = _render_correction_packet(
        *_context(
            candidate,
            _empty_list_inventory(),
            _runtime_contract(),
            [
                Finding(
                    "plan_binding_validation",
                    "supplied_input binding source_ref must be facts:<ref>: inbox_text",
                    "runtime_bindings[0].source_ref",
                )
            ],
        )
    )

    option = _option(packet)
    assert option["kind"] == "source"
    sources = {entry["source_ref"]: entry for entry in option["referenced_fact_sources"]}
    assert sources["facts:state:inbox"] == {
        "documented_selectors": {"value": "array"},
        "matching_expected_type": [],
        "source_kind": "supplied_input",
        "source_ref": "facts:state:inbox",
        "source_schema_type": "array",
        "supplied_value_empty": True,
        "supplied_value_empty_note": _EMPTY_LIST_NOTE,
        "truncated": False,
    }
    assert all(
        "supplied_value_empty" not in entry
        for ref, entry in sources.items()
        if ref != "facts:state:inbox"
    )


_ORDER_FIELDS = ("customer_id", "item", "status")


def _keyed_orders_inventory() -> dict:
    """Return keyed order facts whose full selector list exceeds the repair cap.

    ORD-201 sorts after fourteen other orders, so a collection-wide
    enumeration capped at 40 selectors never reaches it.
    """

    keys = [f"ORD-1{index:02d}" for index in range(1, 15)] + ["ORD-201"]
    inventory = _inventory()
    inventory["facts"].extend(
        [
            {
                "ref": "state:orders",
                "value": {
                    key: {field: f"{key}-{field}" for field in _ORDER_FIELDS} for key in keys
                },
                "schema": {
                    "type": "object",
                    "properties": {
                        key: {
                            "type": "object",
                            "properties": {field: {"type": "string"} for field in _ORDER_FIELDS},
                        }
                        for key in keys
                    },
                },
            },
            {
                "ref": "state:orders:records",
                "value": {key: {"record_key": key} for key in keys},
                "schema": {
                    "type": "object",
                    "properties": {
                        key: {"type": "object", "properties": {"record_key": {"type": "string"}}}
                        for key in keys
                    },
                },
            },
        ]
    )
    return inventory


def _named_record_binding(source_ref: str, selector: str) -> dict:
    return {
        "name": "target_order_id",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": source_ref,
        "selector": selector,
        "consumers": ["detector.target_order_id"],
        "on_missing": "stop",
    }


_NAMED_ORDER_SOURCES = [
    {
        "source_kind": "supplied_input",
        "source_ref": "facts:state:orders",
        "source_schema_type": "object",
        "documented_selectors": {
            "value.ORD-201": "object",
            "value.ORD-201.customer_id": "string",
            "value.ORD-201.item": "string",
            "value.ORD-201.status": "string",
        },
        "matching_expected_type": [
            "value.ORD-201.customer_id",
            "value.ORD-201.item",
            "value.ORD-201.status",
        ],
        "truncated": False,
    },
    {
        "source_kind": "supplied_input",
        "source_ref": "facts:state:orders:records",
        "source_schema_type": "object",
        "documented_selectors": {
            "value.ORD-201": "object",
            "value.ORD-201.record_key": "string",
        },
        "matching_expected_type": ["value.ORD-201.record_key"],
        "truncated": False,
    },
]


def _assert_named_sources_validate(inventory: dict, sources: list[dict]) -> None:
    schemas = {f"facts:{fact['ref']}": fact["schema"] for fact in inventory["facts"]}
    for source in sources:
        for selector, json_type in source["documented_selectors"].items():
            assert _binding_selector_type(schemas[source["source_ref"]], selector) == json_type


def test_selector_repair_lists_the_named_record_beyond_the_selector_cap() -> None:
    inventory = _keyed_orders_inventory()
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        _named_record_binding("facts:state:orders:ORD-201", "value.order_id")
    ]
    packet = _render_correction_packet(
        *_context(
            candidate,
            inventory,
            _runtime_contract(),
            [
                Finding(
                    "plan_binding_validation",
                    "undocumented selector for binding target_order_id: value.order_id",
                    "runtime_bindings[0].selector",
                )
            ],
        )
    )

    option = _option(packet)
    assert option["kind"] == "selector"
    assert option["truncated"] is True
    assert "value.ORD-201.customer_id" not in option["documented_selectors"]
    assert option["named_record_key"] == "ORD-201"
    assert option["named_record_sources"] == _NAMED_ORDER_SOURCES
    _assert_named_sources_validate(inventory, option["named_record_sources"])
    descriptions = packet.payload["binding_repair_options"]["field_descriptions"]
    assert descriptions["named_record_key"]
    assert descriptions["named_record_sources"]


def test_source_repair_lists_the_named_record_of_an_unresolved_field_shorthand() -> None:
    inventory = _keyed_orders_inventory()
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        _named_record_binding("facts:state:orders:ORD-201:order_id", "value")
    ]
    packet = _render_correction_packet(
        *_context(
            candidate,
            inventory,
            _runtime_contract(),
            [
                Finding(
                    "plan_binding_validation",
                    "unknown supplied fact: state:orders:ORD-201:order_id",
                    "runtime_bindings[0].source_ref",
                )
            ],
        )
    )

    option = _option(packet)
    assert option["kind"] == "source"
    assert option["resolved_source"] is False
    assert option["named_record_key"] == "ORD-201"
    assert option["named_record_sources"] == _NAMED_ORDER_SOURCES


def test_repair_options_list_the_record_an_exact_fact_selector_names() -> None:
    inventory = _keyed_orders_inventory()
    candidate = _candidate()
    candidate["runtime_bindings"] = [_named_record_binding("facts:state:orders", "value.ORD-201")]
    packet = _render_correction_packet(
        *_context(
            candidate,
            inventory,
            _runtime_contract(),
            [
                Finding(
                    "plan_binding_validation",
                    "binding type mismatch for target_order_id: expected string, source is object",
                    "runtime_bindings[0].selector",
                )
            ],
        )
    )

    option = _option(packet)
    assert option["kind"] == "selector"
    assert option["named_record_key"] == "ORD-201"
    assert option["named_record_sources"] == _NAMED_ORDER_SOURCES


def test_repair_options_omit_named_record_fields_for_a_whole_fact_selector() -> None:
    inventory = _keyed_orders_inventory()
    candidate = _candidate()
    candidate["runtime_bindings"] = [_named_record_binding("facts:state:orders", "value")]
    packet = _render_correction_packet(
        *_context(
            candidate,
            inventory,
            _runtime_contract(),
            [
                Finding(
                    "plan_binding_validation",
                    "binding type mismatch for target_order_id: expected string, source is object",
                    "runtime_bindings[0].selector",
                )
            ],
        )
    )

    option = _option(packet)
    assert option["kind"] == "selector"
    assert "named_record_key" not in option
    assert "named_record_sources" not in option


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
        *_context(
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
        *_context(
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
        *_context(
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
        *_context(
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
        *_context(
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
        *_context(
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
        *_context(
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


def test_prerequisite_options_skip_findings_that_name_no_prerequisite_object() -> None:
    prerequisites = [
        {"name": "record_ready", "binding": "existing"},
        "not an object",
    ]
    findings = [
        Finding("consumer_mismatch", "missing", "prerequisites[1].binding"),
        Finding("consumer_mismatch", "missing", "prerequisites[7].binding"),
        Finding("consumer_mismatch", "missing", "prerequisites[0].binding"),
        Finding("consumer_mismatch", "missing", "prerequisites[0].name"),
        Finding("other_code", "missing", "prerequisites[0].binding"),
    ]

    options = _prerequisite_binding_repair_options(findings, prerequisites, _candidate(), {})

    assert [option["required_consumer"] for option in options] == ["prerequisites.existing"]


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
        *_context(
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
        *_context(
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


def test_static_repair_fields_are_case_independent_and_separate_from_options() -> None:
    first_packet = _unknown_binding_packet("first_binding")
    second_packet = _unknown_binding_packet("second_binding")

    first_fields = _section(first_packet, "BINDING REPAIR OPTION FIELDS", "BINDING REPAIR OPTIONS")
    second_fields = _section(
        second_packet, "BINDING REPAIR OPTION FIELDS", "BINDING REPAIR OPTIONS"
    )
    assert first_fields == second_fields
    assert "first_binding" not in first_fields
    assert "second_binding" not in second_fields

    options = _section(first_packet, "BINDING REPAIR OPTIONS", "CORRECTION INSTRUCTIONS")
    assert '"description"' not in options
    assert '"field_descriptions"' not in options


def test_binding_selector_type_resolves_nested_schema_paths() -> None:
    schema = {
        "type": "object",
        "properties": {
            "order": {
                "type": "object",
                "properties": {"amount": {"type": "number"}},
            }
        },
    }

    assert _binding_selector_type(schema, "value.order.amount") == "number"
    assert _binding_selector_type(schema, "value.order.missing") is None


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
        *_context(candidate, _inventory(), _runtime_contract(), [finding, finding])
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


_UNRESOLVED_BINDING = {
    "name": "record_value",
    "expected_type": "string",
    "source_kind": "supplied_input",
    "source_ref": "facts:not-present",
}


def test_selector_option_is_none_when_the_source_does_not_resolve() -> None:
    finding = Finding("plan_binding_validation", "bad selector", "runtime_bindings[0].selector")

    option = _repair_selector_option(
        finding, _UNRESOLVED_BINDING, _inventory(), _runtime_contract()
    )

    assert option is None


def test_review_binding_option_is_none_when_the_source_does_not_resolve() -> None:
    finding = Finding("semantic_review", "wrong selector", "runtime_bindings[0]")

    option = _repair_review_binding_option(
        finding, 0, _UNRESOLVED_BINDING, _inventory(), _runtime_contract()
    )

    assert option is None


def _unknown_binding_option(declared: int, evidence: int) -> dict:
    inventory = {
        "facts": [
            {
                "ref": f"fact:{index:02d}",
                "value": f"value-{index}",
                "schema": {"type": "string"},
                "provenance": "test",
            }
            for index in range(evidence)
        ],
        "operations": [],
    }
    candidate = _candidate()
    candidate["runtime_bindings"] = [
        {
            "name": f"binding_{index:02d}",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:fact:00",
            "selector": "value",
            "consumers": ["stimulus.user_text"],
            "on_missing": "stop",
        }
        for index in range(declared)
    ]
    candidate["prerequisites"] = [
        {
            "name": "record_ready",
            "check": "The record is ready.",
            "evidence_refs": [f"fact:{index:02d}" for index in range(evidence)],
            "binding": "missing_binding",
            "equals": "ready",
        }
    ]
    packet = _render_correction_packet(
        *_context(
            candidate,
            inventory,
            _runtime_contract(),
            [Finding("unknown_binding", "binding is not declared", "prerequisites[0].binding")],
        )
    )
    return _option(packet)


def test_unknown_binding_caps_declared_names_and_fact_sources_with_a_note() -> None:
    option = _unknown_binding_option(declared=41, evidence=41)

    assert len(option["declared_binding_names"]) == 40
    assert option["declared_binding_names_truncated"] is True
    assert len(option["evidence_ref_sources"]) == 40
    assert option["evidence_ref_sources_truncated"] is True
    assert "binding name and supplied fact source" in option["truncation_note"]


def test_unknown_binding_note_names_only_the_truncated_list() -> None:
    option = _unknown_binding_option(declared=41, evidence=1)

    assert option["declared_binding_names_truncated"] is True
    assert option["evidence_ref_sources_truncated"] is False
    assert "binding name" in option["truncation_note"]
    assert "supplied fact source" not in option["truncation_note"]


def test_unknown_binding_without_truncation_has_no_note() -> None:
    option = _unknown_binding_option(declared=1, evidence=1)

    assert option["declared_binding_names"] == ["binding_00"]
    assert "truncation_note" not in option


def test_source_repair_lists_a_repeated_setup_operation_once() -> None:
    operation = {
        "name": "lookup_record",
        "result_schema": {"type": "string"},
        "description": "test operation",
    }
    inventory = {"facts": [], "operations": [operation, copy.deepcopy(operation)]}
    candidate = _candidate()
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
    packet = _render_correction_packet(
        *_context(
            candidate,
            inventory,
            {"setup_permissions": ["lookup_record"]},
            [Finding("plan_binding_validation", "bad source", "runtime_bindings[0].source_ref")],
        )
    )

    option = _option(packet)
    assert [source["source_ref"] for source in option["permitted_setup_sources"]] == [
        "setup:lookup_record"
    ]
    assert option["permitted_setup_sources_truncated"] is False
    assert "truncation_note" not in option


def _facts(*facts: object) -> dict:
    return {"facts": list(facts)}


@pytest.mark.parametrize(
    ("source_kind", "source_ref", "inventory", "shape"),
    [
        ("supplied_input", "facts:f", _facts({"ref": "f", "value": []}), "list"),
        ("supplied_input", "facts:f", _facts({"ref": "f", "value": {}}), "object"),
        ("supplied_input", "facts:f", _facts({"ref": "f", "value": [1]}), None),
        ("supplied_input", "facts:f", _facts({"ref": "f", "value": ""}), None),
        ("supplied_input", "facts:f", _facts({"ref": "f"}), None),
        ("supplied_input", "facts:f", _facts("not a dict", {"ref": "g", "value": []}), None),
        ("supplied_input", 3, _facts({"ref": "f", "value": []}), None),
        ("setup_result", "setup:f", _facts({"ref": "f", "value": []}), None),
    ],
)
def test_supplied_value_empty_fields_name_only_empty_lists_and_objects(
    source_kind: str, source_ref: object, inventory: dict, shape: str | None
) -> None:
    fields = _supplied_value_empty_fields(source_kind, source_ref, inventory)

    if shape is None:
        assert fields == {}
    else:
        assert fields["supplied_value_empty"] is True
        assert f"is an empty {shape};" in fields["supplied_value_empty_note"]


@pytest.mark.parametrize(
    ("details", "bindings", "expected"),
    [
        (None, [{"name": "a"}], []),
        ({"review_location": "plan.runtime_bindings[1].selector"}, [{}, {}], [1]),
        ({"review_location": "runtime_bindings[2]"}, [{}, {}], []),
        (
            {"review_location": 3, "review_required_change": "bind order_id, not order_ids"},
            ["text", {"name": ""}, {"name": 4}, {"name": "order_id"}, {"name": "order"}],
            [3],
        ),
        ({"review_location": "uses order_id"}, [{"name": "order_id"}, {"name": "x"}], [0]),
    ],
)
def test_review_binding_indices_follow_the_pointer_or_exact_identifiers(
    details: object, bindings: list, expected: list[int]
) -> None:
    finding = Finding("semantic_review", "detail", "plan", details)

    assert _review_binding_indices(finding, bindings) == expected
    assert _review_binding_indices({"details": details}, bindings) == expected


@pytest.mark.parametrize(
    ("permissions", "expected_setup"),
    [(["lookup"], [("setup:lookup", {"type": "object"})]), ("lookup", []), ([], [])],
)
def test_review_documented_sources_list_facts_and_permitted_setup_results(
    permissions: object, expected_setup: list
) -> None:
    inventory = {
        "facts": [
            {"ref": "f", "schema": {"type": "array"}},
            {"ref": "", "schema": {}},
            {"ref": "g", "schema": "not a dict"},
            "not a dict",
        ],
        "operations": [
            {"name": "lookup", "result_schema": {"type": "object"}},
            {"name": "other", "result_schema": {}},
            {"name": "lookup2", "result_schema": "not a dict"},
            {"result_schema": {}},
        ],
    }

    sources = _review_documented_sources(inventory, {"setup_permissions": permissions})

    assert sources == [("facts:f", {"type": "array"}), *expected_setup]


_ITEMS_SCHEMA = {
    "type": "object",
    "properties": {"rows": {"type": "array", "items": {"type": "string"}}},
}


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("value", "object"),
        ("value.rows", "array"),
        ("value.rows.items", "string"),
        ("value.rows.first", None),
        ("value.rows.items.deeper", None),
        ("value.", None),
        ("", None),
    ],
)
def test_binding_selector_type_follows_properties_and_array_items(
    selector: str, expected: str | None
) -> None:
    assert _binding_selector_type(_ITEMS_SCHEMA, selector) == expected


def test_binding_selector_type_stops_at_an_undocumented_schema() -> None:
    assert _binding_selector_type({"type": "string"}, "value.field") is None
    assert _binding_selector_type({"type": "object", "properties": []}, "value.field") is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("runtime_bindings[2].selector", (2, "selector")),
        ("prerequisites[0].binding", None),
        ("runtime_bindings[2].selector:slot", None),
        ("runtime_bindings[02].selector", None),
        ("runtime_bindings[2].selector.x", None),
        ("runtime_bindings.selector", None),
    ],
)
def test_indexed_field_reads_the_finding_target(path: str, expected: object) -> None:
    from asago_artifact_generator.authoring.binding_repair import _indexed_field

    assert _indexed_field(Finding("c", "d", path), "runtime_bindings") == expected
    assert _indexed_field({"code": "c", "path": path}, "runtime_bindings") == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("interpretation.source_refs[0]", "interpretation.source_refs"),
        ("selected_evidence[3]", "selected_evidence[].ref"),
        ("selected_evidence[3].ref", "selected_evidence[].ref"),
        ("assumptions[1].ref", "assumptions[].ref"),
        ("prerequisites[2].evidence_refs[0]", "prerequisites[].evidence_refs"),
        ("prerequisites[2].evidence_refs", None),
        ("selected_evidence[3].ref:q", None),
        ("interpretation.source_refs[x]", None),
        (None, None),
    ],
)
def test_reference_field_matches_target_shapes(path: object, expected: str | None) -> None:
    from asago_artifact_generator.authoring.binding_repair import _reference_field

    assert _reference_field(path) == expected
