"""A stimulus that carries a bracketed placeholder cannot exercise the failure."""

from __future__ import annotations

import pytest

from asago_artifact_generator.authoring.checks import (
    _artifact_stimulus_findings,
    collect_plan_findings_v2,
)
from asago_artifact_generator.authoring.placeholder import placeholder_findings

_RUNTIME = {"delivery": ["direct_user_message"], "setup_permissions": []}
_INVENTORY = {"facts": [], "operations": [], "source_handles": []}


def _plan(request: str, history: list[str] | None = None) -> dict:
    return {
        "interpretation": {
            "failure": "f",
            "safe_alternative": "s",
            "conditions": [],
            "source_refs": [],
        },
        "selected_evidence": [],
        "assumptions": [],
        "setup_recipe": [],
        "runtime_bindings": [],
        "prerequisites": [],
        "stimulus_approach": {
            "request": request,
            "delivery": "direct_user_message",
            "history": history or [],
        },
        "observation_claim": {
            "violation": "v",
            "absence": "a",
            "inconclusive": "i",
            "claim_level": "reply",
        },
        "required_observations": {"assistant_messages": {"required": True}},
        "semantic_judge": {"needed": True, "scope": "The reply."},
        "unresolved_requirements": [],
    }


def _stimulus(user_text: str, history: list[dict] | None = None) -> dict:
    return {
        "user_text": user_text,
        "delivery": "direct_user_message",
        "history": history or [],
        "slots": [],
    }


@pytest.mark.parametrize(
    "text",
    [
        "Help me with this message? [Obscene/Harmful Content Placeholder]",
        "Send [placeholder] to the owner",
        "Use [Account Number PLACEHOLDER here] now",
    ],
)
def test_a_bracketed_placeholder_is_reported(text: str) -> None:
    [finding] = placeholder_findings(text, "stimulus.user_text")

    assert finding.code == "placeholder_stimulus"
    assert finding.path == "stimulus.user_text"
    assert "placeholder" in finding.detail.lower()


@pytest.mark.parametrize(
    "text",
    [
        "Show me reservation RES-201.",
        "Please add a placeholder page to my listing.",
        "Fill the {{reservation_id}} slot",
        "Is [RES-201] mine?",
        "[Obscene] content, placeholder",
        "",
    ],
)
def test_ordinary_text_and_brackets_pass(text: str) -> None:
    assert placeholder_findings(text, "stimulus.user_text") == []


def test_non_text_has_nothing_to_check() -> None:
    assert placeholder_findings(None, "stimulus.user_text") == []
    assert placeholder_findings(["[placeholder]"], "stimulus.user_text") == []


def test_plan_request_and_history_are_checked_where_the_model_can_repair_them() -> None:
    plan = _plan("Ask [Harmful Placeholder]", ["Earlier: [Another placeholder]"])

    findings = [
        finding
        for finding in collect_plan_findings_v2(plan, _INVENTORY, _RUNTIME)
        if finding.code == "placeholder_stimulus"
    ]

    assert [finding.path for finding in findings] == [
        "stimulus_approach.request",
        "stimulus_approach.history[0]",
    ]
    assert {finding.stage for finding in findings} == {"plan"}


def test_a_plan_without_a_placeholder_has_no_such_finding() -> None:
    plan = _plan("Ask about reservation RES-201.")

    codes = [finding.code for finding in collect_plan_findings_v2(plan, _INVENTORY, _RUNTIME)]

    assert "placeholder_stimulus" not in codes


def test_artifact_user_text_and_history_are_checked() -> None:
    plan = _plan("Ask about the reservation.")
    stimulus = _stimulus(
        "Help with [Harmful Content Placeholder]",
        [{"role": "user", "content": "Earlier [placeholder]"}],
    )

    findings = [
        finding
        for finding in _artifact_stimulus_findings(stimulus, plan, _INVENTORY, _RUNTIME, [])
        if finding.code == "placeholder_stimulus"
    ]

    assert [finding.path for finding in findings] == [
        "stimulus.user_text",
        "stimulus.history[0].content",
    ]
