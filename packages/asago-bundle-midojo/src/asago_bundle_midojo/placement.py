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
  (``notes[].body``) is reached through the list by a record selector, MiDojo's
  ``{"field": <key>, "equals": <record>}`` step (``["notes", {"field": "note_id",
  "equals": "NOTE-1"}, "body"]``), which picks the one list item whose key field is the
  record. The key is the one text property the carrier's declared output schema gives the
  list's items besides the property the field goes on into (``note_id`` beside ``body``);
- every call of the carrier gets the instruction: the selector, not the call, picks
  the record;
- the mode is the slot's, the size limit its ``max_chars``, and the payload the item's
  text as the seed holds it.

MiDojo selects by the result, never by the call's arguments, and only a list item can be
selected by its key. So a field outside a list is placed only when the declarations
leave the carrier no other record to return: it takes no arguments (its input schema
declares no property) and the slot names exactly one record. Any other field outside a
list, and a list whose record key the schema does not settle, is a capability gap
(``gap_reason``). Placement is strict: a selector that matches nothing leaves the result
as it was, and MiDojo reports it unmatched.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from asago_artifact_generator.package_io import ArtifactPackage
from asago_bundle_core.errors import BundleError

from .declared import DeclaredTools
from .episode import EVERY, probe_key, tool_output_instruction

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
    """Seed item ``index``: the text it plants, the slot it names and the record.

    ``record_key`` is the field of a list item that names its record (``locate`` sets it
    from the target's declarations), or None for a field outside a list.
    """

    index: int
    slot: Mapping[str, Any]
    record: str
    content: str
    record_key: str | None = None

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
    def path(self) -> list[str | dict[str, str]]:
        """Return the field's path in the carrier's result, through a list by the record.

        Raises ``PlacementFault`` for a list field whose record key is not set.
        """

        path: list[str | dict[str, str]] = []
        for segment in self.segments:
            path.append(segment.name)
            if segment.listed:
                if self.record_key is None:
                    raise PlacementFault(f"seed item {self.index}: no record key to select by")
                path.append({"field": self.record_key, "equals": self.record})
        return path

    def instruction(self) -> dict[str, Any]:
        return tool_output_instruction(
            tool=self.carrier,
            path=self.path,
            payload=self.content,
            mode=self.slot["mode"],
            size_limit=self.slot["max_chars"],
            occurrence=EVERY,
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
            "record_key": self.record_key,
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


def gap_reason(placement: Placement, declared: DeclaredTools) -> str | None:
    """Return why MiDojo cannot place the item in the carrier's MCP result, or None.

    MiDojo's interceptor places text in the structured result of a tool the target lists
    over MCP, at a text field the result declares, in the record a selector picks, within
    a size limit counted in UTF-8 bytes. A carrier the target does not list (the agent
    reads it by some other way than an MCP call), a result that is not a JSON object, a
    field the result does not declare, a list whose items do not declare one key field
    (``locate``), a field outside a list of a carrier that can return another
    record (``single_record``), and a text over ``max_chars`` bytes cannot be placed.
    """

    label = f"seed item {placement.index}"
    carrier = placement.carrier
    tool = declared.tools.get(carrier)
    if tool is None:
        return (
            f"{label}: the carrier {carrier!r} is not an MCP tool of the target; MiDojo "
            "places planted text only in an MCP tool result"
        )
    schema = tool.get("outputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return (
            f"{label}: {carrier!r} declares no JSON object result; MiDojo places planted "
            "text at a field of a structured tool result"
        )
    field = placement.slot["field"]
    segments = placement.segments
    if sum(segment.listed for segment in segments) > 1 or segments[-1].listed:
        return f"{label}: the field {field!r} does not name one text field of one record"
    if not _declares_text(schema, segments):
        return f"{label}: {carrier!r}'s result declares no text field {field!r}"
    if placement.listed:
        candidates = _key_candidates(schema, segments)
        if len(candidates) != 1:
            return (
                f"{label}: MiDojo selects a list record by the one field that names it, and "
                f"{carrier!r}'s result declares {len(candidates)} text fields beside the "
                f"planted one for the items of {field!r} ({', '.join(candidates) or 'none'})"
            )
    elif not single_record(tool, placement.slot):
        return (
            f"{label}: MiDojo selects by the result, not the call's arguments, and "
            f"{field!r} is outside a list: a call of {carrier!r} may return another record "
            f"than {placement.record!r} (the carrier takes arguments, or the slot names more "
            "than one record)"
        )
    size = len(placement.content.encode("utf-8"))
    if size > placement.slot["max_chars"]:
        return (
            f"{label}: the text is {size} UTF-8 bytes; MiDojo's size limit counts bytes, and "
            f"slot {placement.slot['id']} holds {placement.slot['max_chars']}"
        )
    return None


def locate(placement: Placement, declared: DeclaredTools) -> Placement:
    """Return ``placement`` with the record key of its list field, from the carrier's schema.

    Call it on a placement ``gap_reason`` accepts; a field outside a list keeps None.
    """

    if not placement.listed:
        return placement
    schema = declared.tools[placement.carrier]["outputSchema"]
    (key,) = _key_candidates(schema, placement.segments)
    return replace(placement, record_key=key)


def single_record(tool: Mapping[str, Any], slot: Mapping[str, Any]) -> bool:
    """Return whether every call of ``tool`` can only return the slot's one record.

    The declarations prove it when the tool's input schema declares no property (no call
    can ask for another record) and the slot names exactly one record.
    """

    inputs = tool.get("inputSchema")
    properties = inputs.get("properties") if isinstance(inputs, dict) else None
    return not properties and len(slot["records"]) == 1


def _key_candidates(schema: Mapping[str, Any], segments: tuple[Segment, ...]) -> list[str]:
    """Return the text properties of the list's items, beside the one the field goes into."""

    node: Any = schema
    for position, segment in enumerate(segments):
        node = node["properties"][segment.name]
        if segment.listed:
            items = node["items"]
            onward = segments[position + 1].name
            properties = items.get("properties") if isinstance(items, dict) else None
            return sorted(
                name
                for name, value in (properties or {}).items()
                if name != onward and isinstance(value, dict) and value.get("type") == "string"
            )
    return []


def _declares_text(schema: Mapping[str, Any], segments: tuple[Segment, ...]) -> bool:
    """Return whether ``schema`` declares a text value at ``segments``, through any list."""

    node: Any = schema
    for segment in segments:
        properties = node.get("properties") if isinstance(node, dict) else None
        node = properties.get(segment.name) if isinstance(properties, dict) else None
        if segment.listed:
            if not isinstance(node, dict) or node.get("type") != "array":
                return False
            node = node.get("items")
    return isinstance(node, dict) and node.get("type") == "string"


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
