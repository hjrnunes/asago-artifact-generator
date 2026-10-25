"""Paths the adapter's tests share; the planted packages are in ``episode_cases``."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
RECEIPT_SCHEMA = REPO / "contracts/execution-receipt/execution-receipt-v1.schema.json"
