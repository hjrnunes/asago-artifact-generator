"""MiDojo's snapshot ID, recomputed from the snapshot as ``GET .../snapshot`` returns it.

MiDojo names a sealed snapshot by its own content digest (asago/main a5f81e9 and 3824922,
``src/midojo/control_plane/state.py``: ``snapshot_digest``, ``_canonical`` and
``_dumps``): ``sha256:<hex>`` of a lossless JSON encoding of everything in the
snapshot but its ID. This module ports that encoding for the values a JSON snapshot
holds: ``str``, ``int``, ``bool`` and None are themselves; a float is
``["float", <its IEEE-754 bits as big-endian hex>]``; a list is ``["list", [items]]``;
an object is ``["mapping", [[key, value], ...]]`` with its items sorted by their
encoding, written compactly (``separators=(",", ":")``, ASCII escapes). MiDojo encodes
the environments, call records and observations as their Python ``model_dump()``, the
turns and capture failures as their dataclass fields, and those are the objects of the
same names in the JSON form, field for field: a call record sealed from 3824922 with its
``structured_content`` and ``injection_outcomes``, one sealed before without them.

From a19562e an injection outcome may say what it placed and where (``payload_digest``,
``placed_digest``, ``slot``), and MiDojo encodes each of those only when it is set
(``state._call_content``): a None one is left out, so a snapshot without them keeps the ID
it had before they existed. The JSON form holds each as null, or not at all when sealed
before; both encode the same.

A value JSON does not carry back as the type MiDojo encoded (bytes, a date, a tuple or
set, a non-finite float, a mapping with keys that are not text, in a field MiDojo does
not type) gives a different digest, so the snapshot is refused rather than renamed:
MiDojo's own ``snapshot_from_response`` (``control_plane/client.py``) refuses such a
round trip too. The values a planted-item episode holds are all JSON values.
``tests/fixtures/episode/`` holds the IDs and encodings MiDojo computed, and the tests
require this port to reproduce them.
"""

from __future__ import annotations

import hashlib
import json
import struct
from typing import Any

# The snapshot's content, in ``snapshot_digest``'s order: every field but ``snapshot_id``.
CONTENT_FIELDS = (
    "evaluation_id",
    "run_id",
    "user_task_id",
    "injection_task_id",
    "agent_output",
    "pre_environment",
    "environment",
    "function_calls",
    "turns",
    "observations",
    "evidence_status",
    "sealed_at",
    "capture_failures",
)


# The fields an injection outcome gained after snapshots were first digested
# (``state._LATER_OUTCOME_FIELDS``): each is encoded only when set.
LATER_OUTCOME_FIELDS = ("payload_digest", "placed_digest", "slot")


class DigestFault(ValueError):
    """A value no JSON document holds: the snapshot was not read from JSON."""


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    """Return ``sha256:<hex>`` of the snapshot's content, as MiDojo's ``snapshot_digest``.

    Every field of ``CONTENT_FIELDS`` must be present (``KeyError`` otherwise).
    """

    content = {name: snapshot[name] for name in CONTENT_FIELDS}
    content["function_calls"] = [call_content(call) for call in content["function_calls"]]
    return f"sha256:{hashlib.sha256(dumps(canonical(content)).encode()).hexdigest()}"


def call_content(call: Any) -> Any:
    """Return a call record as the digest encodes it (``state._call_content``): each injection
    outcome without a later field (``LATER_OUTCOME_FIELDS``) that is None. ``call`` itself is
    left as it is."""

    if not isinstance(call, dict) or not isinstance(call.get("injection_outcomes"), list):
        return call
    outcomes = [
        {
            key: value
            for key, value in outcome.items()
            if not (key in LATER_OUTCOME_FIELDS and value is None)
        }
        if isinstance(outcome, dict)
        else outcome
        for outcome in call["injection_outcomes"]
    ]
    return {**call, "injection_outcomes": outcomes}


def canonical(value: Any) -> Any:
    """Return MiDojo's lossless encoding of a JSON value (``state._canonical``)."""

    kind = type(value)
    if value is None or kind in (str, int, bool):
        return value
    if kind is float:
        return ["float", struct.pack(">d", value).hex()]
    if kind is list:
        return ["list", [canonical(item) for item in value]]
    if kind is dict:
        items = [[canonical(key), canonical(item)] for key, item in value.items()]
        return ["mapping", sorted(items, key=dumps)]
    raise DigestFault(f"a {kind.__name__} is not a JSON value")


def dumps(encoded: Any) -> str:
    """Return the text MiDojo hashes and sorts by (``state._dumps``)."""

    return json.dumps(encoded, separators=(",", ":"))
