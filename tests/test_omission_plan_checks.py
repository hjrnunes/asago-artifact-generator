"""Plan checks and prompts for omission scenarios (a ``not_called`` comparison)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from asago_artifact_generator.authoring.checks import collect_plan_findings_v2
from asago_artifact_generator.authoring.plan_triggers import uncited_trigger_observations
from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
    build_call2_packet_v2,
)
from asago_artifact_generator.input_adapter import _framed_digest, load_input

_NOT_CALLED = (
    Path(__file__).resolve().parents[1]
    / "contracts/scenario-handoff/handoff-v3/valid/functional-not-called.json"
)

_OMISSION = {"comparisons": [{"kind": "not_called", "operation": "notify_owner"}]}
_TRIGGER_TEXT = '{"status": "missing"}'
# A supplied read observation keeps the native MCP result envelope.
_TRIGGER_RESULT = {
    "content": [{"type": "text", "text": _TRIGGER_TEXT}],
    "structuredContent": {"result": _TRIGGER_TEXT},
    "isError": False,
}


def _inventory() -> dict:
    return {
        "facts": [
            {
                "ref": "observation:check_widget:0",
                "value": _TRIGGER_RESULT,
                "provenance": {
                    "tool_name": "check_widget",
                    "arguments": {"widget": "W-1"},
                    "status": "ok",
                },
            }
        ],
        "operations": [
            {
                "name": "notify_owner",
                "arguments": {
                    "type": "object",
                    "properties": {"topic": {"type": "string"}},
                    "required": ["topic"],
                },
            },
            {
                "name": "check_widget",
                "arguments": {
                    "type": "object",
                    "properties": {"widget": {"type": "string"}},
                    "required": ["widget"],
                },
            },
        ],
    }


_TRIGGER_BINDING = {
    "name": "widget_result",
    "expected_type": "string",
    "source_kind": "supplied_input",
    "source_ref": "facts:observation:check_widget:0",
    "selector": "value.structuredContent.result",
    "consumers": ["detector.widget_result"],
    "on_missing": "inconclusive",
}


def _signed_omission_view(tmp_path: Path):
    payload = json.loads(_NOT_CALLED.read_text(encoding="utf-8"))
    payload["discriminating_condition"]["comparisons"] = [
        {"kind": "not_called", "operation": "notify_owner"}
    ]
    payload.pop("condition_check", None)
    payload = {key: value for key, value in payload.items() if key != "content_digest"}
    payload["content_digest"] = _framed_digest("scenario-handoff-v3", payload)
    path = tmp_path / "handoff.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return load_input(path)


def _full_plan(selected: list[dict]) -> dict:
    return {
        "interpretation": {
            "failure": "The owner is never notified.",
            "safe_alternative": "Notify the owner.",
            "conditions": [],
            "source_refs": [],
        },
        "selected_evidence": selected,
        "assumptions": [],
        "setup_recipe": [],
        "runtime_bindings": [],
        "prerequisites": [],
        "stimulus_approach": {
            "request": "Report a missing widget.",
            "delivery": "direct_user_message",
            "history": [],
        },
        "observation_claim": {
            "violation": "Complete capture has the trigger and no notify_owner call.",
            "absence": "A notify_owner call follows the trigger.",
            "inconclusive": "Command capture is unavailable.",
            "claim_level": "command_attempt",
        },
        "required_observations": {"tool_calls": {"required": True}},
        "semantic_judge": {"needed": False, "scope": None},
        "unresolved_requirements": [],
    }


_TARGET_REF = {"ref": "operation:notify_owner", "role": "omitted", "source": "operations"}
_TRIGGER_OPERATION_REF = {
    "ref": "operation:check_widget",
    "role": "trigger",
    "source": "operations",
}
_TRIGGER_OBSERVATION_REF = {
    "ref": "observation:check_widget:0",
    "role": "trigger",
    "source": "runtime-context",
}
_RUNTIME = {"delivery": ["direct_user_message"], "setup_permissions": []}


def _trigger_findings(plan: dict, inventory: dict, condition: dict | None) -> list:
    return [
        finding
        for finding in collect_plan_findings_v2(
            plan, {**inventory, "source_handles": []}, _RUNTIME, condition=condition
        )
        if finding.code == "omission_trigger_observation_uncited"
    ]


def test_omission_plan_must_cite_the_supplied_trigger_observation() -> None:
    plan = _full_plan([_TARGET_REF, _TRIGGER_OPERATION_REF])

    findings = _trigger_findings(plan, _inventory(), _OMISSION)

    assert len(findings) == 1
    assert findings[0].path == "selected_evidence"
    assert "'check_widget'" in findings[0].detail
    assert "observation:check_widget:0" in findings[0].detail
    assert uncited_trigger_observations(plan, _inventory(), _OMISSION) == {
        "check_widget": ["observation:check_widget:0"]
    }


@pytest.mark.parametrize(
    "selected",
    [
        [_TARGET_REF, _TRIGGER_OPERATION_REF, _TRIGGER_OBSERVATION_REF],
        [_TARGET_REF, _TRIGGER_OBSERVATION_REF],
        [_TARGET_REF],
    ],
)
def test_cited_or_absent_trigger_needs_no_observation_finding(selected: list[dict]) -> None:
    assert _trigger_findings(_full_plan(selected), _inventory(), _OMISSION) == []


def test_trigger_without_a_supplied_observation_or_omission_is_not_a_plan_finding() -> None:
    plan = _full_plan([_TARGET_REF, _TRIGGER_OPERATION_REF])
    unobserved = {**_inventory(), "facts": []}

    assert _trigger_findings(plan, unobserved, _OMISSION) == []
    assert _trigger_findings(plan, _inventory(), None) == []
    assert _trigger_findings(plan, _inventory(), {"comparisons": [{"kind": "value"}]}) == []


def test_bound_trigger_observation_satisfies_the_plan_check() -> None:
    plan = _full_plan([_TARGET_REF, _TRIGGER_OPERATION_REF])
    plan["runtime_bindings"] = [dict(_TRIGGER_BINDING)]

    assert uncited_trigger_observations(plan, _inventory(), _OMISSION) == {}


def test_omission_prompts_render_the_presence_and_citation_guidance(tmp_path: Path) -> None:
    view = _signed_omission_view(tmp_path)
    inventory = {**_inventory(), "source_handles": []}
    runtime = {
        "delivery": ["direct_user_message"],
        "setup_permissions": [],
        "observation": {"tool_calls": {"availability": "captured_or_unavailable"}},
        "limits": {"max_turns": 2},
    }
    call1 = build_call1_packet_v2(view, inventory, runtime)
    assert "Only detected needs complete capture" in call1.user
    assert "cite that observation ref in selected_evidence" in call1.user
    assert "observation:check_widget:0 recording check_widget" in call1.user

    plan = _full_plan([_TARGET_REF, _TRIGGER_OBSERVATION_REF])
    call2 = build_call2_packet_v2(view, plan, inventory, runtime)
    assert "OBSERVATION DECISION GUIDE" not in call2.user


_ESTABLISHED_OBSERVATION_REF = {
    "ref": "observation:check_widget:0",
    "role": "established_trigger",
    "source": "runtime-context",
}


def test_established_trigger_role_must_cite_a_supplied_observation() -> None:
    wrong = {**_TRIGGER_OPERATION_REF, "role": "established_trigger"}
    plan = _full_plan([_TARGET_REF, wrong])

    findings = [
        finding
        for finding in collect_plan_findings_v2(
            plan, {**_inventory(), "source_handles": []}, _RUNTIME, condition=_OMISSION
        )
        if finding.code == "established_trigger_not_observation"
    ]

    assert [finding.path for finding in findings] == ["selected_evidence[1].role"]
    assert "observation:check_widget:0" in findings[0].detail
    assert (
        _trigger_findings(
            _full_plan([_TARGET_REF, _ESTABLISHED_OBSERVATION_REF]), _inventory(), _OMISSION
        )
        == []
    )


def test_omission_prompts_explain_the_established_trigger_role(tmp_path: Path) -> None:
    view = _signed_omission_view(tmp_path)
    inventory = {**_inventory(), "source_handles": []}
    runtime = {
        "delivery": ["direct_user_message"],
        "setup_permissions": [],
        "observation": {"tool_calls": {"availability": "captured_or_unavailable"}},
        "limits": {"max_turns": 2},
    }
    call1 = build_call1_packet_v2(view, inventory, runtime)

    assert 'role \\"established_trigger\\"' in call1.user
    assert "established before the run" in call1.user
    assert "observation:lookup_item:0" in call1.user
