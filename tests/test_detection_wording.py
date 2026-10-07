"""Authoring prompts describe the detection that downstream actually runs.

A command_attempt claim is decided by the producer's tool-call condition, which
holds its supplied values and reads no runtime binding. A reply claim is decided
by the semantic judge, which receives every resolved runtime binding. The
prompts no longer describe detector code that reads ``evidence.bindings``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tests.test_owner_scope_block as osb
from tests.test_omission_plan_checks import _signed_omission_view

_REMOVED_MECHANISM = (
    "evidence.bindings",
    "evaluate(evidence)",
    "detector code handles",
    "detector_criteria",
    "A detector input is",
    "when the detector reads the resolved value",
    "hardcode the supplied literal",
    "established by its supplied_input binding",
)
_CONDITION_READS_NO_BINDING = "so it reads no runtime binding"
_JUDGE_RECEIVES_BINDINGS = "receives every resolved runtime binding"
_CONSUMER_RULE = "the producer's tool-call condition for a command_attempt claim reads none"
_FACT_OPERAND_RULE = "the producer resolves it to its supplied value"


@pytest.fixture(params=["without_condition", "with_condition"])
def packets(request: pytest.FixtureRequest, tmp_path: Path) -> dict:
    view = osb._view() if request.param == "without_condition" else _signed_omission_view(tmp_path)
    return osb._render_all_stage_packets(view)


def test_no_prompt_describes_the_removed_detector_mechanism(packets: dict) -> None:
    for stage, packet in packets.items():
        text = packet.system + packet.user
        assert [phrase for phrase in _REMOVED_MECHANISM if phrase in text] == [], stage


def test_plan_author_prompts_state_what_each_detector_reads(packets: dict) -> None:
    for stage in ("call1", "plan_correction"):
        assert _CONDITION_READS_NO_BINDING in packets[stage].user, stage
        assert _JUDGE_RECEIVES_BINDINGS in packets[stage].user, stage


def test_binding_contract_names_the_judge_as_the_only_detector_consumer(packets: dict) -> None:
    for stage in ("call1", "plan_review", "artifact_review", "plan_correction"):
        assert _CONSUMER_RULE in packets[stage].user, stage


def test_condition_rule_resolves_fact_operands_without_a_binding(tmp_path: Path) -> None:
    packets = osb._render_all_stage_packets(_signed_omission_view(tmp_path))

    for stage in ("call1", "plan_review", "artifact_review", "plan_correction"):
        assert _FACT_OPERAND_RULE in packets[stage].user, stage
    assert _FACT_OPERAND_RULE not in packets["call2"].user


def test_every_prompt_role_carries_a_new_version(packets: dict) -> None:
    # The shared plan field meanings changed, so every role's rendered text changed.
    assert {stage: packet.version for stage, packet in packets.items()} == {
        "call1": "authoring-call1-v20",
        "plan_review": "authoring-plan-review-v19",
        "call2": "authoring-call2-v24",
        "artifact_review": "authoring-artifact-review-v20",
        "plan_correction": "authoring-correction-v31",
        "artifact_correction": "authoring-correction-v33",
    }
