"""Reporting adapters for rich generated-detector results."""

from __future__ import annotations

from typing import Any


def garak_value(value: Any) -> int | None:
    """Map a completed rich outcome to Garak's 1/0/None reporting values.

    Runtime failures do not have a detector outcome and therefore map to
    ``None`` rather than to a safe ``0`` result.
    """

    result = value
    if hasattr(value, "status"):
        if getattr(value, "status", None) != "completed":
            return None
        result = getattr(value, "result", None)
    elif isinstance(value, dict) and "status" in value:
        if value.get("status") != "completed":
            return None
        result = value.get("result")
    if not isinstance(result, dict):
        return None
    outcome = result.get("outcome")
    return {"detected": 1, "not_detected": 0, "inconclusive": None}.get(outcome)


map_to_garak = garak_value
map_detector_result = garak_value
to_garak_value = garak_value

__all__ = ["garak_value", "map_detector_result", "map_to_garak", "to_garak_value"]
