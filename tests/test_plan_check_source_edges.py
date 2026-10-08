"""Plan-check edges for trigger selection and setup binding sources."""

from __future__ import annotations

import pytest

from asago_artifact_generator.authoring.checks import collect_plan_findings_v2
from asago_artifact_generator.authoring.plan_triggers import uncited_trigger_observations

from .test_omission_plan_checks import _OMISSION, _TARGET_REF, _full_plan, _inventory


def test_a_selected_item_naming_its_operation_without_a_ref_is_a_trigger() -> None:
    plan = _full_plan([_TARGET_REF, {"operation": "check_widget", "role": "trigger"}])

    assert uncited_trigger_observations(plan, _inventory(), _OMISSION) == {
        "check_widget": ["observation:check_widget:0"]
    }


def test_an_unsupplied_observation_ref_selects_no_trigger() -> None:
    plan = _full_plan([_TARGET_REF, {"ref": "observation:check_widget:9", "role": "trigger"}])

    assert uncited_trigger_observations(plan, _inventory(), _OMISSION) == {}


def test_a_fact_without_a_recording_tool_is_not_a_trigger_observation() -> None:
    inventory = _inventory()
    inventory["facts"][0]["provenance"] = {"status": "ok"}
    plan = _full_plan([_TARGET_REF, {"operation": "check_widget", "role": "trigger"}])

    assert uncited_trigger_observations(plan, inventory, _OMISSION) == {}


@pytest.mark.parametrize(
    ("permissions", "source_findings"),
    [([], ["setup operation is not permitted: check_widget"]), (["check_widget"], [])],
    ids=["not-permitted", "permitted"],
)
def test_a_setup_output_binding_reads_a_permitted_setup_operation(
    permissions: list[str], source_findings: list[str]
) -> None:
    inventory = {**_inventory(), "source_handles": []}
    inventory["operations"][1]["result_schema"] = {
        "type": "object",
        "properties": {"status": {"type": "string"}},
    }
    plan = _full_plan([_TARGET_REF])
    plan["runtime_bindings"] = [
        {
            "name": "widget_status",
            "expected_type": "string",
            "source_kind": "setup_output",
            "source_ref": "setup:check_widget",
            "selector": "result.status",
            "consumers": ["judge.widget_status"],
            "on_missing": "inconclusive",
        }
    ]
    runtime = {"delivery": ["direct_user_message"], "setup_permissions": permissions}

    findings = collect_plan_findings_v2(plan, inventory, runtime)

    assert [
        finding.detail for finding in findings if finding.path == "runtime_bindings[0].source_ref"
    ] == source_findings
