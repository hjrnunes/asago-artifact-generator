"""Where an indirect shape plants its text: the seed slot and record, chosen by code.

The model writes the planted text and the benign turns. Code alone picks the slot
and the record, from the runtime contract's ``seed_slots`` and the shape's carrier,
content kind and record, so the package cannot name a slot the target lacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# A policy document is looked up by words in its original text. Appending keeps
# the lookup working; replacing the body can make the carrier stop returning it.
APPEND_CONTENT_KINDS = frozenset({"policy_document"})
_SLOT_TEXT_FIELDS = ("id", "carrier_operation", "field", "controller", "content_kind")


@dataclass(frozen=True)
class PlantedTarget:
    """The slot and record a package plants its text in, and how."""

    slot: str
    carrier_operation: str
    field: str
    content_kind: str
    controller: str
    record_ref: str
    mode: str


def seed_slots(runtime_contract: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the runtime contract's well-formed seed slots, in their listed order."""

    listed = runtime_contract.get("seed_slots")
    return [slot for slot in listed if _well_formed(slot)] if isinstance(listed, list) else []


def _well_formed(slot: Any) -> bool:
    return (
        isinstance(slot, dict)
        and all(isinstance(slot.get(key), str) for key in _SLOT_TEXT_FIELDS)
        and isinstance(slot.get("records"), list)
        and all(isinstance(record, str) for record in slot["records"])
    )


def slots_for_shape(
    shape: dict[str, Any], runtime_contract: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return the slots that carry the shape's operation and content kind."""

    indirect = shape["indirect"]
    return [
        slot
        for slot in seed_slots(runtime_contract)
        if slot["carrier_operation"] == indirect["carrier_operation"]
        and slot["content_kind"] == indirect["data_item"]["content_kind"]
    ]


def _record_in(slot: dict[str, Any], record_ref: str | None) -> str | None:
    """Return the record to plant in ``slot``: the named one if listed, else the first."""

    records = slot["records"]
    if record_ref is None:
        return records[0] if records else None
    return record_ref if record_ref in records else None


def choose_planted_target(
    shape: dict[str, Any], runtime_contract: dict[str, Any]
) -> PlantedTarget | None:
    """Return the slot and record that carry an indirect shape, or None when none does."""

    if shape["indirect"] is None:
        return None
    record_ref = shape["indirect"]["data_item"]["record_ref"]
    for slot in slots_for_shape(shape, runtime_contract):
        record = _record_in(slot, record_ref)
        if record is not None:
            return _target(slot, record)
    return None


def _target(slot: dict[str, Any], record: str) -> PlantedTarget:
    return PlantedTarget(
        slot=slot["id"],
        carrier_operation=slot["carrier_operation"],
        field=slot["field"],
        content_kind=slot["content_kind"],
        controller=slot["controller"],
        record_ref=record,
        mode="append" if slot["content_kind"] in APPEND_CONTENT_KINDS else "replace",
    )


def unavailable_details(shape: dict[str, Any], runtime_contract: dict[str, Any]) -> dict[str, Any]:
    """Return why no slot carries the shape, for the refusal that reports it."""

    indirect = shape["indirect"]
    details: dict[str, Any] = {
        "carrier_operation": indirect["carrier_operation"],
        "content_kind": indirect["data_item"]["content_kind"],
        "record_ref": indirect["data_item"]["record_ref"],
    }
    matching = slots_for_shape(shape, runtime_contract)
    if not matching:
        every = [
            slot["id"]
            for slot in seed_slots(runtime_contract)
            if slot["carrier_operation"] == indirect["carrier_operation"]
        ]
        return {**details, "reason": "no_slot", "slots_for_carrier": every}
    return {
        **details,
        "reason": "record_not_listed",
        "slot": matching[0]["id"],
        "listed_records": list(matching[0]["records"]),
    }
