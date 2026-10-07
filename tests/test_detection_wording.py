"""Authoring prompts describe the detection that downstream actually runs.

A command_attempt claim is decided by the producer's tool-call condition, which
holds its supplied values and reads no runtime binding. A reply claim is decided
by the semantic judge, which receives every resolved runtime binding. The
prompts no longer describe detector code that reads ``evidence.bindings``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .prompt_support import render_stage_packets
from .support import signed_omission_view, world_builders

(_view,) = world_builders("ehr", "view")

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


@pytest.fixture(params=["without_condition", "with_condition"])
def packets(request: pytest.FixtureRequest, tmp_path: Path) -> dict:
    view = _view() if request.param == "without_condition" else signed_omission_view(tmp_path)
    return render_stage_packets(view=view)


def test_no_prompt_describes_the_removed_detector_mechanism(packets: dict) -> None:
    for stage, packet in packets.items():
        text = packet.system + packet.user
        assert [phrase for phrase in _REMOVED_MECHANISM if phrase in text] == [], stage
