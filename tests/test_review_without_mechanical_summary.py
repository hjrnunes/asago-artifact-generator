"""A reviewer prompt has no mechanical-check summary; the plan reviewer keeps selector forms."""

from __future__ import annotations

from pathlib import Path

import pytest

from asago_artifact_generator.authoring.core import PromptPacket
from asago_artifact_generator.authoring.review import (
    _PLAN_SELECTOR_FORMS,
    build_artifact_review_packet,
    build_artifact_reviewer_context,
    build_plan_review_packet,
    build_plan_reviewer_context,
)

from .support import world
from .turn_support import (
    indirect_inventory,
    indirect_metadata,
    indirect_plan,
    indirect_runtime,
    indirect_view,
    sequential_metadata,
    sequential_plan,
    sequential_runtime,
    v4_view,
)

_SECTION = "BINDING AND SETUP RULES"


def _ehr_packets() -> tuple[PromptPacket, PromptPacket]:
    ehr = world("ehr")
    args = (ehr["view"], ehr["plan"])
    plan = build_plan_review_packet(*args, ehr["inventory"], ehr["runtime_contract"])
    artifact = build_artifact_review_packet(
        ehr["view"],
        ehr["plan"],
        ehr["metadata"],
        ehr["inventory"],
        ehr["runtime_contract"],
    )
    return plan, artifact


def _sequential_packets(tmp_path: Path) -> tuple[PromptPacket, PromptPacket]:
    refund = world("refund")
    view = v4_view(tmp_path, 3)
    runtime = sequential_runtime(refund["runtime_contract"])
    plan = sequential_plan(refund["plan"], 3)
    return (
        build_plan_review_packet(view, plan, refund["inventory"], runtime),
        build_artifact_review_packet(
            view, plan, sequential_metadata(refund["metadata"], 3), refund["inventory"], runtime
        ),
    )


def _indirect_packets(tmp_path: Path) -> tuple[PromptPacket, PromptPacket]:
    refund = world("refund")
    view = indirect_view(tmp_path, 2)
    inventory = indirect_inventory(refund["inventory"])
    runtime = indirect_runtime(refund["runtime_contract"])
    plan = indirect_plan(refund["plan"], 2)
    return (
        build_plan_review_packet(view, plan, inventory, runtime),
        build_artifact_review_packet(
            view, plan, indirect_metadata(refund["metadata"], 2), inventory, runtime
        ),
    )


def _all_packets(tmp_path: Path) -> list[PromptPacket]:
    return [
        *_ehr_packets(),
        *_sequential_packets(tmp_path),
        *_indirect_packets(tmp_path),
    ]


def test_no_reviewer_prompt_or_payload_names_the_mechanical_summary(tmp_path: Path) -> None:
    for packet in _all_packets(tmp_path):
        assert "MECHANICAL" not in packet.system
        assert "MECHANICAL" not in packet.user
        assert "mechanical_check_summary" not in packet.payload
        assert "mechanical_check_summary" not in packet.user


def test_the_reviewer_contexts_have_no_mechanical_summary() -> None:
    ehr = world("ehr")
    plan_context = build_plan_reviewer_context(
        ehr["view"], ehr["plan"], ehr["inventory"], ehr["runtime_contract"]
    )
    artifact_context = build_artifact_reviewer_context(
        ehr["view"], ehr["plan"], ehr["metadata"], ehr["inventory"], ehr["runtime_contract"]
    )

    assert "mechanical_check_summary" not in plan_context
    assert "mechanical_check_summary" not in artifact_context


def test_each_reviewer_reads_every_selector_form_among_the_binding_rules(
    tmp_path: Path,
) -> None:
    packets = _all_packets(tmp_path)

    for packet in packets:
        following = (
            "NEUTRAL OUTCOME EXAMPLE" if packet.stage == "plan_review" else "RUNTIME CAPABILITIES"
        )
        section = packet.user[
            packet.user.index(f"\n{_SECTION}\n") : packet.user.index(f"\n{following}\n")
        ]
        assert all(form in section for form in _PLAN_SELECTOR_FORMS)
        assert packet.payload["binding_and_setup_rules"]["documented_selector_forms"] == list(
            _PLAN_SELECTOR_FORMS
        )


def test_the_plan_reviewer_is_told_that_a_valid_binding_can_still_mean_the_wrong_thing() -> None:
    packet = _ehr_packets()[0]
    section = packet.user[packet.user.index(_SECTION) : packet.user.index("NEUTRAL OUTCOME")]

    assert "wrong record, field, actor, or value" in section
    assert "must cite the conflicting scenario fact" in section


@pytest.mark.parametrize("stage", ["plan_review", "artifact_review"])
def test_each_reviewer_prompt_carries_its_bumped_version(stage: str) -> None:
    plan, artifact = _ehr_packets()

    assert {"plan_review": plan, "artifact_review": artifact}[stage].version.endswith("-v21")
