"""Where a seed item goes: its runtime contract slot, as a MiDojo tool-output instruction.

The package's runtime contract (``inputs.json``'s ``runtime_contract``, else the plan's,
else the manifest's ``runtime_capabilities``) declares each seed slot: the carrier
operation whose result returns the field to the agent, the ``field`` in that result, the
``records`` whose field may hold planted text, the ``mode`` the target writes it with
(``append`` or ``replace``), and ``max_chars``. Each item of the package's
``artifact-seed-v1`` seed names a slot, one of its records and that slot's mode, and
becomes one instruction (``episode.tool_output_instruction``):

- the tool is the slot's carrier operation, and the path is the slot's field in the
  tool's structured result;
- the record selects the place in that result. A field inside a list of records
  (``notes[].body``) is reached through the list by the record's id (``["notes",
  "NOTE-1", "body"]``), on every call of the carrier; a field outside a list (``item``)
  is the result's own, on the carrier's first call;
- the mode is the slot's, the size limit its ``max_chars``, and the payload the item's
  text as the seed holds it.

MiDojo's selector names exact keys and positions. At f910063 it has no match by record
id, so it reports a path through a list by id as unmatched and leaves the result as it
was: the text lands where the slot declares it or nowhere.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from asago_artifact_generator.package_io import ArtifactPackage
from asago_bundle_core.errors import BundleError

from .episode import EVERY, FIRST, probe_key, tool_output_instruction

SEED_SCHEMA_VERSION = "artifact-seed-v1"
MODES = ("append", "replace")
LIST_MARK = "[]"
SLOT_TEXT = ("id", "carrier_operation", "field")


class PlacementFault(BundleError):
    """The seed, or the runtime contract slot it names, cannot be placed as declared."""


@dataclass(frozen=True)
class Segment:
    """One step of a slot's field: a key, and whether it holds a list of records."""

    name: str
    listed: bool


@dataclass(frozen=True)
class Placement:
    """Seed item ``index``: the text it plants, the slot it names and the record."""

    index: int
    slot: Mapping[str, Any]
    record: str
    content: str

    @property
    def carrier(self) -> str:
        return self.slot["carrier_operation"]

    @property
    def segments(self) -> tuple[Segment, ...]:
        return field_segments(self.slot["field"])

    @property
    def listed(self) -> bool:
        return any(segment.listed for segment in self.segments)

    @property
    def path(self) -> list[str]:
        """Return the field's path in the carrier's result, through a list by the record."""

        path = []
        for segment in self.segments:
            path.append(segment.name)
            if segment.listed:
                path.append(self.record)
        return path

    def instruction(self) -> dict[str, Any]:
        return tool_output_instruction(
            tool=self.carrier,
            path=self.path,
            payload=self.content,
            mode=self.slot["mode"],
            size_limit=self.slot["max_chars"],
            occurrence=EVERY if self.listed else FIRST,
            key=probe_key(self.index),
        )

    def entry(self) -> dict[str, Any]:
        """Return what the episode document says of this placement, in the contract's terms."""

        return {
            "instruction": self.index,
            "slot": self.slot["id"],
            "record": self.record,
            "carrier_operation": self.carrier,
            "field": self.slot["field"],
        }


def runtime_contract(package: ArtifactPackage) -> Mapping[str, Any]:
    """Return the runtime contract of ``inputs.json``, else the plan's, else the manifest's.

    This is the order package preparation reads it in.
    """

    for member in ("inputs.json", "plan.json"):
        document = _json(package, member)
        contract = document.get("runtime_contract") if isinstance(document, dict) else None
        if isinstance(contract, dict):
            return contract
    capabilities = package.manifest.runtime_capabilities
    return capabilities if isinstance(capabilities, dict) else {}


def seed_slots(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Return the contract's seed slots by id, each checked for what placing it needs."""

    slots = contract.get("seed_slots", [])
    if not isinstance(slots, list):
        raise PlacementFault("the runtime contract's seed_slots is not a list")
    found: dict[str, Mapping[str, Any]] = {}
    for position, slot in enumerate(slots):
        name = slot.get("id") if isinstance(slot, dict) else None
        label = f"seed slot {name!r}" if isinstance(name, str) else f"seed slot {position}"
        if not isinstance(slot, dict) or not all(
            isinstance(slot.get(key), str) and slot[key] for key in SLOT_TEXT
        ):
            raise PlacementFault(f"the runtime contract's {label} lacks {', '.join(SLOT_TEXT)}")
        records = slot.get("records")
        if not isinstance(records, list) or not all(isinstance(item, str) for item in records):
            raise PlacementFault(f"the runtime contract's {label} has no list of records")
        limit = slot.get("max_chars")
        if type(limit) is not int or limit < 1:
            raise PlacementFault(f"the runtime contract's {label} has no positive max_chars")
        if slot.get("mode") not in MODES:
            raise PlacementFault(f"the runtime contract's {label} has no mode of {list(MODES)}")
        field_segments(slot["field"])
        found[slot["id"]] = slot
    return found


def placements(seed: Any, slots: Mapping[str, Mapping[str, Any]]) -> list[Placement]:
    """Return one placement per seed item, in order; raise when an item disagrees with its slot."""

    if not isinstance(seed, dict) or seed.get("schema_version") != SEED_SCHEMA_VERSION:
        raise PlacementFault(f"the seed is not {SEED_SCHEMA_VERSION}")
    items = seed.get("items")
    if not isinstance(items, list) or not items:
        raise PlacementFault("the seed has no items")
    found = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise PlacementFault(f"seed item {index} has no content text")
        slot = slots.get(item.get("slot"))  # type: ignore[arg-type]
        if slot is None:
            raise PlacementFault(
                f"seed item {index}: the runtime contract has no seed slot {item.get('slot')!r}"
            )
        if seed.get("carrier_operation") != slot["carrier_operation"]:
            raise PlacementFault(
                f"seed item {index}: the seed's carrier {seed.get('carrier_operation')!r} is "
                f"not slot {slot['id']}'s {slot['carrier_operation']!r}"
            )
        if item.get("record_ref") not in slot["records"]:
            raise PlacementFault(
                f"seed item {index}: record {item.get('record_ref')!r} is not a record of "
                f"slot {slot['id']}"
            )
        if item.get("mode") != slot["mode"]:
            raise PlacementFault(
                f"seed item {index}: mode {item.get('mode')!r} is not slot {slot['id']}'s "
                f"{slot['mode']!r}"
            )
        content = item["content"]
        if not 0 < len(content) <= slot["max_chars"]:
            raise PlacementFault(
                f"seed item {index}: the text is {len(content)} characters; slot "
                f"{slot['id']} holds 1 to {slot['max_chars']}"
            )
        found.append(Placement(index, slot, item["record_ref"], content))
    return found


def field_segments(field: str) -> tuple[Segment, ...]:
    """Split ``notes[].body`` into its keys, marking each that holds a list of records."""

    segments = []
    for part in field.split("."):
        listed = part.endswith(LIST_MARK)
        name = part[: -len(LIST_MARK)] if listed else part
        if not name or "[" in name or "]" in name:
            raise PlacementFault(f"the seed slot field {field!r} is not keys joined by '.'")
        segments.append(Segment(name, listed))
    return tuple(segments)


def _json(package: ArtifactPackage, name: str) -> Any:
    content = package.members.get(name)
    if content is None:
        return None
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlacementFault(f"package {name} is not JSON: {exc}") from exc
