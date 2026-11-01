"""Paths the adapter's tests share; the planted packages are in ``injection_spec_cases``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from midojo_sdk.injection_spec_schema import (
    INJECTION_SPEC_DOCUMENT_V1,
    LEGACY_INJECTION_SPEC_DOCUMENT_V1,
)

from asago_bundle_midojo.injection_spec_compile import INJECTION_SPEC_FILE, LEGACY_EPISODE_FILE

REPO = Path(__file__).resolve().parents[3]
RECEIPT_SCHEMA = REPO / "contracts/execution-receipt/execution-receipt-v1.schema.json"
DELIVERY_CONTRACT = REPO / "contracts/midojo-delivery"


def as_sealed(outcomes: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """A receipt's injection outcomes as MiDojo sealed them, without the placement parse
    adds to each (``payload_sha256`` and ``slot``)."""

    if outcomes is None:
        return None
    return [
        {key: value for key, value in outcome.items() if key not in ("payload_sha256", "slot")}
        for outcome in outcomes
    ]


def as_written(fixture: Path) -> dict[str, bytes]:
    """One recorded fixture directory's compiled document, as compile writes it now.

    The recorded fixtures keep the document's pre-rename filename and ID
    (``episode.json``, ``midojo-episode-v1``) until M9 regenerates them; compile writes
    ``injection-spec.json`` holding ``midojo-injection-spec-v1``. The rest of the bytes are
    what a new compile writes, so the comparison stays byte for byte.
    """

    recorded = (fixture / LEGACY_EPISODE_FILE).read_bytes()
    return {
        INJECTION_SPEC_FILE: recorded.replace(
            LEGACY_INJECTION_SPEC_DOCUMENT_V1.encode(), INJECTION_SPEC_DOCUMENT_V1.encode()
        )
    }
