"""Paths the adapter's tests share; the planted packages are in ``injection_spec_cases``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from midojo_sdk.injection_spec_schema import (
    INJECTION_SPEC_DOCUMENT_V1,
    LEGACY_INJECTION_SPEC_DOCUMENT_V1,
)

from asago_injection_spec.injection_spec_compile import (
    INJECTION_SPEC_FILE,
    LEGACY_EPISODE_FILE,
    TOOL_REVISION_RANGE,
)

REPO = Path(__file__).resolve().parents[3]
RECEIPT_SCHEMA = REPO / "contracts/execution-receipt/execution-receipt-v2/schema.json"
DELIVERY_CONTRACT = REPO / "contracts/midojo-delivery"
# The range the recorded fixtures name, compiled at 8e8208f. Compile's minimum rose to
# 2a794f3 when the document gained the ID that revision reads; a recorded fixture keeps
# its bytes (see docs/midojo-changelog.md), so the comparison normalizes the range back.
RECORDED_TOOL_REVISION_RANGE = ">=8e8208f"


def receipt_payload(document: dict[str, Any]) -> dict[str, Any]:
    """Return a receipt without its self-identifying envelope.

    A receipt writer stamps the ``contract`` envelope beside the document; the version's
    schema describes the document alone, so raw schema checks read the payload.
    """

    return {key: value for key, value in document.items() if key != "contract"}


def as_current_slot_keys(item: Any) -> Any:
    """Return ``item`` with every ``probe_key`` renamed to ``slot_key``, recursively.

    A run recorded before term.5a seals a delivery fact, an exposure item and an injection
    outcome with ``probe_key``; the receipt always carries the current name. Comparing a
    receipt with a recorded document means renaming the recorded one.
    """

    if isinstance(item, list):
        return [as_current_slot_keys(entry) for entry in item]
    if isinstance(item, dict):
        renamed = {
            key: as_current_slot_keys(value) for key, value in item.items() if key != "probe_key"
        }
        if "probe_key" in item:
            renamed.setdefault("slot_key", as_current_slot_keys(item["probe_key"]))
        return renamed
    return item


def as_sealed(outcomes: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """A receipt's injection outcomes as MiDojo sealed them, without the placement parse
    adds to each (``payload_sha256`` and ``slot``)."""

    if outcomes is None:
        return None
    return [
        {key: value for key, value in outcome.items() if key not in ("payload_sha256", "slot")}
        for outcome in outcomes
    ]


def as_written(fixture: Path, package: Path) -> dict[str, bytes]:
    """One recorded fixture directory's compiled document, as compile writes it now.

    The recorded fixtures keep the document's pre-rename filename, ID and revision range
    (``episode.json``, ``midojo-episode-v1``, ``>=8e8208f``) as recorded;
    compile writes ``injection-spec.json`` holding ``midojo-injection-spec-v1`` and names
    the first revision that reads it. A recorded document also names the digest of the
    package it was compiled from, which changes whenever the manifest's ``input_kind``
    does; the comparison swaps in the manifest digest of ``package``, the package compile
    reads. The rest of the bytes are what a new compile writes, so the comparison stays
    byte for byte.
    """

    recorded = (fixture / LEGACY_EPISODE_FILE).read_bytes()
    recorded_digest = json.loads(recorded)["package_digest"]
    package_digest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))[
        "manifest_digest"
    ]
    return {
        INJECTION_SPEC_FILE: recorded.replace(
            LEGACY_INJECTION_SPEC_DOCUMENT_V1.encode(), INJECTION_SPEC_DOCUMENT_V1.encode()
        )
        .replace(RECORDED_TOOL_REVISION_RANGE.encode(), TOOL_REVISION_RANGE.encode())
        .replace(recorded_digest.encode(), package_digest.encode())
    }
