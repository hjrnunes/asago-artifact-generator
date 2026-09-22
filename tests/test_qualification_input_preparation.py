from __future__ import annotations

import hashlib
import json
from pathlib import Path

from asago_artifact_generator.authoring import (
    build_plan_author_context,
    collect_plan_findings_v2,
    prepare_saved_plan_continuation_v2,
)
from asago_artifact_generator.qualification_inputs import (
    prepare_o03_authoring_inputs,
    prepare_o04_authoring_inputs,
    prepare_scn030_authoring_inputs,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _mapping_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def test_o04_preparation_uses_approved_education_authority_without_o03_draft_context() -> None:
    prepared = prepare_o04_authoring_inputs()

    assert prepared.input_view.scenario_id == "O04"
    assert prepared.runtime_contract["setup_permissions"] == []
    assert prepared.inventory["schema_version"] == "miniocciai-o04-inventory-v1"
    assert any(
        fact["ref"] == "state:education:EDU-CATARACT"
        and fact["value"]["approved"] is True
        and "A cataract clinic visit reviews your eyes" in fact["value"]["script"]
        for fact in prepared.inventory["facts"]
    )
    assert any(
        fact["ref"] == "state:referrals:REF-104:education_template_id"
        and fact["value"] == "EDU-CATARACT"
        for fact in prepared.inventory["facts"]
    )
    rendered = json.dumps(prepared.inventory, sort_keys=True)
    assert "prepared_draft_status" not in rendered
    assert "review_rule" not in rendered
    assert "summarize_for_ehr" not in rendered
    context = build_plan_author_context(
        prepared.input_view,
        prepared.inventory,
        prepared.runtime_contract,
    )
    assert "summarize_for_ehr" not in json.dumps(context)
    assert prepared.authoring_input_pins["schema_version"] == "authoring-input-pins-v1"
    assert prepared.authoring_input_pins["authority_digests"]["seed_state"]


def test_scn030_preparation_preserves_handoff_pins_and_typed_lookup_result() -> None:
    prepared = prepare_scn030_authoring_inputs()
    selection_path = (
        Path(__file__).resolve().parents[1]
        / "runs"
        / "authoring"
        / "saved-producer-handoff-20260919"
        / "selection.json"
    )
    selection = json.loads(selection_path.read_text(encoding="utf-8"))

    assert prepared.input_view.scenario_id == "SCN-030"
    assert prepared.handoff_pins == selection["handoff"]
    lookup = next(
        operation
        for operation in prepared.inventory["operations"]
        if operation["name"] == "lookup_order"
    )
    assert lookup["result_schema"]["properties"]["refund_eligible"] == {"type": "boolean"}
    assert prepared.runtime_contract["setup_permissions"] == ["lookup_order"]
    assert prepared.inventory["operation_results"][0]["operation"] == "lookup_order"
    assert prepared.inventory["operation_results"][0]["result"]["refund_eligible"] is False
    assert prepared.inventory["operation_results"][0]["result"]["order_id"] == "ORD-104"
    assert prepared.authoring_input_pins["authority_digests"]["gold_cases"]
    assert prepared.authoring_input_pins["handoff_pins"] == prepared.handoff_pins
    context = build_plan_author_context(
        prepared.input_view,
        prepared.inventory,
        prepared.runtime_contract,
    )
    assert context["execution_capabilities"]["setup_permissions"] == ["lookup_order"]
    assert (
        context["field_guide"]["neutral_binding_example"]["runtime_bindings"][0]["source_ref"]
        == "setup:lookup_order"
    )
    assert "setup:process_refund" not in json.dumps(context)


def test_neutral_status_example_is_generic_when_no_case_setup_is_permitted() -> None:
    prepared = prepare_o04_authoring_inputs()

    context = build_plan_author_context(
        prepared.input_view,
        prepared.inventory,
        prepared.runtime_contract,
    )
    example = context["field_guide"]["neutral_binding_example"]

    assert example["label"] == "generic illustration; no case operation is implied"
    assert example["runtime_bindings"] == []
    assert example["prerequisites"] == []
    assert "summarize_for_ehr" not in json.dumps(example)


def _o03_binding_example_fragment() -> dict[str, object]:
    return {
        "runtime_bindings": [
            {
                "name": "setup_status",
                "expected_type": "string",
                "source_kind": "setup_output",
                "source_ref": "setup:summarize_for_ehr",
                "selector": "result.status",
                "consumers": ["prerequisites.setup_status"],
                "on_missing": "stop",
            }
        ],
        "prerequisites": [
            {
                "name": "setup_ready",
                "check": "The summarize_for_ehr operation returned a ready result.",
                "evidence_refs": ["operation:summarize_for_ehr"],
                "binding": "setup_status",
                "equals": "READY",
            }
        ],
    }


def _prerequisite_findings(findings: list) -> list[str]:
    # The legacy and canonical validators can both report the same reference
    # defect, so collapse the duplicate code:path pairs before comparing.
    codes = {
        f"{finding.code}:{finding.path}"
        for finding in findings
        if finding.code in {"unknown_reference", "consumer_mismatch", "undeclared_slot"}
    }
    return sorted(codes)


def test_o03_preparation_pins_prepared_draft_source_and_setup_permission() -> None:
    prepared = prepare_o03_authoring_inputs()

    assert prepared.input_view.scenario_id == "O03"
    assert prepared.runtime_contract["setup_permissions"] == ["summarize_for_ehr"]
    assert prepared.runtime_contract["authoring_transports"]["judge"] == "unavailable"
    assert prepared.inventory["schema_version"] == "miniocciai-o03-inventory-v1"
    summarize = next(
        operation
        for operation in prepared.inventory["operations"]
        if operation["name"] == "summarize_for_ehr"
    )
    assert summarize["result_schema"]["properties"]["draft_id"] == {"type": "string"}
    assert summarize["result_schema"]["properties"]["patient_id"] == {"type": "string"}
    assert summarize["result_schema"]["properties"]["status"] == {"type": "string"}
    commit = next(
        operation
        for operation in prepared.inventory["operations"]
        if operation["name"] == "commit_to_ehr"
    )
    assert commit["arguments"]["properties"] == {
        "patient_id": {"type": "string"},
        "draft_id": {"type": "string"},
    }
    # Live draft identity comes from the permitted setup result, never from
    # the offline fixture; the fixture stays inside the pinned source files.
    assert "operation_results" not in prepared.inventory
    assert "DFT-GOLD-001" not in json.dumps(prepared.inventory, sort_keys=True)
    assert prepared.input_view.source_digests["prepared_draft_state"]
    assert prepared.input_view.source_digests["source_evidence"]
    assert sorted(prepared.authoring_input_pins["authority_digests"]) == [
        "gold_cases",
        "prepared_draft_state",
        "source_evidence",
    ]


def test_o03_and_scn030_rendered_guidance_distinguishes_reference_forms() -> None:
    prepared = prepare_o03_authoring_inputs()
    context = build_plan_author_context(
        prepared.input_view,
        prepared.inventory,
        prepared.runtime_contract,
    )
    guide = context["field_guide"]
    example = guide["neutral_binding_example"]

    assert set(guide["reference_forms"]) == {
        "evidence_citation",
        "setup_binding_source",
        "plain_binding_name",
        "closed_consumer",
        "slot",
    }
    assert "{{binding_name}}" in guide["reference_forms"]["slot"]
    assert "operation:<name>" in guide["binding_meanings"]["evidence_refs"]
    assert "setup:<operation>" in guide["binding_meanings"]["source_ref"]
    binding = example["runtime_bindings"][0]
    assert binding["name"] == "setup_status"
    assert binding["source_ref"] == "setup:summarize_for_ehr"
    assert binding["consumers"] == ["prerequisites.setup_status"]
    assert example["prerequisites"][0]["evidence_refs"] == ["operation:summarize_for_ehr"]
    assert example["prerequisites"][0]["binding"] == "setup_status"
    scenario = json.dumps(context["task"]["scenario"], sort_keys=True)
    assert "attempted review bypass" in scenario
    assert "not proof of a committed EHR record" in scenario
    assert "setup:commit_to_ehr" not in json.dumps(context, sort_keys=True)

    scn030 = prepare_scn030_authoring_inputs()
    scn030_context = build_plan_author_context(
        scn030.input_view,
        scn030.inventory,
        scn030.runtime_contract,
    )
    scn030_example = scn030_context["field_guide"]["neutral_binding_example"]
    assert scn030_example["runtime_bindings"][0]["source_ref"] == "setup:lookup_order"
    assert scn030_example["prerequisites"][0]["evidence_refs"] == ["operation:lookup_order"]


def test_corrected_example_forms_pass_the_plan_validator_while_stale_forms_fail() -> None:
    prepared = prepare_o03_authoring_inputs()
    corrected = _o03_binding_example_fragment()
    stale = {
        "runtime_bindings": corrected["runtime_bindings"],
        "prerequisites": [
            {
                **corrected["prerequisites"][0],
                "evidence_refs": ["setup:summarize_for_ehr"],
            }
        ],
    }
    open_consumer = {
        "runtime_bindings": [
            {**corrected["runtime_bindings"][0], "consumers": ["prerequisites.*"]}
        ],
        "prerequisites": corrected["prerequisites"],
    }

    corrected_findings = collect_plan_findings_v2(
        corrected, prepared.inventory, prepared.runtime_contract
    )
    stale_findings = collect_plan_findings_v2(stale, prepared.inventory, prepared.runtime_contract)
    open_findings = collect_plan_findings_v2(
        open_consumer, prepared.inventory, prepared.runtime_contract
    )

    assert _prerequisite_findings(corrected_findings) == []
    assert _prerequisite_findings(stale_findings) == [
        "unknown_reference:prerequisites[0].evidence_refs[0]"
    ]
    assert _prerequisite_findings(open_findings) == ["consumer_mismatch:prerequisites[0].binding"]


def test_saved_plan_with_stale_setup_evidence_citation_is_rejected() -> None:
    prepared = prepare_o03_authoring_inputs()
    plan: dict[str, object] = {
        "runtime_bindings": _o03_binding_example_fragment()["runtime_bindings"],
        "prerequisites": [
            {
                "name": "setup_ready",
                "check": "The summarize_for_ehr operation returned a ready result.",
                "evidence_refs": ["setup:summarize_for_ehr"],
                "binding": "setup_status",
                "equals": "READY",
            }
        ],
    }
    provenance = prepared.provenance(plan)

    continuation = prepare_saved_plan_continuation_v2(
        saved_plan=plan,
        input_view=prepared.input_view,
        inventory=prepared.inventory,
        runtime_contract=prepared.runtime_contract,
        provenance=provenance,
    )

    assert continuation.decision.mode == "fresh_call1"
    assert any(
        finding.code == "unknown_reference"
        and finding.path == "prerequisites[0].evidence_refs[0]"
        and "setup:summarize_for_ehr" in finding.detail
        for finding in continuation.decision.findings
    )


def test_continuation_rejects_stale_refreshed_authoring_input_pins() -> None:
    prepared = prepare_o04_authoring_inputs()
    plan: dict[str, object] = {}
    stale_pins = dict(prepared.authoring_input_pins)
    stale_pins["inventory_sha256"] = "0" * 64
    provenance = {
        "input_sha256": prepared.input_view.source_sha256,
        "inventory_sha256": _mapping_sha256(prepared.inventory),
        "runtime_contract_sha256": _mapping_sha256(prepared.runtime_contract),
        "plan_sha256": _mapping_sha256(plan),
        "meaning_sha256": _mapping_sha256({}),
        "wire_version": "v2",
        "authoring_input_pins": stale_pins,
    }

    continuation = prepare_saved_plan_continuation_v2(
        saved_plan=plan,
        input_view=prepared.input_view,
        inventory=prepared.inventory,
        runtime_contract=prepared.runtime_contract,
        provenance=provenance,
        authoring_input_pins=prepared.authoring_input_pins,
    )

    assert continuation.decision.mode == "fresh_call1"
    assert any(
        finding.code == "provenance_mismatch" and finding.path == "provenance.authoring_input_pins"
        for finding in continuation.decision.findings
    )
