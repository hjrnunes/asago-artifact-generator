"""An omission plan cites no stimulus trigger, and its author and reviewer are told so."""

from __future__ import annotations

from pathlib import Path

import pytest

from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
    build_call2_packet_v2,
)
from asago_artifact_generator.authoring.review import (
    build_artifact_review_packet,
    build_plan_review_packet,
)
from asago_artifact_generator.input_adapter import load_input

from .support import (
    NO_CONDITION_HANDOFF,
    NOT_CALLED_HANDOFF,
    OBSERVED_HANDOFF,
    world_builders,
)

_inventory, _metadata, _plan, _runtime_contract = world_builders(
    "ehr", "inventory", "metadata", "plan", "runtime_contract"
)

_AUTHOR_SENTENCE = "selected_evidence cites no entry for it"
_REVIEWER_SENTENCE = "An omission plan cites no stimulus trigger"


def _packets(source: Path) -> dict:
    view = load_input(source)
    inventory, runtime, plan = _inventory(), _runtime_contract(), _plan()
    return {
        "call1": build_call1_packet_v2(view, inventory, runtime),
        "plan_review": build_plan_review_packet(view, plan, inventory, runtime),
        "call2": build_call2_packet_v2(view, plan, inventory, runtime),
        "artifact_review": build_artifact_review_packet(
            view, plan, _metadata(), inventory, runtime
        ),
    }


def test_the_plan_author_is_told_an_omission_cites_no_stimulus_trigger() -> None:
    packets = _packets(NOT_CALLED_HANDOFF)

    assert packets["call1"].user.count(_AUTHOR_SENTENCE) == 1
    assert "stimulus is not a supplied observation" in packets["call1"].user
    assert _REVIEWER_SENTENCE not in packets["call1"].user


def test_the_plan_reviewer_is_told_not_to_demand_a_stimulus_trigger() -> None:
    packet = _packets(NOT_CALLED_HANDOFF)["plan_review"]

    assert packet.user.count(_REVIEWER_SENTENCE) == 1
    assert "Do not ask the plan to cite the stimulus as a trigger" in packet.user
    assert packet.payload["binding_and_setup_rules"]["omission_trigger"].startswith(
        _REVIEWER_SENTENCE
    )
    assert _AUTHOR_SENTENCE not in packet.user


def test_only_the_plan_stage_prompts_carry_the_instruction() -> None:
    packets = _packets(NOT_CALLED_HANDOFF)

    for name in ("call2", "artifact_review"):
        assert _AUTHOR_SENTENCE not in packets[name].user, name
        assert _REVIEWER_SENTENCE not in packets[name].user, name


@pytest.mark.parametrize("source", [OBSERVED_HANDOFF, NO_CONDITION_HANDOFF])
def test_a_plan_without_a_not_called_comparison_gets_no_omission_trigger_text(
    source: Path,
) -> None:
    for name, packet in _packets(source).items():
        assert _AUTHOR_SENTENCE not in packet.user, name
        assert _REVIEWER_SENTENCE not in packet.user, name
        assert "omission_trigger" not in packet.user, name
