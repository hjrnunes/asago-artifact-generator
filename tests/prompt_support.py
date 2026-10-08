"""Render every authoring stage's prompt from an example world.

The golden-file test and the detection-wording tests share one render so that they
describe the same six prompts.
"""

from __future__ import annotations

from typing import Any

from asago_artifact_generator.authoring.binding_repair import correction_repair_inputs
from asago_artifact_generator.authoring.core import PromptPacket
from asago_artifact_generator.authoring.correction import (
    _render_correction_packet,
    build_correction_context,
)
from asago_artifact_generator.authoring.prompt_context import (
    build_artifact_author_context,
    build_plan_author_context,
)
from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
    build_call2_packet_v2,
)
from asago_artifact_generator.authoring.review import (
    build_artifact_review_packet,
    build_plan_review_packet,
)

from .support import world_builders

STAGES = (
    "call1",
    "plan_review",
    "call2",
    "artifact_review",
    "plan_correction",
    "artifact_correction",
)


def render_stage_packets(world: str = "ehr", view: Any = None) -> dict[str, PromptPacket]:
    """Return the six stage prompts rendered from ``world``.

    ``view`` replaces the world's own scenario view when a test needs another handoff.
    """

    world_view, build_inventory, build_runtime, build_plan, build_metadata, build_framed = (
        world_builders(
            world, "view", "inventory", "runtime_contract", "plan", "metadata", "framed"
        )
    )
    view = world_view() if view is None else view
    inventory, runtime, plan = build_inventory(), build_runtime(), build_plan()
    return {
        "call1": build_call1_packet_v2(view, inventory, runtime),
        "plan_review": build_plan_review_packet(view, plan, inventory, runtime),
        "call2": build_call2_packet_v2(view, plan, inventory, runtime),
        "artifact_review": build_artifact_review_packet(
            view, plan, build_metadata(), inventory, runtime
        ),
        "plan_correction": _render_correction_packet(
            build_correction_context(
                failed_stage="call1",
                original_context=build_plan_author_context(view, inventory, runtime),
                current_output=b"{}",
                findings=[],
            ),
            correction_repair_inputs(view, inventory, runtime),
        ),
        "artifact_correction": _render_correction_packet(
            build_correction_context(
                failed_stage="call2",
                original_context=build_artifact_author_context(view, plan, inventory, runtime),
                current_output=build_framed(),
                findings=[],
            ),
            correction_repair_inputs(view, inventory, runtime),
        ),
    }
