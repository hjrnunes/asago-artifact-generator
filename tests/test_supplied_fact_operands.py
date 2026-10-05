"""A supplied fact operand holds before the run; reviewers must not demand a lookup."""

from __future__ import annotations

from pathlib import Path

from asago_artifact_generator.authoring.contracts import PLAN_FIELD_MEANINGS
from asago_artifact_generator.authoring.prompt_packets import build_call1_packet_v2
from asago_artifact_generator.authoring.review import (
    _PLAN_REVIEW_QUESTIONS,
    build_plan_review_packet,
)
from asago_artifact_generator.input_adapter import load_input

from .test_run_created_records import _runtime

_HANDOFF = (
    Path(__file__).resolve().parents[1]
    / "contracts/scenario-handoff/handoff-v3/valid/functional-not-called.json"
)
_RULE = "established by its supplied_input binding"


def _plan() -> dict:
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
            "request": "r",
            "delivery": "direct_user_message",
            "history": [],
        },
        "observation_claim": {
            "violation": "v",
            "absence": "a",
            "inconclusive": "i",
            "claim_level": "command_attempt",
        },
        "required_observations": {"tool_calls": {"required": True}},
        "semantic_judge": {"needed": False, "scope": None},
        "unresolved_requirements": [],
    }


def test_observability_question_accepts_supplied_fact_operands() -> None:
    question = next(item for item in _PLAN_REVIEW_QUESTIONS if item["id"] == "observability")

    assert _RULE in question["question"]
    assert "earlier captured call" in question["question"]


def test_field_meanings_distinguish_pre_run_facts_from_captured_evidence() -> None:
    assert _RULE in PLAN_FIELD_MEANINGS


def test_author_and_reviewer_prompts_state_the_fact_operand_rule() -> None:
    view = load_input(_HANDOFF)
    inventory = {"facts": [], "operations": [], "source_handles": []}
    call1 = build_call1_packet_v2(view, inventory, _runtime())
    review = build_plan_review_packet(view, _plan(), inventory, _runtime())

    for packet in (call1, review):
        assert "needs no captured lookup of it" in packet.user, packet.stage
        assert _RULE in packet.user, packet.stage
