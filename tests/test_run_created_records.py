"""A record the target creates during the run is captured evidence, not a blocker."""

from __future__ import annotations

from pathlib import Path

from asago_artifact_generator.authoring.prompt_context import _CURRENT_PLAN_AUTHOR_GUIDANCE
from asago_artifact_generator.authoring.prompt_packets import build_call1_packet_v2
from asago_artifact_generator.input_adapter import load_input

_HANDOFF = (
    Path(__file__).resolve().parents[1]
    / "contracts/scenario-handoff/handoff-v2/valid/functional-not-called.json"
)
_RULE = "a record the target creates during the run"


def _runtime() -> dict:
    return {
        "delivery": ["direct_user_message"],
        "setup_permissions": [],
        "observation": {"tool_calls": {"availability": "captured_or_unavailable"}},
        "limits": {"max_turns": 2},
    }


def test_plan_author_guidance_routes_run_created_records_through_capture() -> None:
    assert _RULE in _CURRENT_PLAN_AUTHOR_GUIDANCE
    assert "not an unresolved requirement" in _CURRENT_PLAN_AUTHOR_GUIDANCE
    assert "empty setup_permissions list permits no setup" in _CURRENT_PLAN_AUTHOR_GUIDANCE


def test_current_plan_prompt_carries_the_rule() -> None:
    view = load_input(_HANDOFF)
    inventory = {"facts": [], "operations": [], "source_handles": []}

    current = build_call1_packet_v2(view, inventory, _runtime())

    assert _RULE in current.user
