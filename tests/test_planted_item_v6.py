"""A shape's controlling party reads under either published name.

``scenario-handoff-v6`` renames ``party_relation.controller`` to
``party_relation.controlling_party``. Authoring reads a shape of either version
through one accessor, and the advisory finding names the field the shape holds.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from asago_artifact_generator.authoring.checks import slot_party_findings
from asago_artifact_generator.authoring.planted_item import (
    controlling_party,
    controlling_party_path,
    indirect_block,
)

from .turn_support import CARRIER_SLOT, indirect_shape

_RUNTIME: dict[str, Any] = {
    "delivery": ["direct_user_message", "planted_item"],
    "setup_permissions": [],
    "observation": {"tool_calls": {"availability": "captured_or_unavailable"}},
    "limits": {"max_turns": 4},
    "seed_slots": [CARRIER_SLOT],
}


def _renamed(shape: dict[str, Any]) -> dict[str, Any]:
    """Return the shape as v6 states it: the party under its new name."""

    renamed = deepcopy(shape)
    relation = renamed["indirect"]["party_relation"]
    relation["controlling_party"] = relation.pop("controller")
    return renamed


def test_the_accessor_reads_the_v5_field_name() -> None:
    shape = indirect_shape()

    assert controlling_party(shape) == "third_party"
    assert controlling_party_path(shape) == "attack_shape.indirect.party_relation.controller"


def test_the_accessor_reads_the_v6_field_name() -> None:
    shape = _renamed(indirect_shape())

    assert controlling_party(shape) == "third_party"
    assert (
        controlling_party_path(shape) == "attack_shape.indirect.party_relation.controlling_party"
    )


def test_the_advisory_finding_names_the_v6_field() -> None:
    slot = {**CARRIER_SLOT, "controller": "counterparty"}

    [finding] = slot_party_findings(_renamed(indirect_shape()), {**_RUNTIME, "seed_slots": [slot]})

    assert finding.code == "slot_party_mismatch"
    assert finding.path == "attack_shape.indirect.party_relation.controlling_party"
    assert finding.details["controlling_party"] == "third_party"


def test_the_planted_block_states_the_v6_party() -> None:
    view = SimpleNamespace(attack_shape=_renamed(indirect_shape()))

    block = indirect_block(view, _RUNTIME, None)

    assert block["shape_controlling_party"]["party"] == "third_party"
