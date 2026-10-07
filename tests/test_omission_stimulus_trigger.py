"""An omission plan cites no stimulus trigger, and the plan reviewer's rules say so.

The prompt wording lives in ``tests/phrases/call1.yaml`` and ``plan_review.yaml`` (case
``not-called``); this test pins the structured rule the wording comes from.
"""

from __future__ import annotations

from asago_artifact_generator.authoring.review import build_plan_review_packet
from asago_artifact_generator.input_adapter import load_input

from .support import NOT_CALLED_HANDOFF, world_builders

_inventory, _plan, _runtime_contract = world_builders(
    "ehr", "inventory", "plan", "runtime_contract"
)


def test_the_plan_reviewer_rules_carry_the_omission_trigger_sentence() -> None:
    packet = build_plan_review_packet(
        load_input(NOT_CALLED_HANDOFF), _plan(), _inventory(), _runtime_contract()
    )

    rules = packet.payload["binding_and_setup_rules"]
    assert rules["omission_trigger"].startswith("An omission plan cites no stimulus trigger")
