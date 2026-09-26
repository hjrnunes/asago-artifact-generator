from __future__ import annotations

import copy
import json

from asago_artifact_generator.authoring import (
    CALL1_PROMPT_VERSION_V8,
    CORRECTION_PROMPT_VERSION_V13,
    Finding,
    _render_correction_packet,
    build_artifact_author_context,
    build_call1_packet_v2,
    build_correction_context,
    build_plan_author_context,
    collect_plan_findings_v2,
    scenario_provenance_ids,
)

from .test_versioned_prompt_roles import _inventory, _plan, _runtime_contract, _view


def _section(user: str, title: str) -> dict:
    start = user.index(f"\n{title}\n") + len(title) + 2
    end = user.index("\n\n", start)
    return json.loads(user[start:end])


def test_call1_renders_evidence_references_with_lineage_provenance() -> None:
    packet = build_call1_packet_v2(_view(), _inventory(), _runtime_contract())

    assert packet.version == CALL1_PROMPT_VERSION_V8
    assert packet.user.index("SOURCE CONTEXT") < packet.user.index("EVIDENCE REFERENCES")
    assert packet.user.index("EVIDENCE REFERENCES") < packet.user.index("EXECUTION CAPABILITIES")
    section = _section(packet.user, "EVIDENCE REFERENCES")
    assert section["citable_references"] == {
        "facts": ["draft:status", "reservation:RES-201", "session:actor"],
        "source_handles": ["source:case"],
        "operations": ["operation:summarize_for_ehr"],
    }
    ids = section["provenance_ids"]["ids"]
    assert ids["SC-1"] == "scenario lineage (constraint IDs)"
    assert ids["CA-1-1"] == "scenario lineage (control action ID)"
    assert "interpretation.source_refs" in section["provenance_ids"]["rule"]
    assert "not valid here" in section["field_rules"]["assumptions[].ref"]
    assert any("assistant_messages" in item for item in section["not_references"])


def test_attack_tree_node_ids_are_citable_provenance_with_plain_locations() -> None:
    view = copy.deepcopy(_view())
    view.payload["attack_tree"]["branches"][0]["node_id"] = "AT-REFUND-1"

    provenance = scenario_provenance_ids(view)
    section = _section(
        build_call1_packet_v2(view, _inventory(), _runtime_contract()).user,
        "EVIDENCE REFERENCES",
    )

    assert "AT-REFUND-1" in provenance
    assert section["provenance_ids"]["ids"]["AT-REFUND-1"] == (
        "named by attack-tree node AT-REFUND-1"
    )
    assert isinstance(section["provenance_ids"]["ids"]["AT-REFUND-1"], str)


def test_provenance_locations_are_plain_language_not_reference_tokens() -> None:
    section = build_plan_author_context(_view(), _inventory(), _runtime_contract())[
        "evidence_references"
    ]
    locations = section["provenance_ids"]["ids"]

    assert locations["SC-1"] == "scenario lineage (constraint IDs)"
    assert locations["CA-1-1"] == "scenario lineage (control action ID)"
    assert "attack_tree:" not in json.dumps(locations)
    assert "lineage." not in json.dumps(locations)


def test_call1_schema_describes_every_reference_field() -> None:
    contract = build_plan_author_context(_view(), _inventory(), _runtime_contract())[
        "response_contract"
    ]
    properties = contract["schema"]["properties"]

    interpretation = properties["interpretation"]["properties"]
    assert "lineage ID" in interpretation["source_refs"]["description"]
    assert (
        "Observation scopes"
        in (properties["selected_evidence"]["items"]["properties"]["ref"]["description"])
    )
    assert (
        "Lineage IDs are invalid"
        in (properties["assumptions"]["items"]["properties"]["ref"]["description"])
    )
    assert (
        "Binding names"
        in (properties["prerequisites"]["items"]["properties"]["evidence_refs"]["description"])
    )


def test_legacy_binding_contract_context_has_no_evidence_reference_section() -> None:
    context = build_plan_author_context(
        _view(), _inventory(), _runtime_contract(), legacy_binding_contract=True
    )

    assert "evidence_references" not in context
    assert (
        "description"
        not in (
            context["response_contract"]["schema"]["properties"]["interpretation"]["properties"][
                "source_refs"
            ]
        )
    )


def _codes_at(findings: list[Finding], prefix: str) -> list[str]:
    return [finding.code for finding in findings if finding.path.startswith(prefix)]


def test_provenance_ids_are_valid_only_in_interpretation_source_refs() -> None:
    plan = copy.deepcopy(_plan())
    plan["interpretation"]["source_refs"] = ["source:case", "SC-1", "CA-1-1"]
    plan["assumptions"] = [{"ref": "SC-1", "reason": "lineage is not a supplied fact"}]
    provenance = scenario_provenance_ids(_view())

    accepted = collect_plan_findings_v2(
        plan, _inventory(), _runtime_contract(), provenance_ids=provenance
    )
    strict = collect_plan_findings_v2(plan, _inventory(), _runtime_contract())

    assert _codes_at(accepted, "interpretation.source_refs") == []
    assert _codes_at(accepted, "assumptions[0].ref") == ["unknown_reference"]
    assert _codes_at(strict, "interpretation.source_refs") == [
        "unknown_reference",
        "unknown_reference",
    ]


def test_observation_scopes_stay_invalid_as_selected_evidence() -> None:
    plan = copy.deepcopy(_plan())
    plan["selected_evidence"] = [{"ref": "tool_calls", "role": "capture", "source": "runtime"}]

    findings = collect_plan_findings_v2(
        plan,
        _inventory(),
        _runtime_contract(),
        provenance_ids=scenario_provenance_ids(_view()),
    )

    assert [finding.to_dict() for finding in findings] == [
        {
            "code": "unknown_reference",
            "detail": "unknown_reference: tool_calls",
            "path": "selected_evidence[0]",
        }
    ]


def test_plan_correction_explains_each_unknown_reference() -> None:
    candidate = copy.deepcopy(_plan())
    candidate["assumptions"] = [{"ref": "SC-1", "reason": "constraint"}]
    candidate["selected_evidence"].append(
        {"ref": "assistant_messages", "role": "reply", "source": "runtime"}
    )
    candidate["interpretation"]["source_refs"] = ["scenario:narrative"]
    findings = collect_plan_findings_v2(
        candidate,
        _inventory(),
        _runtime_contract(),
        provenance_ids=scenario_provenance_ids(_view()),
    )
    packet = _render_correction_packet(
        build_correction_context(
            failed_stage="call1",
            original_context=build_plan_author_context(_view(), _inventory(), _runtime_contract()),
            current_output=json.dumps(candidate),
            findings=findings,
        )
    )

    assert packet.version == CORRECTION_PROMPT_VERSION_V13
    options = {
        item["path"]: item for item in packet.payload["reference_repair_options"]["options"]
    }
    assert {path: item["rejected_value_kind"] for path, item in options.items()} == {
        "selected_evidence[1]": "observation_scope",
        "interpretation.source_refs[0]": "unlisted",
        "assumptions[0].ref": "provenance_id",
    }
    assert options["assumptions[0].ref"]["field_rule"].endswith(
        "cite it in interpretation.source_refs instead."
    )
    assert "REFERENCE REPAIR OPTIONS" in packet.user
    assert packet.user.index("CURRENT FINDINGS") < packet.user.index("REFERENCE REPAIR OPTIONS")
    assert '"provenance_ids"' in packet.user


def test_artifact_correction_explains_provenance_ids_outside_source_refs() -> None:
    view = copy.deepcopy(_view())
    view.payload["attack_tree"]["branches"][0]["node_id"] = "AT-REFUND-1"
    context = build_artifact_author_context(view, _plan(), _inventory(), _runtime_contract())
    correction = _render_correction_packet(
        build_correction_context(
            failed_stage="call2",
            original_context=context,
            current_output="candidate",
            findings=[
                {
                    "code": "unknown_reference",
                    "detail": "unknown_reference: AT-REFUND-1",
                    "path": "assumptions[0].ref",
                }
            ],
        )
    )

    option = correction.payload["reference_repair_options"]["options"][0]
    assert option["rejected_value_kind"] == "provenance_id"
    assert "attack-tree node ID" in option["repair"]
    assert "interpretation.source_refs" in option["repair"]
