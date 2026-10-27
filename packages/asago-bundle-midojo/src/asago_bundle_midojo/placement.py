"""Where a seed item goes: its runtime contract slot, as a MiDojo tool-output instruction.

The package's runtime contract (``inputs.json``'s ``runtime_contract``, else the plan's,
else the manifest's ``runtime_capabilities``) declares each seed slot: the carrier
operation whose result returns the field to the agent, the ``field`` in that result, the
``records`` whose field may hold planted text, the ``mode`` the target writes it with
(``append`` or ``replace``), ``max_chars``, and the optional ``record_key``: the name
whose value is a record's ID. The caller's target config may declare the same slot's
``record_key`` in its ``runtime-spec.yaml`` (``declared.py``). Each item of the package's
``artifact-seed-v1`` seed names a slot, one of its records and that slot's mode, and
becomes one instruction (``episode.tool_output_instruction``):

- the tool is the slot's carrier operation, and the path is the slot's field in the
  tool's structured result;
- the record selects the place in that result, in one of two ways:

  - a field inside a list of records (``notes[].body``) is reached through the list by a
    record selector, MiDojo's ``{"field": <key>, "equals": <record>}`` step (``["notes",
    {"field": "note_id", "equals": "NOTE-1"}, "body"]``), which picks the one list item
    whose key field is the record. The ``record_key`` is a property of the list's items;
  - a field outside any list (``body``) belongs to the one record a single-record carrier
    returns, reached by the plain path into the result (``["body"]``). The carrier's
    output schema must declare the result and every key on the field's path above the
    field exactly as an object (``"type": "object"``; an array, a scalar, a union such as
    ``["object", "null"]``, or no type at all does not guarantee the path reaches the
    field). The instruction's target then names the record in one of two ways, the first
    the declarations allow:

    - when the carrier's input schema declares the ``record_key`` as a required text
      argument, the call names the record, and the target matches the call's arguments,
      ``call_arguments`` ``{<key>: <record>}`` (``{"note_id": "NOTE-2"}``);
    - otherwise, when the output schema declares the ``record_key`` as a text property of
      the result object itself (its top level, where MiDojo looks), the result names the
      record, and the target matches the result, ``result_record`` ``{"field": <key>,
      "equals": <record>}`` (``get_referral(patient_id)`` answering ``{"referral_id":
      "REF-1", ...}``: ``{"field": "referral_id", "equals": "REF-1"}``), with no
      ``call_arguments``. MiDojo compares the value type for type, so the record stays the
      text the seed names and the property must be declared text.

    A call naming another record, or none, or whose result holds another record, is left
    unchanged; MiDojo reports it ``unmatched`` at occurrence 0 and does not count it;

  the key is the slot's declared ``record_key``, used as declared; nothing infers it;
- every call of the carrier gets the instruction: the selector, or the call's arguments,
  picks the record;
- the mode is the slot's, the size limit its ``max_chars``, and the payload the item's
  text as the seed holds it.

A field the declarations do not make addressable either way is a capability gap
(``gap_reason``): a slot that declares no ``record_key``, a list field whose key the
carrier's output schema does not give the list's items as a text property, and a field
outside a list whose key the carrier's input schema does not declare as a required text
argument nor its result object as a text property. Placement is strict: a selector that
matches nothing leaves the result as it was, and MiDojo reports it unmatched.

A ``result_record`` target needs an interceptor that declares MiDojo's
``InterceptorCapability.result_record`` (asago/main from c0bee12). The episode intercepts
on the host only (``episode_compile.HOST_INTERCEPTION``), and MiDojo's host interceptor
(``MidojoMCP``) declares it, so the compiler models no other interceptor; the episode's
``tool_revision_range`` names the revision instead (``episode_compile``).
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

    ``record_key`` names the record, as the slot declares it (``locate`` sets it): the list
    item property for a field inside a list, the carrier's argument or its result's
    property for one outside, or None when no declaration names one. ``by_result`` is True
    when the result's property names the record (``locate`` sets it too): the target then
    matches the call's result, not its arguments.
    """

    index: int
    slot: Mapping[str, Any]
    record: str
    content: str
    record_key: str | None = None
    by_result: bool = False

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

    @property
    def call_arguments(self) -> dict[str, str]:
        """Return the arguments a call must have for its result to be the record.

        Empty for a list field, whose path selects the record, and for one the result
        names; ``{record_key: record}`` for any other field outside a list. Raises
        ``PlacementFault`` for one whose record key is not set.
        """

        if self.listed or self.by_result:
            return {}
        if self.record_key is None:
            raise PlacementFault(f"seed item {self.index}: no record key to match the call by")
        return {self.record_key: self.record}

    @property
    def result_record(self) -> dict[str, str] | None:
        """Return the record selector a call's result must match, or None.

        ``{"field": record_key, "equals": record}`` for a field the result names the record
        of (``by_result``); None for any other.
        """

        if not self.by_result:
            return None
        if self.record_key is None:
            raise PlacementFault(f"seed item {self.index}: no record key to match the result by")
        return {"field": self.record_key, "equals": self.record}

    def instruction(self) -> dict[str, Any]:
        return tool_output_instruction(
            tool=self.carrier,
            path=self.path,
            call_arguments=self.call_arguments,
            result_record=self.result_record,
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
        key = slot.get("record_key")
        if key is not None and not (isinstance(key, str) and key):
            raise PlacementFault(
                f"the runtime contract's {label} has a record_key that is not text"
            )
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
    over MCP, at a text field the result declares, in the record a selector or the call's
    arguments pick, within a size limit counted in UTF-8 bytes. A carrier the target does
    not list (the agent reads it by some other way than an MCP call), a result that is not
    a JSON object, a field the result does not declare as text, a slot that declares no
    ``record_key``, a list field whose ``record_key`` the list's items do not declare as
    text, a field outside a list below a key the result does not declare exactly as an
    object, a field outside a list whose ``record_key`` the carrier declares neither as a
    required text argument nor as a text property of its result, and a text over
    ``max_chars`` bytes cannot be placed. Call it on a placement ``locate`` returned.
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
    if not placement.listed and not _through_objects(schema, segments):
        return (
            f"{label}: {carrier!r}'s result does not declare every key above {field!r} as "
            'an object ("type": "object"), so its path is not sure to reach the field'
        )
    reason = (
        _list_key_reason(placement, schema)
        if placement.listed
        else _single_record_reason(placement, tool, schema)
    )
    if reason:
        return f"{label}: {reason}"
    size = len(placement.content.encode("utf-8"))
    if size > placement.slot["max_chars"]:
        return (
            f"{label}: the text is {size} UTF-8 bytes; MiDojo's size limit counts bytes, and "
            f"slot {placement.slot['id']} holds {placement.slot['max_chars']}"
        )
    return None


def _list_key_reason(placement: Placement, schema: Mapping[str, Any]) -> str | None:
    """Why the list's items do not name the record by the slot's ``record_key``, or None."""

    slot, field, carrier = placement.slot["id"], placement.slot["field"], placement.carrier
    key = placement.record_key
    if key is None:
        return (
            f"slot {slot} declares no record_key, the property of the items of {field!r} "
            "that names a record, which MiDojo selects the record by"
        )
    declared_type = _item_property(schema, placement.segments, key)
    if declared_type is None:
        return (
            f"{carrier!r}'s result declares no property {key!r} (slot {slot}'s record_key) "
            f"for the items of {field!r}"
        )
    if declared_type != "string":
        return (
            f"{carrier!r}'s result declares {key!r} (slot {slot}'s record_key) for the items "
            f"of {field!r} as {declared_type!r}, not text"
        )
    return None


def _single_record_reason(
    placement: Placement, tool: Mapping[str, Any], schema: Mapping[str, Any]
) -> str | None:
    """Why neither the call nor the result of the carrier names the one record, or None.

    The call's argument comes first; when it does not name the record, the result's
    property may. Both reasons are given when neither does.
    """

    if placement.by_result:
        return _result_key_reason(placement, schema)
    argument = _argument_key_reason(placement, tool)
    if argument is None or placement.record_key is None:
        return argument
    result = _result_key_reason(placement, schema)
    return f"{argument}; {result}" if result else None


def _result_key_reason(placement: Placement, schema: Mapping[str, Any]) -> str | None:
    """Why the carrier's result object does not name its record by ``record_key``, or None.

    MiDojo matches a ``result_record`` on the result's structured content object itself, by
    the property's value, type for type: the output schema must declare ``record_key`` a
    text property at its top level.
    """

    slot, carrier, key = placement.slot["id"], placement.carrier, placement.record_key
    properties = schema.get("properties")
    value = properties.get(key) if isinstance(properties, dict) and key is not None else None
    if not isinstance(value, dict):
        return (
            f"{carrier!r}'s result object declares no property {key!r} (slot {slot}'s "
            "record_key) either, by which MiDojo would match the result"
        )
    declared_type = value.get("type", "untyped")
    if declared_type != "string":
        return (
            f"{carrier!r}'s result declares {key!r} (slot {slot}'s record_key) as "
            f"{declared_type!r}, not text"
        )
    return None


def _argument_key_reason(placement: Placement, tool: Mapping[str, Any]) -> str | None:
    """Why a call of the carrier does not name the one record it returns, or None.

    The slot's ``record_key`` must be a text argument the carrier's input schema declares
    and requires: MiDojo matches the call by that argument's value, type for type.
    """

    slot, field, carrier = placement.slot["id"], placement.slot["field"], placement.carrier
    key = placement.record_key
    if key is None:
        return (
            f"{field!r} is outside a list, and slot {slot} declares no record_key, the "
            f"argument of {carrier!r} that names the one record its result holds, which "
            "MiDojo matches the call by"
        )
    inputs = tool.get("inputSchema")
    properties = inputs.get("properties") if isinstance(inputs, dict) else None
    argument = properties.get(key) if isinstance(properties, dict) else None
    if not isinstance(argument, dict):
        return (
            f"{carrier!r} declares no argument {key!r} (slot {slot}'s record_key), so no "
            f"call names the record whose {field!r} it returns"
        )
    declared_type = argument.get("type", "untyped")
    if declared_type != "string":
        return (
            f"{carrier!r} declares its argument {key!r} (slot {slot}'s record_key) as "
            f"{declared_type!r}, not text"
        )
    required = inputs.get("required") if isinstance(inputs, dict) else None
    if not isinstance(required, list) or key not in required:
        return (
            f"{carrier!r} does not require its argument {key!r} (slot {slot}'s record_key), "
            f"so a call need not name the record whose {field!r} it returns"
        )
    return None


def locate(placement: Placement, declared: DeclaredTools) -> Placement:
    """Return ``placement`` with its slot's ``record_key``, as the declarations state it.

    The key is the package runtime contract's slot ``record_key``, else the target
    config's (``DeclaredTools.record_keys``). When both state one they must agree, or
    ``PlacementFault`` is raised: the package was authored against another declaration.
    A field outside a list is matched by the call's result (``by_result``) when the
    carrier does not take the key as a required text argument and its result object
    declares it a text property; ``gap_reason`` says why when neither holds.
    """

    slot_id = placement.slot["id"]
    stated = placement.slot.get("record_key")
    target = declared.record_keys.get(slot_id)
    if stated is not None and target is not None and stated != target:
        raise PlacementFault(
            f"seed item {placement.index}: slot {slot_id}'s record_key is {stated!r} in the "
            f"runtime contract and {target!r} in the target config"
        )
    located = replace(placement, record_key=stated if stated is not None else target)
    tool = declared.tools.get(located.carrier)
    if located.listed or located.record_key is None or tool is None:
        return located
    schema = tool.get("outputSchema")
    if (
        not isinstance(schema, dict)
        or _argument_key_reason(located, tool) is None
        or _result_key_reason(located, schema) is not None
    ):
        return located
    return replace(located, by_result=True)


def _item_property(
    schema: Mapping[str, Any], segments: tuple[Segment, ...], name: str
) -> str | None:
    """Return the declared type of property ``name`` of the list's items, or None.

    ``"untyped"`` when the property is declared with no type. Call it on a field
    ``_declares_text`` accepts.
    """

    node: Any = schema
    for segment in segments:
        node = node["properties"][segment.name]
        if segment.listed:
            properties = node["items"].get("properties") or {}
            value = properties.get(name)
            if not isinstance(value, dict):
                return None
            return value.get("type", "untyped")
    return None


def _through_objects(schema: Mapping[str, Any], segments: tuple[Segment, ...]) -> bool:
    """Return whether the result and each key above the field are declared exactly objects.

    Only ``"type": "object"`` counts: an array, a scalar, a union of types (a nullable
    object included) or an untyped node may hold no such key when the call returns. Call it
    on a field outside any list that ``_declares_text`` accepts.
    """

    node: Any = schema
    for segment in segments:
        if not isinstance(node, dict) or node.get("type") != "object":
            return False
        node = node["properties"][segment.name]
    return True


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
