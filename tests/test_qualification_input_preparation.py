from __future__ import annotations

import hashlib
import json
from pathlib import Path

from asago_artifact_generator.authoring import (
    build_plan_author_context,
    prepare_saved_plan_continuation_v2,
)
from asago_artifact_generator.qualification_inputs import (
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
    assert context["field_guide"]["neutral_binding_example"]["runtime_bindings"][0][
        "source_ref"
    ] == "setup:lookup_order"
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
        finding.code == "provenance_mismatch"
        and finding.path == "provenance.authoring_input_pins"
        for finding in continuation.decision.findings
    )
