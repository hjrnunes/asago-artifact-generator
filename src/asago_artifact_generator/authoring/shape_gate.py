"""Pre-model refusals for an attack shape this consumer cannot author.

A refused scenario is non-executable and stays so: it is never folded into a
single-turn direct attack. The refusal needs no model request, so it is built
from the handoff and the runtime contract alone.
"""

from __future__ import annotations

from typing import Any

from ..attack_shape import CHANNEL_DIRECT, CHANNEL_FORGED, CHANNEL_INDIRECT
from ..input_adapter import InputView
from .core import Finding
from .planted_item import PLANTED_ITEM_DELIVERY, choose_planted_target, unavailable_details

SEQUENTIAL_DELIVERY = "sequential_user_turns"
DIRECT_DELIVERY = "direct_user_message"
# The planned-turn limit a runtime contract without the field is read with.
DEFAULT_MAX_PLANNED_TURNS = 4
_SHAPE_PATH = "attack_shape"


def _delivery_for(shape: dict[str, Any]) -> str | None:
    """Return the runtime delivery a shape needs, or None when no delivery can carry it."""

    if shape["channel"] == CHANNEL_INDIRECT:
        return PLANTED_ITEM_DELIVERY
    if shape["channel"] == CHANNEL_DIRECT:
        return SEQUENTIAL_DELIVERY if shape["turn_count"] > 1 else DIRECT_DELIVERY
    return None


def max_planned_turns(runtime_contract: dict[str, Any]) -> int:
    """Return the most user turns the runtime contract lets a package plan."""

    limits = runtime_contract.get("limits")
    value = limits.get("max_planned_turns") if isinstance(limits, dict) else None
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool)
        else (DEFAULT_MAX_PLANNED_TURNS)
    )


def shape_refusal_findings(
    view: InputView, runtime_contract: dict[str, Any], inventory: dict[str, Any] | None = None
) -> list[Finding]:
    """Return the findings that make the handoff's attack shape unauthorable.

    An empty list means the shape (or its absence) is authorable. v3 handoffs
    and functional v4 handoffs carry a single-turn direct shape, which the
    runtime contract of every release delivers. An indirect shape also needs its
    carrier in the ``inventory`` and a seed slot in the runtime contract.
    """

    shape = view.attack_shape
    if shape is None or shape["channel"] == CHANNEL_DIRECT and shape["turn_count"] == 1:
        return []
    if shape["channel"] == CHANNEL_FORGED:
        return [_forged_refusal(shape)]
    findings = []
    delivery = runtime_contract.get("delivery")
    listed = list(delivery) if isinstance(delivery, list) else []
    if _delivery_for(shape) not in listed:
        findings.append(_channel_refusal(shape, listed))
    elif shape["channel"] == CHANNEL_INDIRECT:
        findings.extend(_planted_item_refusals(shape, runtime_contract, inventory or {}))
    limit = max_planned_turns(runtime_contract)
    if shape["turn_count"] > limit:
        findings.append(_turn_count_refusal(shape, limit))
    return findings


def _refusal(code: str, text: str, details: dict[str, Any]) -> Finding:
    return Finding(code, text, _SHAPE_PATH, details, stage="plan")


def _forged_refusal(shape: dict[str, Any]) -> Finding:
    return _refusal(
        "shape_forged_transcript_unsupported",
        "the scenario is not executable: its attack shape forges a transcript, "
        "which no runtime delivers",
        {"threat_label": shape["threat_label"]},
    )


def _channel_refusal(shape: dict[str, Any], listed: list[Any]) -> Finding:
    channel = shape["channel"]
    return _refusal(
        "shape_channel_unsupported",
        f"the scenario is not executable: its {channel} attack shape needs the "
        f"{_delivery_for(shape)} delivery, which the runtime contract does not list",
        {"channel": channel, "runtime_delivery": listed},
    )


def _planted_item_refusals(
    shape: dict[str, Any], runtime_contract: dict[str, Any], inventory: dict[str, Any]
) -> list[Finding]:
    findings = []
    carrier = shape["indirect"]["carrier_operation"]
    exposed = _operation_names(inventory)
    if carrier not in exposed:
        findings.append(_carrier_refusal(carrier, exposed))
    if choose_planted_target(shape, runtime_contract) is None:
        findings.append(_slot_refusal(shape, runtime_contract))
    return findings


def _operation_names(inventory: dict[str, Any]) -> list[str]:
    operations = inventory.get("operations")
    names = (
        [item.get("name") for item in operations if isinstance(item, dict)]
        if (isinstance(operations, list))
        else []
    )
    return [name for name in names if isinstance(name, str)]


def _carrier_refusal(carrier: str, exposed: list[str]) -> Finding:
    return _refusal(
        "shape_carrier_not_exposed",
        f"the scenario is not executable: its indirect attack shape plants text in what "
        f"{carrier} returns, and the package's inventory does not expose {carrier}",
        {"carrier_operation": carrier, "inventory_operations": exposed},
    )


def _slot_refusal(shape: dict[str, Any], runtime_contract: dict[str, Any]) -> Finding:
    details = unavailable_details(shape, runtime_contract)
    why = (
        "lists no seed slot for that carrier and content kind"
        if details["reason"] == "no_slot"
        else f"lists seed slot {details['slot']} without record {details['record_ref']}"
    )
    return _refusal(
        "shape_seed_slot_unavailable",
        f"the scenario is not executable: its indirect attack shape plants "
        f"{details['content_kind']} in {details['carrier_operation']}, "
        f"and the runtime contract {why}",
        details,
    )


def _turn_count_refusal(shape: dict[str, Any], limit: int) -> Finding:
    return _refusal(
        "shape_turn_count_unsupported",
        f"the scenario is not executable: its attack shape plans {shape['turn_count']} "
        f"user turns and the runtime contract allows {limit}",
        {"turn_count": shape["turn_count"], "max_planned_turns": limit},
    )
