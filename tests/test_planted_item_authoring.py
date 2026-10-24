"""An indirect shape: benign user turns, one planted text, and the seed that carries it."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from asago_artifact_generator.authoring.binding_repair import correction_repair_inputs
from asago_artifact_generator.authoring.checks import (
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
)
from asago_artifact_generator.authoring.contracts import _call2_contract_v2
from asago_artifact_generator.authoring.core import Finding, PromptPacket
from asago_artifact_generator.authoring.correction import (
    _render_correction_packet,
    build_correction_context,
)
from asago_artifact_generator.authoring.planted_item import (
    STIMULUS_SLOTS_MEANING,
    VERBATIM_RUN,
    planted_item_findings,
    runtime_contract_for_prompt,
)
from asago_artifact_generator.authoring.policy import AuthoringPolicy
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
from asago_artifact_generator.authoring.sequential_turns import (
    multi_turn_block,
    multi_turn_payload,
    sequential_artifact_findings,
    sequential_plan_findings,
)
from asago_artifact_generator.authoring.templates import prose
from asago_artifact_generator.input_adapter import InputView
from asago_artifact_generator.package_io import load_package

from .support import (
    ScriptedAuthoringTransport,
    json_section,
    load_failure_evidence,
    rendered_response_contract,
    review_response,
    scripted_orchestrator,
    stage_local_orchestrator,
    world,
)
from .turn_support import (
    ASKS,
    CARRIER_SLOT,
    PLANTED,
    PLANTED_ITEM,
    indirect_inventory,
    indirect_metadata,
    indirect_plan,
    indirect_runtime,
    indirect_view,
)


def setup(tmp_path: Path, turns: int = 2):
    refund = world("refund")
    return (
        indirect_view(tmp_path, turns),
        indirect_inventory(refund["inventory"]),
        indirect_runtime(refund["runtime_contract"]),
        indirect_plan(refund["plan"], turns),
        indirect_metadata(refund["metadata"], turns),
    )


LIMIT = CARRIER_SLOT["max_chars"]
SEED_SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "artifact-package"
    / "artifact-seed-v1"
    / "schema.json"
)


def runtime_with(**slot: Any) -> dict[str, Any]:
    """Return the indirect runtime whose only slot is the carrier slot with ``slot`` changes."""

    runtime = indirect_runtime(world("refund")["runtime_contract"])
    return {**runtime, "seed_slots": [{**CARRIER_SLOT, **slot}]}


# --- the model-facing rules --------------------------------------------------------


def test_the_rule_block_names_the_target_and_explains_each_field(
    tmp_path: Path,
) -> None:
    view, _, rt, _, _ = setup(tmp_path)

    block = multi_turn_block(view, rt)

    assert block["delivery"] == PLANTED_ITEM
    assert block["turn_count"] == 2
    assert [turn["purpose"] for turn in block["turn_plan"]] == [
        "ask_to_read_item",
        "follow_up_on_item",
    ]
    assert all(turn["meaning"] for turn in block["turn_plan"])
    assert block["planted_item"] == {
        "carrier_operation": "get_order",
        "slot": "klarna.order.item",
        "field": "item",
        "record_ref": "ORD-101",
        "content_kind": "order_content",
        "slot_controller": "third_party",
        "mode": "replace",
        "mode_meaning": block["planted_item"]["mode_meaning"],
        "record_ref_fixed_by": block["planted_item"]["record_ref_fixed_by"],
    }
    assert block["planted_item"]["mode_meaning"]
    assert "producer's attack shape" in block["planted_item"]["record_ref_fixed_by"]
    assert block["shape_controller"]["controller"] == "third_party"
    assert block["shape_controller"]["meaning"]
    assert block["benign_user_actor_ref"] == "CUST001"
    for name, entry in block["fields"].items():
        assert entry["meaning"], name
        assert "example" in entry, name
    assert set(block["fields"]) == PLAN_FIELDS
    assert block["fields"]["stimulus_approach.delivery"]["example"] == PLANTED_ITEM


PLAN_FIELDS = {
    "stimulus_approach.delivery",
    "stimulus_approach.history",
    "stimulus_approach.request",
}
ARTIFACT_FIELDS = {
    "stimulus.delivery",
    "stimulus.history",
    "stimulus.user_text",
    "planted_text",
}
PLANTED_TEXT_RULE = prose("planted_item/planted_text_rule")


def _assert_plan_block(block: dict[str, Any], stage: str) -> None:
    assert set(block["fields"]) == PLAN_FIELDS, stage
    assert "planted_text" not in json.dumps(block), stage
    assert PLANTED_TEXT_RULE not in block["rule"], stage


def _assert_artifact_block(block: dict[str, Any], stage: str) -> None:
    assert set(block["fields"]) == ARTIFACT_FIELDS, stage
    assert block["rule"].endswith(" " + PLANTED_TEXT_RULE), stage
    assert "planted_text is at most" in block["check"], stage
    for name, entry in block["fields"].items():
        assert entry["meaning"] and "example" in entry, (stage, name)
    assert block["fields"]["stimulus.delivery"]["example"] == PLANTED_ITEM, stage


def test_the_artifact_block_explains_the_stimulus_and_the_planted_text_only(
    tmp_path: Path,
) -> None:
    view, _, rt, plan, _ = setup(tmp_path)

    plan_block = multi_turn_block(view, rt)
    artifact_block = multi_turn_block(view, rt, plan)

    _assert_plan_block(plan_block, "plan")
    _assert_artifact_block(artifact_block, "artifact")
    assert artifact_block["rule"] == f"{plan_block['rule']} {PLANTED_TEXT_RULE}"


def _before(user: str, title: str) -> str:
    """Return the prompt text before the section ``title``: the stage's own instructions."""

    return user[: user.index(f"\n{title}\n")]


def test_each_dispatched_prompt_asks_only_for_the_fields_its_stage_writes(
    tmp_path: Path,
) -> None:
    refund = world("refund")
    plan = indirect_plan(refund["plan"], 2)
    metadata = indirect_metadata(refund["metadata"], 2)
    without_text = {key: value for key, value in metadata.items() if key != "planted_text"}
    responses = [
        json.dumps({**plan, "planted_text": PLANTED}),
        json.dumps(plan),
        review_response(),
        json.dumps(without_text),
        json.dumps(metadata),
        review_response(),
    ]
    orchestrator, transport = scripted_orchestrator(
        tmp_path, responses, task_id="task", policy=AuthoringPolicy()
    )

    result = orchestrator.run(
        indirect_view(tmp_path, 2),
        indirect_inventory(refund["inventory"]),
        indirect_runtime(refund["runtime_contract"]),
    )

    assert result.status == "accepted"
    call1, plan_correction, plan_review, call2, artifact_correction, artifact_review = (
        transport.requests
    )
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "correction",
        "plan_review",
        "call2",
        "correction",
        "artifact_review",
    ]
    for stage, request in (("call1", call1), ("plan_review", plan_review)):
        assert "planted_text" not in request["user"], stage
        _assert_plan_block(json_section(request["user"], "INDIRECT SHAPE"), stage)
    # The plan correction shows the rejected output and its finding, and nothing else
    # names the field.
    assert "planted_text" not in _before(plan_correction["user"], "CURRENT OUTPUT")
    _assert_plan_block(
        plan_correction["payload"]["original_context"]["multi_turn_shape"], "plan_correction"
    )
    assert plan_correction["payload"]["findings"] == [
        {
            "code": "unexpected_field",
            "detail": "unexpected plan field: planted_text",
            "path": "planted_text",
        }
    ]
    for stage, request in (("call2", call2), ("artifact_review", artifact_review)):
        _assert_artifact_block(json_section(request["user"], "INDIRECT SHAPE"), stage)
    _assert_artifact_block(
        artifact_correction["payload"]["original_context"]["multi_turn_shape"],
        "artifact_correction",
    )
    assert PLANTED_TEXT_RULE in artifact_correction["user"]


def test_the_plan_check_rejects_a_plan_that_writes_planted_text(tmp_path: Path) -> None:
    view, inventory, rt, plan, _ = setup(tmp_path)
    plan["planted_text"] = PLANTED

    findings = collect_plan_findings_v2(plan, inventory, rt)

    assert [(f.code, f.path) for f in findings] == [("unexpected_field", "planted_text")]
    assert sequential_plan_findings(view, plan, rt) == []


def test_every_packet_of_an_indirect_handoff_carries_the_block(tmp_path: Path) -> None:
    view, inventory, rt, plan, metadata = setup(tmp_path)
    packets = {
        "call1": build_call1_packet_v2(view, inventory, rt),
        "call2": build_call2_packet_v2(view, plan, inventory, rt),
        "plan_review": build_plan_review_packet(view, plan, inventory, rt),
        "artifact_review": build_artifact_review_packet(view, plan, metadata, inventory, rt),
    }

    for stage, packet in packets.items():
        assert "INDIRECT SHAPE" in packet.user, stage
        assert json_section(packet.user, "INDIRECT SHAPE")["delivery"] == PLANTED_ITEM, stage
        assert "MULTI-TURN SHAPE" not in packet.user, stage


def _seed_slot_lists(user: str) -> list[Any]:
    """Return every ``seed_slots`` array a rendered prompt holds, wherever it sits."""

    decoder = json.JSONDecoder()
    found = []
    start = user.find('"seed_slots":')
    while start >= 0:
        value, end = decoder.raw_decode(user, user.index("[", start))
        found.append(value)
        start = user.find('"seed_slots":', end)
    return found


def test_every_request_shows_the_seed_slots_without_their_limit_and_mode(
    tmp_path: Path,
) -> None:
    view, inventory, rt, plan, metadata = setup(tmp_path)
    assert all({"max_chars", "mode"} <= set(slot) for slot in rt["seed_slots"])

    def correction(failed_stage: str, accepted_plan: Any) -> PromptPacket:
        context = (
            build_plan_author_context(view, inventory, rt)
            if accepted_plan is None
            else build_artifact_author_context(view, accepted_plan, inventory, rt)
        )
        payload = build_correction_context(
            failed_stage=failed_stage,
            original_context={**context, **multi_turn_payload(view, rt, accepted_plan)},
            current_output=b"{}",
            findings=[],
        )
        return _render_correction_packet(payload, correction_repair_inputs(view, inventory, rt))

    packets = {
        "call1": build_call1_packet_v2(view, inventory, rt),
        "call2": build_call2_packet_v2(view, plan, inventory, rt),
        "plan_review": build_plan_review_packet(view, plan, inventory, rt),
        "artifact_review": build_artifact_review_packet(view, plan, metadata, inventory, rt),
        "plan_correction": correction("call1", None),
        "artifact_correction": correction("call2", plan),
    }

    shown = {"id", "carrier_operation", "field", "controller", "content_kind", "records"}
    for stage, packet in packets.items():
        lists = _seed_slot_lists(packet.user)
        assert lists, stage
        assert all(set(slot) == shown for slots in lists for slot in slots), stage
        assert "max_chars" not in packet.user, stage


def test_the_prompt_copy_leaves_the_supplied_contract_and_unreadable_slots_as_they_are() -> None:
    runtime = {"delivery": [PLANTED_ITEM], "seed_slots": [CARRIER_SLOT, "slot", None]}

    shown = runtime_contract_for_prompt(runtime)

    assert shown["seed_slots"][1:] == ["slot", None]
    assert "mode" not in shown["seed_slots"][0]
    assert runtime["seed_slots"][0] is CARRIER_SLOT and "mode" in CARRIER_SLOT
    assert runtime_contract_for_prompt({"delivery": []}) == {"delivery": []}


def test_the_block_and_the_plan_contract_name_the_planted_item_delivery(
    tmp_path: Path,
) -> None:
    view, inventory, rt, _, _ = setup(tmp_path, turns=1)

    packet = build_call1_packet_v2(view, inventory, rt)

    properties = rendered_response_contract(packet)["schema"]["properties"]
    assert properties["stimulus_approach"]["properties"]["delivery"]["enum"] == [PLANTED_ITEM]


def test_the_artifact_contract_asks_for_planted_text_only_under_the_planted_item_plan(
    tmp_path: Path,
) -> None:
    _, _, _, plan, _ = setup(tmp_path)
    plain = world("refund")["plan"]

    contract = _call2_contract_v2(plan, planted_max_chars=120)
    assert "planted_text" in contract["fields"]
    assert "planted_text" in contract["schema"]["required"]
    text = contract["schema"]["properties"]["planted_text"]
    assert text["type"] == "string"
    assert text["maxLength"] == 120
    assert "at most 120 characters" in text["description"]
    unchanged = _call2_contract_v2(plain, planted_max_chars=120)
    assert "planted_text" not in unchanged["fields"]
    assert "planted_text" not in unchanged["schema"]["properties"]
    assert "planted_text" not in _call2_contract_v2(None)["fields"]


def test_the_slot_limit_reaches_the_artifact_request_and_its_correction(tmp_path: Path) -> None:
    view, inventory, _, plan, _ = setup(tmp_path)
    rt = runtime_with(max_chars=50)

    packet = build_call2_packet_v2(view, plan, inventory, rt)
    correction = build_correction_context(
        failed_stage="call2",
        original_context={
            **build_artifact_author_context(view, plan, inventory, rt),
            **multi_turn_payload(view, rt, plan),
        },
        current_output=b"{}",
        findings=[],
    )

    for contract in (rendered_response_contract(packet), correction["response_contract"]):
        text = contract["schema"]["properties"]["planted_text"]
        assert text["maxLength"] == 50
        assert "at most 50 characters" in text["description"]
    block = json_section(packet.user, "INDIRECT SHAPE")
    assert "at most 50 characters" in block["check"]
    assert "at most 50 characters" in block["fields"]["planted_text"]["meaning"]


def test_only_a_plan_that_plants_a_text_explains_the_stimulus_slots(tmp_path: Path) -> None:
    _, _, _, plan, _ = setup(tmp_path)
    plain = world("refund")["plan"]
    untyped = {"type": "array", "items": {"type": "string"}}

    def slots(contract: dict[str, Any]) -> dict[str, Any]:
        return contract["schema"]["properties"]["stimulus"]["properties"]["slots"]

    assert slots(_call2_contract_v2(plan, planted_max_chars=120)) == {
        **untyped,
        "description": STIMULUS_SLOTS_MEANING,
    }
    assert "{{binding_name}}" in STIMULUS_SLOTS_MEANING
    assert slots(_call2_contract_v2(plan)) == untyped
    assert slots(_call2_contract_v2(plain, planted_max_chars=120)) == untyped
    assert slots(_call2_contract_v2(None)) == untyped


def test_the_slots_meaning_reaches_the_artifact_request_and_its_correction_once(
    tmp_path: Path,
) -> None:
    view, inventory, rt, plan, _ = setup(tmp_path)
    original = {
        **build_artifact_author_context(view, plan, inventory, rt),
        **multi_turn_payload(view, rt, plan),
    }
    correction = build_correction_context(
        failed_stage="call2", original_context=original, current_output=b"{}", findings=[]
    )
    call2 = build_call2_packet_v2(view, plan, inventory, rt)
    artifact_correction = _render_correction_packet(
        correction, correction_repair_inputs(view, inventory, rt)
    )

    for contract in (rendered_response_contract(call2), correction["response_contract"]):
        slots = contract["schema"]["properties"]["stimulus"]["properties"]["slots"]
        assert slots["description"] == STIMULUS_SLOTS_MEANING
    for packet in (call2, artifact_correction):
        assert packet.user.count(STIMULUS_SLOTS_MEANING) == 1
        assert STIMULUS_SLOTS_MEANING not in packet.system


def test_no_other_prompt_carries_the_slots_meaning(tmp_path: Path) -> None:
    view, inventory, rt, plan, metadata = setup(tmp_path)
    refund = world("refund")
    plan_correction = _render_correction_packet(
        build_correction_context(
            failed_stage="call1",
            original_context={
                **build_plan_author_context(view, inventory, rt),
                **multi_turn_payload(view, rt),
            },
            current_output=b"{}",
            findings=[],
        ),
        correction_repair_inputs(view, inventory, rt),
    )
    packets = [
        build_call1_packet_v2(view, inventory, rt),
        build_plan_review_packet(view, plan, inventory, rt),
        build_artifact_review_packet(view, plan, metadata, inventory, rt),
        plan_correction,
        build_call2_packet_v2(
            refund["view"], refund["plan"], refund["inventory"], refund["runtime_contract"]
        ),
    ]

    for packet in packets:
        assert "placeholders in" not in packet.system + packet.user


def test_a_planted_item_plan_without_a_slot_limit_plants_no_text(tmp_path: Path) -> None:
    _, _, _, plan, metadata = setup(tmp_path)

    assert "planted_text" not in _call2_contract_v2(plan)["fields"]
    assert [f.code for f in planted_item_findings(metadata, plan, None)] == ["unexpected_field"]
    del metadata["planted_text"]
    assert planted_item_findings(metadata, plan, None) == []


def test_the_artifact_review_receives_the_planted_text_as_a_labelled_section(
    tmp_path: Path,
) -> None:
    view, inventory, rt, plan, metadata = setup(tmp_path)

    packet = build_artifact_review_packet(view, plan, metadata, inventory, rt)
    again = build_artifact_review_packet(
        view, plan, {**metadata, "planted_text": "Another planted text."}, inventory, rt
    )

    section = packet.user.split("PLANTED TEXT", 1)[1]
    assert PLANTED in section
    assert "failure criterion" in section
    assert "third_party" in section
    assert "PLANTED TEXT" not in build_plan_review_packet(view, plan, inventory, rt).user
    assert PLANTED not in json.dumps(packet.payload["multi_turn_shape"])
    assert packet.payload["multi_turn_shape"] == again.payload["multi_turn_shape"]


def test_a_direct_handoff_gets_no_indirect_block_and_no_planted_field() -> None:
    refund = world("refund")
    packets = [
        build_call1_packet_v2(refund["view"], refund["inventory"], refund["runtime_contract"]),
        build_call2_packet_v2(
            refund["view"], refund["plan"], refund["inventory"], refund["runtime_contract"]
        ),
    ]

    # The structured-analysis note defines every procedure channel, tool_data included.
    note = json.dumps(prose("prompt_context/structured_analysis_note"))[1:-1]
    for packet in packets:
        assert "INDIRECT SHAPE" not in packet.user
        assert "planted" not in packet.user.replace(note, "").lower()


@pytest.mark.parametrize("turns", [1, 2])
def test_a_plan_correction_keeps_the_block_and_the_planted_item_contract(
    tmp_path: Path, turns: int
) -> None:
    view, inventory, rt, _, _ = setup(tmp_path, turns)
    context = build_correction_context(
        failed_stage="call1",
        original_context={
            **build_plan_author_context(view, inventory, rt),
            **multi_turn_payload(view, rt),
        },
        current_output=b"{}",
        findings=[Finding("shape_delivery_mismatch", "x", "stimulus_approach.delivery")],
    )

    packet: PromptPacket = _render_correction_packet(
        context, correction_repair_inputs(view, inventory, rt)
    )

    assert "multi_turn_shape" in packet.user
    assert "planted_item" in packet.user
    properties = context["response_contract"]["schema"]["properties"]
    assert properties["stimulus_approach"]["properties"]["delivery"]["enum"] == [PLANTED_ITEM]


# --- plan and artifact checks --------------------------------------------------------


def test_the_existing_checks_accept_the_indirect_plan_and_artifact(tmp_path: Path) -> None:
    view, inventory, rt, plan, metadata = setup(tmp_path)

    assert sequential_plan_findings(view, plan) == []
    assert sequential_artifact_findings(view, metadata) == []
    assert collect_plan_findings_v2(plan, inventory, rt) == []
    assert (
        collect_artifact_findings_v2(metadata, plan, inventory, rt, planted_max_chars=LIMIT) == []
    )


def test_an_indirect_plan_must_use_the_planted_item_delivery(tmp_path: Path) -> None:
    view, _, _, plan, _ = setup(tmp_path)
    plan["stimulus_approach"]["delivery"] = "sequential_user_turns"

    [finding] = sequential_plan_findings(view, plan)

    assert finding.code == "shape_delivery_mismatch"
    assert finding.path == "stimulus_approach.delivery"
    assert PLANTED_ITEM in finding.detail


def test_an_indirect_artifact_must_use_the_planted_item_delivery_and_the_turn_count(
    tmp_path: Path,
) -> None:
    view, _, _, _, metadata = setup(tmp_path)
    metadata["stimulus"]["delivery"] = "sequential_user_turns"
    metadata["stimulus"]["history"] = []

    codes = {finding.code for finding in sequential_artifact_findings(view, metadata)}

    assert codes == {"shape_delivery_mismatch", "shape_turn_count_mismatch"}


def test_a_one_turn_indirect_shape_has_no_earlier_turns(tmp_path: Path) -> None:
    refund = world("refund")
    view = indirect_view(tmp_path, 1)

    assert sequential_artifact_findings(view, indirect_metadata(refund["metadata"], 1)) == []
    assert sequential_plan_findings(view, indirect_plan(refund["plan"], 1)) == []


def test_a_direct_artifact_with_planted_text_is_found(tmp_path: Path) -> None:
    refund = world("refund")
    metadata = {**refund["metadata"], "planted_text": PLANTED}

    findings = collect_artifact_findings_v2(
        metadata, refund["plan"], refund["inventory"], refund["runtime_contract"]
    )

    assert [(f.code, f.path) for f in findings] == [("unexpected_field", "planted_text")]


def test_an_indirect_artifact_without_planted_text_is_found(tmp_path: Path) -> None:
    _, inventory, rt, plan, metadata = setup(tmp_path)
    del metadata["planted_text"]

    findings = collect_artifact_findings_v2(metadata, plan, inventory, rt, planted_max_chars=LIMIT)

    assert [(f.code, f.path) for f in findings] == [("missing_field", "planted_text")]


@pytest.mark.parametrize("value", [None, 7, ["x"], ""])
def test_planted_text_must_be_a_non_empty_string(tmp_path: Path, value: Any) -> None:
    _, _, _, plan, metadata = setup(tmp_path)
    metadata["planted_text"] = value

    [finding] = planted_item_findings(metadata, plan, LIMIT)

    assert finding.path == "planted_text"
    assert finding.code in {"type_error", "planted_text_empty"}


def test_planted_text_of_exactly_the_slot_limit_passes_and_one_more_is_found(
    tmp_path: Path,
) -> None:
    _, _, _, plan, metadata = setup(tmp_path)
    metadata["planted_text"] = "x" * 120
    assert planted_item_findings(metadata, plan, 120) == []

    metadata["planted_text"] = "x" * 121
    [finding] = planted_item_findings(metadata, plan, 120)

    assert finding.code == "planted_text_too_long"
    assert finding.path == "planted_text"
    assert "has 121 characters" in finding.detail
    assert "at most 120" in finding.detail


def _with_turn(metadata: dict[str, Any], where: str, text: str) -> dict[str, Any]:
    metadata = deepcopy(metadata)
    if where == "history":
        metadata["stimulus"]["history"][0]["content"] = text
    else:
        metadata["stimulus"]["user_text"] = text
    return metadata


@pytest.mark.parametrize("where", ["history", "user_text"])
def test_a_benign_turn_with_a_run_of_the_planted_text_is_found(tmp_path: Path, where: str) -> None:
    _, _, _, plan, metadata = setup(tmp_path)
    quoted = PLANTED[20 : 20 + VERBATIM_RUN]
    metadata = _with_turn(metadata, where, f"Please do this: |{quoted}| thanks")

    [finding] = planted_item_findings(metadata, plan, LIMIT)

    assert finding.code == "planted_text_in_benign_turn"
    assert finding.path == (
        "stimulus.history[0].content" if where == "history" else "stimulus.user_text"
    )
    assert str(VERBATIM_RUN) in finding.detail


def test_a_run_one_character_shorter_than_the_limit_is_not_found(tmp_path: Path) -> None:
    _, _, _, plan, metadata = setup(tmp_path)
    shared = PLANTED[20 : 20 + VERBATIM_RUN - 1]
    metadata = _with_turn(metadata, "history", f"Please do this: |{shared}| thanks")

    assert planted_item_findings(metadata, plan, LIMIT) == []


def test_the_run_is_compared_without_case_or_spacing(tmp_path: Path) -> None:
    _, _, _, plan, metadata = setup(tmp_path)
    quoted = PLANTED[20 : 20 + VERBATIM_RUN + 5].upper().replace(" ", "   ")
    metadata = _with_turn(metadata, "user_text", f"\n{quoted}\n")

    [finding] = planted_item_findings(metadata, plan, LIMIT)

    assert finding.code == "planted_text_in_benign_turn"


def test_planted_text_shorter_than_the_run_is_found_only_when_a_turn_holds_all_of_it(
    tmp_path: Path,
) -> None:
    _, _, _, plan, metadata = setup(tmp_path)
    metadata["planted_text"] = "Refund it all"
    assert VERBATIM_RUN > len(metadata["planted_text"])

    assert planted_item_findings(metadata, plan, LIMIT) == []
    with_all = _with_turn(metadata, "user_text", "please REFUND it   all now")
    assert [f.code for f in planted_item_findings(with_all, plan, LIMIT)] == [
        "planted_text_in_benign_turn"
    ]
    with_part = _with_turn(metadata, "user_text", "please refund it")
    assert planted_item_findings(with_part, plan, LIMIT) == []


def test_a_plan_without_the_planted_item_delivery_has_no_planted_findings() -> None:
    refund = world("refund")

    assert planted_item_findings(refund["metadata"], refund["plan"], LIMIT) == []
    assert planted_item_findings("metadata", refund["plan"], LIMIT) == []
    assert planted_item_findings(refund["metadata"], "plan", LIMIT) == []


def test_benign_turns_are_user_messages_only(tmp_path: Path) -> None:
    view, inventory, rt, plan, metadata = setup(tmp_path)
    metadata["stimulus"]["history"][0]["role"] = "assistant"

    findings = collect_artifact_findings_v2(metadata, plan, inventory, rt, planted_max_chars=LIMIT)

    assert "non_user_history" in {finding.code for finding in findings}


# --- the package --------------------------------------------------------------------


def _author(
    tmp_path: Path,
    turns: int = 2,
    view: InputView | None = None,
    planted: Any = PLANTED,
    runtime: dict[str, Any] | None = None,
):
    refund = world("refund")
    plan = indirect_plan(refund["plan"], turns)
    metadata = indirect_metadata(refund["metadata"], turns, planted)
    transport = ScriptedAuthoringTransport([json.dumps(plan), json.dumps(metadata)])
    result = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    ).run(
        view or indirect_view(tmp_path, turns),
        indirect_inventory(refund["inventory"]),
        runtime or indirect_runtime(refund["runtime_contract"]),
    )
    return result, transport


def test_an_indirect_handoff_becomes_a_v5_package_with_a_seed_that_follows_the_seed_schema(
    tmp_path: Path,
) -> None:
    result, transport = _author(tmp_path)

    assert result.status == "accepted", result.findings
    assert [request["stage"] for request in transport.requests] == ["call1", "call2"]
    package = tmp_path / "package"
    seed = json.loads((package / "seed.json").read_text(encoding="utf-8"))
    Draft202012Validator(json.loads(SEED_SCHEMA.read_text(encoding="utf-8"))).validate(seed)
    assert seed == {
        "schema_version": "artifact-seed-v1",
        "carrier_operation": "get_order",
        "items": [
            {
                "slot": "klarna.order.item",
                "record_ref": "ORD-101",
                "mode": "replace",
                "content": PLANTED,
            }
        ],
    }
    stimulus = json.loads((package / "stimulus.json").read_text(encoding="utf-8"))
    assert stimulus["mode"] == "sequential"
    assert stimulus["turn_count"] == 2
    assert stimulus["delivery"] == PLANTED_ITEM
    assert len(stimulus["history"]) == 1
    assert PLANTED not in json.dumps(stimulus)
    assert "planted_text" not in json.dumps(stimulus)
    loaded = load_package(package)
    assert loaded.manifest.schema_version == "artifact-package-v5"
    assert "seed.json" in loaded.members


def test_a_one_turn_indirect_package_is_single_and_still_seeded(tmp_path: Path) -> None:
    result, _ = _author(tmp_path, turns=1)

    assert result.status == "accepted", result.findings
    stimulus = json.loads((tmp_path / "package" / "stimulus.json").read_text(encoding="utf-8"))
    assert (stimulus["mode"], stimulus["turn_count"], stimulus["history"]) == ("single", 1, [])
    assert (tmp_path / "package" / "seed.json").is_file()


def test_planted_text_that_quotes_a_benign_turn_is_not_packaged(tmp_path: Path) -> None:
    result, transport = _author(tmp_path, planted=ASKS[0])

    assert result.status != "accepted"
    assert not (tmp_path / "package" / "seed.json").exists()
    evidence = load_failure_evidence(result.failure_evidence_path)
    call2 = next(attempt for attempt in evidence["attempts"] if attempt["stage"] == "call2")
    assert [finding["code"] for finding in call2["findings"]] == ["planted_text_in_benign_turn"]


def test_the_package_holds_planted_text_up_to_its_slot_limit(tmp_path: Path) -> None:
    (tmp_path / "fits").mkdir()
    fits, _ = _author(tmp_path / "fits", runtime=runtime_with(max_chars=len(PLANTED)))
    longer, _ = _author(tmp_path, runtime=runtime_with(max_chars=len(PLANTED) - 1))

    assert fits.status == "accepted", fits.findings
    assert longer.status != "accepted"
    assert not (tmp_path / "package" / "seed.json").exists()
    evidence = load_failure_evidence(longer.failure_evidence_path)
    call2 = next(attempt for attempt in evidence["attempts"] if attempt["stage"] == "call2")
    assert [finding["code"] for finding in call2["findings"]] == ["planted_text_too_long"]


def test_the_seed_writes_the_text_with_the_mode_its_slot_states(tmp_path: Path) -> None:
    result, _ = _author(tmp_path, runtime=runtime_with(mode="append"))

    assert result.status == "accepted", result.findings
    seed = json.loads((tmp_path / "package" / "seed.json").read_text(encoding="utf-8"))
    assert seed["items"][0]["mode"] == "append"


def test_a_direct_package_has_no_seed_and_costs_the_same_requests(tmp_path: Path) -> None:
    refund = world("refund")
    transport = ScriptedAuthoringTransport(
        [json.dumps(refund["plan"]), json.dumps(refund["metadata"])]
    )
    result = stage_local_orchestrator(
        transport=transport, package_dir=tmp_path / "package", task_id="task"
    ).run(refund["view"], refund["inventory"], refund["runtime_contract"])
    (tmp_path / "other").mkdir()
    indirect, indirect_transport = _author(tmp_path / "other")

    assert result.status == "accepted"
    assert not (tmp_path / "package" / "seed.json").exists()
    assert [r["stage"] for r in transport.requests] == [
        r["stage"] for r in indirect_transport.requests
    ]
    assert indirect.status == "accepted"
