"""Plan checks and prompts for omission scenarios (a ``not_called`` comparison)."""

from __future__ import annotations

from pathlib import Path

import pytest

from asago_artifact_generator.authoring.checks import collect_plan_findings_v2
from asago_artifact_generator.authoring.plan_triggers import uncited_trigger_observations
from asago_artifact_generator.authoring.prompt_context import _gate_condition
from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
    build_call2_packet_v2,
)

from .support import signed_derived_omission_view, signed_omission_view

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
    "consumers": ["judge.widget_result"],
    "on_missing": "inconclusive",
}


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
    view = signed_omission_view(tmp_path)
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
    view = signed_omission_view(tmp_path)
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


# -- an omission plan that states no trigger is refused ---------------------------


def _finding_codes(plan: dict, inventory: dict, condition: dict | None) -> list:
    return [
        finding.code
        for finding in collect_plan_findings_v2(
            plan, {**inventory, "source_handles": []}, _RUNTIME, condition=condition
        )
    ]


def test_an_omission_plan_that_cites_no_trigger_and_names_none_is_refused() -> None:
    """The pilot's stimulus-trigger shapes: only the omitted operation is selected."""

    plan = _full_plan([_TARGET_REF])

    codes = _finding_codes(plan, _inventory(), _OMISSION)

    assert "omission_trigger_missing" in codes
    findings = _omission_findings(plan, _inventory(), _OMISSION)
    assert findings[0].path == "selected_evidence"
    assert "'notify_owner'" in findings[0].detail
    assert "observation:check_widget:0" in findings[0].detail


def test_a_named_trigger_operation_without_a_citation_keeps_its_own_finding() -> None:
    """Naming the trigger operation uncited is the existing finding, not the new one."""

    plan = _full_plan([_TARGET_REF, _TRIGGER_OPERATION_REF])

    codes = _finding_codes(plan, _inventory(), _OMISSION)

    assert "omission_trigger_observation_uncited" in codes
    assert "omission_trigger_missing" not in codes


def test_a_cited_trigger_observation_states_the_trigger() -> None:
    for selected in (
        [_TARGET_REF, _TRIGGER_OPERATION_REF, _TRIGGER_OBSERVATION_REF],
        [_TARGET_REF, _TRIGGER_OBSERVATION_REF],
    ):
        codes = _finding_codes(_full_plan(selected), _inventory(), _OMISSION)
        assert "omission_trigger_missing" not in codes


def test_a_cited_established_trigger_states_the_trigger() -> None:
    codes = _finding_codes(
        _full_plan([_TARGET_REF, _ESTABLISHED_OBSERVATION_REF]), _inventory(), _OMISSION
    )

    assert "omission_trigger_missing" not in codes
    assert "established_trigger_not_observation" not in codes


def test_a_bound_trigger_observation_states_the_trigger() -> None:
    plan = _full_plan([_TARGET_REF, _TRIGGER_OPERATION_REF])
    plan["runtime_bindings"] = [dict(_TRIGGER_BINDING)]

    assert "omission_trigger_missing" not in _finding_codes(plan, _inventory(), _OMISSION)


def test_the_missing_trigger_finding_names_the_gap_when_nothing_is_supplied() -> None:
    plan = _full_plan([_TARGET_REF])
    unobserved = {**_inventory(), "facts": []}

    findings = _omission_findings(plan, unobserved, _OMISSION)

    assert [finding.code for finding in findings] == ["omission_trigger_missing"]
    assert "supplies no observation of another operation" in findings[0].detail


def test_a_plan_without_a_not_called_needs_no_trigger() -> None:
    for condition in (None, {"comparisons": [{"kind": "value"}]}):
        assert "omission_trigger_missing" not in _finding_codes(
            _full_plan([_TARGET_REF]), _inventory(), condition
        )


def _omission_findings(plan: dict, inventory: dict, condition: dict | None) -> list:
    return [
        finding
        for finding in collect_plan_findings_v2(
            plan, {**inventory, "source_handles": []}, _RUNTIME, condition=condition
        )
        if finding.code == "omission_trigger_missing"
    ]


def test_the_author_prompt_states_the_required_citation(tmp_path: Path) -> None:
    view = signed_omission_view(tmp_path)
    inventory = {**_inventory(), "source_handles": []}
    runtime = {
        "delivery": ["direct_user_message"],
        "setup_permissions": [],
        "observation": {"tool_calls": {"availability": "captured_or_unavailable"}},
        "limits": {"max_turns": 2},
    }
    call1 = build_call1_packet_v2(view, inventory, runtime)

    assert (
        "The plan check refuses a plan that states no trigger: when the "
        "condition's own comparisons scope the omission to another "
        "operation" in call1.user
    )
    assert (
        "the condition's own comparisons can scope the omission to another "
        "operation, or selected_evidence can cite that operation's supplied "
        "observation" in call1.user
    )


# -- the gate reads the bound condition when the handoff cites no authored one ------


def test_a_dropped_condition_with_a_bound_bare_not_called_is_refused(tmp_path: Path):
    """The producer's main path: no authored condition, derived bound not_called.

    The producer drops a condition its corrections cannot ground in a
    trigger but still publishes the bound tool-call condition a missing
    call derives, so the gate must read that bound form, not go silent.
    """

    view = signed_derived_omission_view(tmp_path)
    inventory = {**_inventory(), "source_handles": []}
    plan = _full_plan([_TARGET_REF])

    findings = [
        finding
        for finding in collect_plan_findings_v2(
            plan, inventory, _RUNTIME, condition=_gate_condition(view)
        )
        if "omission" in finding.code
    ]

    assert [finding.code for finding in findings] == ["omission_trigger_missing"]
    assert "'notify_owner'" in findings[0].detail


def test_the_omission_guidance_renders_for_a_dropped_condition(tmp_path: Path) -> None:
    """The author is shown the omission guidance on the same path."""

    view = signed_derived_omission_view(tmp_path)
    inventory = {**_inventory(), "source_handles": []}
    runtime = {
        "delivery": ["direct_user_message"],
        "setup_permissions": [],
        "observation": {"tool_calls": {"availability": "captured_or_unavailable"}},
        "limits": {"max_turns": 2},
    }
    call1 = build_call1_packet_v2(view, inventory, runtime)

    assert "Only detected needs complete capture" in call1.user
    assert "The plan check refuses a plan that states no trigger" in call1.user
    assert "observation:check_widget:0 recording check_widget" in call1.user


def test_the_gate_condition_falls_back_to_the_bound_form(tmp_path: Path) -> None:
    """_gate_condition prefers the authored condition and falls back to the bound one."""

    authored_view = signed_omission_view(tmp_path / "authored")
    bound_view = signed_derived_omission_view(tmp_path / "bound")

    authored = _gate_condition(authored_view)
    bound = _gate_condition(bound_view)

    assert authored["comparisons"] == [{"kind": "not_called", "operation": "notify_owner"}]
    assert (
        bound
        == bound_view.tool_call_condition
        == {"comparisons": [{"kind": "not_called", "operation": "notify_owner", "where": []}]}
    )


# -- M2: the condition's own comparisons can state the trigger ----------------------


def test_a_condition_scoped_by_its_own_comparisons_needs_no_plan_evidence() -> None:
    """A condition whose comparisons scope the omission states its own trigger."""

    condition = {
        "comparisons": [
            {
                "kind": "value",
                "left": {
                    "source": "argument",
                    "operation": "check_widget",
                    "argument": "widget",
                },
                "op": "eq",
                "right": {"source": "literal", "value": "W-1"},
            },
            {"kind": "not_called", "operation": "notify_owner"},
        ]
    }
    plan = _full_plan([_TARGET_REF])

    assert _finding_codes(
        plan, _inventory(), condition
    ) == [] or "omission_trigger_missing" not in _finding_codes(plan, _inventory(), condition)


def test_a_bound_condition_scoped_by_a_where_still_needs_plan_evidence() -> None:
    """A where scope on the omitted operation is not a trigger in the plan either."""

    condition = {
        "comparisons": [
            {
                "kind": "not_called",
                "operation": "notify_owner",
                "where": [{"argument": "topic", "value": "missing"}],
            }
        ]
    }
    plan = _full_plan([_TARGET_REF])

    assert "omission_trigger_missing" in _finding_codes(plan, _inventory(), condition)


def test_a_condition_scoped_by_an_order_on_the_trigger_operation_passes() -> None:
    condition = {
        "comparisons": [
            {"kind": "order", "operation": "check_widget", "requires_prior": "get_widget"},
            {"kind": "not_called", "operation": "notify_owner"},
        ]
    }
    plan = _full_plan([_TARGET_REF])

    assert "omission_trigger_missing" not in _finding_codes(plan, _inventory(), condition)
