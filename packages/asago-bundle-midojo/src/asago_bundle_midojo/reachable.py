"""Whether the episode's conversation can lead the agent to the record a seed item plants in.

An episode sends fixed user turns, and the interceptor plants an item only in the result of
a call of its carrier that reaches the record. A carrier matched by its arguments
(``Placement.call_arguments``: a field outside any list whose carrier requires the slot's
``record_key`` as a text argument) returns the record only to a call that names it, so the
agent must know the record's ID. This is a structural reference check of how it can learn
it, from the declarations alone; the conversation reaches the record when either holds:

- a user turn names the record: the record's ID appears in a turn as it is written, case
  and all, not inside a longer run of letters, digits, ``-`` or ``_`` (``NOTE-2`` is named
  by "Read NOTE-2, please." and not by "Read NOTE-20" or "read note-2"), because MiDojo
  matches the call's argument to the record exactly;
- an allowed tool that requires no argument declares the slot's ``record_key`` as a text
  property in its output schema, at any depth through ``properties`` and an array's
  ``items`` (piggybank's ``list_notes`` returns ``notes[].note_id``), so the agent can
  list the IDs and call the carrier with one.

Anything else is a capability gap: an episode whose agent cannot name the record calls the
carrier with no argument that matches, or not at all, and the item is never delivered.

The check does not judge meaning. It does not read whether a turn asks for the record,
only whether it names it; it does not follow a chain of calls (a tool whose required
argument the conversation would have to supply first); it does not know whether a listing
tool returns this record, only that it declares its key; and an ID the agent's own system
prompt holds is not seen. A field inside a list, and a field whose result names the record
(``by_result``), are not checked: the interceptor selects the record in the result of any
call of the carrier, and which call returns it depends on argument values (a search query,
a patient's ID) that the declarations do not tie to the record.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .declared import DeclaredTools
from .placement import Placement

# A character that continues an identifier: a record named inside a longer run is not named.
_ID_CHARACTER = r"A-Za-z0-9_\-"


def unreachable_reason(
    placement: Placement, turns: list[str], declared: DeclaredTools, allowed: Iterable[str]
) -> str | None:
    """Return why the conversation cannot lead the agent to the placement's record, or None.

    ``turns`` are the episode's user turns and ``allowed`` the tools the agent may call.
    Call it on a placement ``gap_reason`` accepts.
    """

    if placement.listed or placement.by_result or placement.record_key is None:
        return None
    record, key, carrier = placement.record, placement.record_key, placement.carrier
    if any(names_record(turn, record) for turn in turns):
        return None
    if listing_tools(declared, allowed, key):
        return None
    return (
        f"seed item {placement.index}: the conversation cannot reach record {record!r}: "
        f"{carrier!r} returns it only to a call whose argument {key!r} names it, no user "
        f"turn names {record!r}, and no allowed tool that takes no argument declares "
        f"{key!r} in its result, so the agent cannot learn the record to ask {carrier!r} for"
    )


def names_record(turn: str, record: str) -> bool:
    """Return whether ``turn`` holds ``record`` as written, not inside a longer identifier."""

    pattern = rf"(?<![{_ID_CHARACTER}]){re.escape(record)}(?![{_ID_CHARACTER}])"
    return re.search(pattern, turn) is not None


def listing_tools(declared: DeclaredTools, allowed: Iterable[str], key: str) -> list[str]:
    """Return the allowed tools that take no required argument and declare text ``key``."""

    return sorted(
        name
        for name in allowed
        if (tool := declared.tools.get(name)) is not None
        and not _required(tool)
        and _declares_text_property(tool.get("outputSchema"), key)
    )


def _required(tool: Mapping[str, Any]) -> list[Any]:
    inputs = tool.get("inputSchema")
    required = inputs.get("required") if isinstance(inputs, dict) else None
    return required if isinstance(required, list) else []


def _declares_text_property(node: Any, key: str) -> bool:
    """Return whether ``node`` declares a text property ``key`` through properties and items."""

    if not isinstance(node, dict):
        return False
    properties = node.get("properties")
    if isinstance(properties, dict):
        value = properties.get(key)
        if isinstance(value, dict) and value.get("type") == "string":
            return True
        if any(_declares_text_property(child, key) for child in properties.values()):
            return True
    return _declares_text_property(node.get("items"), key)
