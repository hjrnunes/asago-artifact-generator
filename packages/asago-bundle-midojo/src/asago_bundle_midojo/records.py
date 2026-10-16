"""What orch's boundary services recorded for the package, read for ``parse``.

``mcp_capture/calls.jsonl`` holds the tool calls the MCP recording proxy saw and
``boundary/gateway-accounting.json`` the generation request count;
``boundary/judge-exchanges.jsonl`` holds one line per request the boundary judge
served (a reply package's rubric judge). With the
MiDojo call sink on, each recorded call also says whether the sink delivered it
to the control plane (``sink: ok|failed|no_token``). A recorded call's
``result`` is the MCP envelope; the receipt carries the tool's decoded answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asago_bundle_core.mcp_records import (
    count_value,
    decoded,
    decoded_result,
    error_text,
    lines,
    read_json,
    text,
)

PROXY_CALLS = Path("mcp_capture/calls.jsonl")
GATEWAY_ACCOUNTING = Path("boundary/gateway-accounting.json")
JUDGE_EXCHANGES = Path("boundary/judge-exchanges.jsonl")
CALL_FIELDS = ("name", "arguments", "result", "error", "status")
SINK_DELIVERED = "ok"


@dataclass(frozen=True)
class Records:
    """The boundary records of one package; ``calls`` is None when none were recorded."""

    calls: list[dict[str, Any]] | None = None
    request_count: int | None = None
    judge_count: int | None = None
    malformed: bool = False
    undelivered: bool = False


def read_records(directory: Path) -> Records:
    accounting = read_json(directory / GATEWAY_ACCOUNTING)
    count = accounting.get("responses_request_count") if isinstance(accounting, dict) else None
    exchanges = directory / JUDGE_EXCHANGES
    counts = {
        "request_count": count_value(count),
        "judge_count": len(lines(exchanges)) if exchanges.is_file() else None,
    }
    calls_file = directory / PROXY_CALLS
    if not calls_file.is_file():
        return Records(**counts)
    raw = [_raw_call(line) for line in lines(calls_file)]
    if not all(isinstance(call, dict) for call in raw):
        return Records(calls=[], malformed=True, **counts)
    return Records(
        calls=[proxy_call(call) for call in raw], undelivered=_undelivered(raw), **counts
    )


def _undelivered(raw: list[dict[str, Any]]) -> bool:
    """Say whether the sink was on and left a call out of the control plane."""

    if not any("sink" in call for call in raw):
        return False
    return any(call.get("sink") != SINK_DELIVERED for call in raw)


def proxy_call(call: dict[str, Any]) -> dict[str, Any]:
    """Return a recorded call in the receipt's shape; every call belongs to turn 0."""

    fields = {key: call.get(key) for key in CALL_FIELDS}
    fields["result"], decoded_error = decoded_result(fields["result"])
    fields["error"] = error_text(fields["error"]) or decoded_error
    for key in ("name", "status"):
        fields[key] = text(fields[key])
    return {**fields, "turn_index": 0}


def native_call(call: dict[str, Any]) -> dict[str, Any]:
    """Return a control-plane function call in the receipt's shape."""

    return {
        "name": text(call.get("function")),
        "arguments": call.get("args"),
        "result": decoded(call.get("result")),
        "error": text(call.get("error")),
        "status": None,
        "turn_index": 0,
    }


def _raw_call(line: str) -> Any:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None
