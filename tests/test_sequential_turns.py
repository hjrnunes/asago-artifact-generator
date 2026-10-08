"""Sequential user turns: checks, model-facing rules, and prompts that stay as they were."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.binding_repair import correction_repair_inputs
from asago_artifact_generator.authoring.checks import (
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
)
from asago_artifact_generator.authoring.core import Finding, PromptPacket
from asago_artifact_generator.authoring.correction import (
    _render_correction_packet,
    build_correction_context,
)
from asago_artifact_generator.authoring.prompt_context import build_plan_author_context
from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
    build_call2_packet_v2,
)
from asago_artifact_generator.authoring.review import (
    build_artifact_review_packet,
    build_plan_review_packet,
)
from asago_artifact_generator.authoring.sequential_turns import (
    SEQUENTIAL_DELIVERY,
    multi_turn_block,
    multi_turn_payload,
    sequential_artifact_findings,
    sequential_plan_findings,
)
from asago_artifact_generator.input_adapter import InputView

from .support import json_section, rendered_response_contract, world
from .turn_support import (
    EARLIER,
    PURPOSES,
    sequential_metadata,
    sequential_plan,
    sequential_runtime,
    v4_view,
)

# --- prompts that must not change -------------------------------------------------


def _packets(view: InputView, refund: dict[str, Any], plan: dict, metadata: dict):
    inventory, rt = refund["inventory"], sequential_runtime(refund["runtime_contract"])
    return {
        "call1": build_call1_packet_v2(view, inventory, rt),
        "call2": build_call2_packet_v2(view, plan, inventory, rt),
        "plan_review": build_plan_review_packet(view, plan, inventory, rt),
        "artifact_review": build_artifact_review_packet(view, plan, metadata, inventory, rt),
    }


def _as_v3(packet: PromptPacket, v3: InputView, v4: InputView) -> tuple[str, str]:
    """Return the packet's model text with the v4 source identity written as the v3 one.

    The two documents are different files: each prompt names its own input kind and
    carries its own content digest. Nothing else may differ.
    """

    def rewrite(text: str) -> str:
        text = text.replace(v4.kind.value, v3.kind.value)
        return text.replace(v4.source_digests["input"], v3.source_digests["input"])

    return rewrite(packet.system), rewrite(packet.user)


def test_a_one_turn_v4_handoff_renders_the_prompts_of_the_v3_handoff(tmp_path: Path) -> None:
    refund = world("refund")
    plan, metadata = refund["plan"], refund["metadata"]
    v4_input = v4_view(tmp_path, 1)
    v3 = _packets(refund["view"], refund, plan, metadata)
    v4 = _packets(v4_input, refund, plan, metadata)

    for stage, packet in v3.items():
        assert _as_v3(v4[stage], refund["view"], v4_input) == (packet.system, packet.user), stage
        assert v4[stage].version == packet.version
    assert "MULTI-TURN" not in "".join(packet.user for packet in v4.values())


def test_a_v3_prompt_carries_no_multi_turn_block() -> None:
    refund = world("refund")

    for packet in _packets(refund["view"], refund, refund["plan"], refund["metadata"]).values():
        assert "MULTI-TURN" not in packet.user
        assert "multi_turn_shape" not in packet.payload


def test_the_response_contract_of_a_one_turn_v4_handoff_is_the_v3_contract(
    tmp_path: Path,
) -> None:
    refund = world("refund")
    v3 = build_call1_packet_v2(refund["view"], refund["inventory"], refund["runtime_contract"])
    v4 = build_call1_packet_v2(
        v4_view(tmp_path, 1), refund["inventory"], refund["runtime_contract"]
    )

    assert rendered_response_contract(v4) == rendered_response_contract(v3)


# --- the multi-turn rule block ---------------------------------------------------


@pytest.mark.parametrize("turns", [2, 3, 4])
def test_every_packet_of_a_multi_turn_handoff_carries_the_rule_block(
    tmp_path: Path, turns: int
) -> None:
    refund = world("refund")
    view = v4_view(tmp_path, turns)
    plan = sequential_plan(refund["plan"], turns)
    packets = _packets(view, refund, plan, sequential_metadata(refund["metadata"], turns))

    for stage, packet in packets.items():
        assert "MULTI-TURN SHAPE" in packet.user, stage
        assert json_section(packet.user, "MULTI-TURN SHAPE")["turn_count"] == turns, stage


def test_the_rule_block_explains_each_new_field_and_gives_one_example_of_it(
    tmp_path: Path,
) -> None:
    block = multi_turn_block(v4_view(tmp_path, 3))

    assert block is not None
    assert block["turn_count"] == 3
    assert [turn["purpose"] for turn in block["turn_plan"]] == PURPOSES[3]
    assert all(turn["meaning"] for turn in block["turn_plan"])
    fields = block["fields"]
    assert set(fields) == {
        "stimulus_approach.delivery",
        "stimulus_approach.history",
        "stimulus_approach.request",
        "stimulus.delivery",
        "stimulus.history",
        "stimulus.user_text",
    }
    for name, entry in fields.items():
        assert entry["meaning"], name
        assert "example" in entry, name
    assert fields["stimulus_approach.delivery"]["example"] == SEQUENTIAL_DELIVERY
    assert len(fields["stimulus_approach.history"]["example"]) == 2
    assert len(fields["stimulus.history"]["example"]) == 2
    assert fields["stimulus.history"]["example"][0]["role"] == "user"
    assert isinstance(fields["stimulus.user_text"]["example"], str)


def test_the_rule_block_is_absent_for_one_turn_and_for_v3(tmp_path: Path) -> None:
    assert multi_turn_block(v4_view(tmp_path, 1)) is None
    assert multi_turn_block(world("refund")["view"]) is None


def test_a_multi_turn_plan_contract_names_the_sequential_delivery(tmp_path: Path) -> None:
    refund = world("refund")
    packet = build_call1_packet_v2(
        v4_view(tmp_path, 3), refund["inventory"], sequential_runtime(refund["runtime_contract"])
    )

    stimulus = rendered_response_contract(packet)["schema"]["properties"]["stimulus_approach"]
    assert stimulus["properties"]["delivery"]["enum"] == [SEQUENTIAL_DELIVERY]


# --- checks -------------------------------------------------------------------------


def test_a_plan_with_the_right_number_of_turn_intents_passes(tmp_path: Path) -> None:
    refund = world("refund")
    plan = sequential_plan(refund["plan"], 3)

    assert sequential_plan_findings(v4_view(tmp_path, 3), plan) == []


@pytest.mark.parametrize("intents", [0, 1, 3])  # 2 is the right number for 3 turns
def test_a_plan_with_the_wrong_number_of_turn_intents_is_found(
    tmp_path: Path, intents: int
) -> None:
    refund = world("refund")
    plan = sequential_plan(refund["plan"], 3)
    plan["stimulus_approach"]["history"] = (EARLIER + ["Ask once more."])[:intents]

    [finding] = sequential_plan_findings(v4_view(tmp_path, 3), plan)

    assert finding.code == "shape_turn_count_mismatch"
    assert finding.path == "stimulus_approach.history"
    assert "3 user turns" in finding.detail
    assert "2 earlier" in finding.detail


def test_a_multi_turn_plan_must_use_the_sequential_delivery(tmp_path: Path) -> None:
    refund = world("refund")
    plan = sequential_plan(refund["plan"], 3)
    plan["stimulus_approach"]["delivery"] = "direct_user_message"

    [finding] = sequential_plan_findings(v4_view(tmp_path, 3), plan)

    assert finding.code == "shape_delivery_mismatch"
    assert finding.path == "stimulus_approach.delivery"
    assert SEQUENTIAL_DELIVERY in finding.detail


def test_a_one_turn_plan_with_earlier_turns_or_the_sequential_delivery_is_found(
    tmp_path: Path,
) -> None:
    refund = world("refund")
    plan = sequential_plan(refund["plan"], 3)

    codes = {finding.code for finding in sequential_plan_findings(v4_view(tmp_path, 1), plan)}

    assert codes == {"shape_turn_count_mismatch", "shape_delivery_mismatch"}


def test_a_v3_plan_is_never_held_to_the_shape(tmp_path: Path) -> None:
    refund = world("refund")
    plan = sequential_plan(refund["plan"], 3)

    assert sequential_plan_findings(refund["view"], plan) == []
    assert (
        sequential_artifact_findings(refund["view"], sequential_metadata(refund["metadata"], 3))
        == []
    )


def test_an_artifact_with_the_right_number_of_turns_passes(tmp_path: Path) -> None:
    refund = world("refund")
    metadata = sequential_metadata(refund["metadata"], 3)

    assert sequential_artifact_findings(v4_view(tmp_path, 3), metadata) == []


def test_an_artifact_whose_history_does_not_fit_the_turn_count_is_found(
    tmp_path: Path,
) -> None:
    refund = world("refund")
    metadata = sequential_metadata(refund["metadata"], 3)
    metadata["stimulus"]["history"].pop()

    [finding] = sequential_artifact_findings(v4_view(tmp_path, 3), metadata)

    assert finding.code == "shape_turn_count_mismatch"
    assert finding.path == "stimulus.history"
    assert "3 user turns" in finding.detail
    assert "found 1" in finding.detail


def test_an_artifact_with_the_wrong_delivery_is_found(tmp_path: Path) -> None:
    refund = world("refund")
    metadata = sequential_metadata(refund["metadata"], 3)
    metadata["stimulus"]["delivery"] = "direct_user_message"

    [finding] = sequential_artifact_findings(v4_view(tmp_path, 3), metadata)

    assert finding.code == "shape_delivery_mismatch"
    assert finding.path == "stimulus.delivery"


def test_a_malformed_stimulus_leaves_the_type_checks_to_the_existing_findings(
    tmp_path: Path,
) -> None:
    view = v4_view(tmp_path, 3)

    assert sequential_artifact_findings(view, {"stimulus": "text"}) == []
    assert sequential_artifact_findings(view, {"stimulus": {"history": "x"}}) == []
    assert sequential_plan_findings(view, {"stimulus_approach": {"history": "x"}}) == []
    assert sequential_plan_findings(view, "plan") == []


def test_the_existing_checks_accept_the_sequential_plan_and_artifact(tmp_path: Path) -> None:
    refund = world("refund")
    rt = sequential_runtime(refund["runtime_contract"])
    plan = sequential_plan(refund["plan"], 3)
    metadata = sequential_metadata(refund["metadata"], 3)

    assert collect_plan_findings_v2(plan, refund["inventory"], rt) == []
    assert collect_artifact_findings_v2(metadata, plan, refund["inventory"], rt) == []


@pytest.mark.parametrize("turns", [1, 3])
def test_a_plan_correction_keeps_the_rule_block_and_the_contract_of_its_turn_count(
    tmp_path: Path, turns: int
) -> None:
    refund = world("refund")
    view = v4_view(tmp_path, turns)
    rt = sequential_runtime(refund["runtime_contract"])
    finding = Finding("shape_turn_count_mismatch", "found 0", "stimulus_approach.history")
    context = build_correction_context(
        failed_stage="call1",
        original_context={
            **build_plan_author_context(view, refund["inventory"], rt),
            **multi_turn_payload(view, rt),
        },
        current_output=b"{}",
        findings=[finding],
    )

    packet = _render_correction_packet(
        context, correction_repair_inputs(view, refund["inventory"], rt)
    )

    assert ("multi_turn_shape" in packet.user) == (turns > 1)
    enum = context["response_contract"]["schema"]["properties"]["stimulus_approach"]["properties"][
        "delivery"
    ]["enum"]
    assert (enum == [SEQUENTIAL_DELIVERY]) == (turns > 1)
