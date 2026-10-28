"""Paths the adapter's tests share; the planted packages are in ``episode_cases``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
