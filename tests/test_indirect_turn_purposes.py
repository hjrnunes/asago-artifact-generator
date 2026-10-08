"""Every purpose the producer allows on a benign user turn has a meaning in the indirect block."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.attack_shape import attack_shape_violation
from asago_artifact_generator.authoring.planted_item import _TURN_MEANINGS
from asago_artifact_generator.authoring.prompt_packets import build_call1_packet_v2
from asago_artifact_generator.authoring.sequential_turns import (
    _PURPOSE_MEANINGS,
    multi_turn_block,
)
from asago_artifact_generator.input_adapter import (
    InputView,
    _framed_digest,
    _handoff_schema,
    load_input,
)

from .support import json_section, world
from .turn_support import indirect_inventory, indirect_runtime, indirect_shape

_REFUND_BOUND = Path(__file__).resolve().parent / "fixtures" / "handoffs" / "refund-bound.json"
_SCHEMA = _handoff_schema("scenario-handoff-v4")
_PURPOSES = _SCHEMA["$defs"]["TurnPurpose"]["enum"]
_READ = "ask_to_read_item"


def _indirect_shape_with(purposes: list[str]) -> dict[str, Any]:
    shape = indirect_shape(turns=len(purposes))
    for turn, purpose in zip(shape["turn_plan"], purposes, strict=True):
        turn["purpose"] = purpose
    return shape


def _direct_shape_with(purpose: str) -> dict[str, Any]:
    shape = _indirect_shape_with([purpose])
    shape["channel"] = "direct"
    shape["indirect"] = None
    shape["turn_plan"][0]["speaker"] = "attacker_user"
    return shape


def _view(tmp_path: Path, purposes: list[str]) -> InputView:
    payload = json.loads(_REFUND_BOUND.read_text(encoding="utf-8"))
    payload.pop("content_digest")
    payload["schema_version"] = "scenario-handoff-v4"
    payload["attack_shape"] = _indirect_shape_with(purposes)
    payload["content_digest"] = _framed_digest("scenario-handoff-v4", payload)
    path = tmp_path / "indirect.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return load_input(path)


def _allowed_on_benign_user() -> list[str]:
    return [
        purpose
        for purpose in _PURPOSES
        if attack_shape_violation(_indirect_shape_with([_READ, purpose]), _SCHEMA) is None
    ]


def _allowed_on_attacker_user() -> list[str]:
    return [
        purpose
        for purpose in _PURPOSES
        if attack_shape_violation(_direct_shape_with(purpose), _SCHEMA) is None
    ]


# The plan of a C6 handoff that crashed authoring (klarna-r2 SCN-025), then each
# non-reading purpose after the turn that reads the item.
@pytest.mark.parametrize(
    "purposes",
    [
        [_READ, "request_action"],
        ["establish_context", _READ],
        [_READ, "assert_authority"],
        [_READ, "apply_pressure"],
        [_READ, "follow_up_on_item"],
    ],
)
def test_the_indirect_plan_payload_builds_for_every_benign_turn_purpose(
    tmp_path: Path, purposes: list[str]
) -> None:
    refund = world("refund")
    view = _view(tmp_path, purposes)
    inventory = indirect_inventory(refund["inventory"])
    runtime = indirect_runtime(refund["runtime_contract"])

    block = multi_turn_block(view, runtime)
    packet = build_call1_packet_v2(view, inventory, runtime)

    assert [turn["purpose"] for turn in block["turn_plan"]] == purposes
    assert all(turn["meaning"].strip() for turn in block["turn_plan"])
    assert json_section(packet.user, "INDIRECT SHAPE")["turn_plan"] == block["turn_plan"]


def test_every_purpose_the_contract_allows_on_a_benign_user_turn_has_an_indirect_meaning() -> None:
    allowed = _allowed_on_benign_user()

    assert "forged_history" not in allowed
    assert set(allowed) == set(_PURPOSES) - {"forged_history"}
    assert set(allowed) <= set(_TURN_MEANINGS)


def test_every_purpose_the_contract_allows_on_an_attacker_user_turn_has_a_meaning() -> None:
    allowed = _allowed_on_attacker_user()

    assert {_READ, "follow_up_on_item", "forged_history"}.isdisjoint(allowed)
    assert allowed
    assert set(allowed) <= set(_PURPOSE_MEANINGS)
