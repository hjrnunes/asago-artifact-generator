"""What the orchestrator's boundary services recorded for the package, read for ``parse``.

``mcp_capture/calls.jsonl`` holds the tool calls the MCP recording proxy saw and
``boundary/gateway-accounting.json`` the generation request count;
``boundary/judge-exchanges.jsonl`` holds one line per request the boundary judge
served (a reply package's rubric judge). With the
MiDojo call sink on, each recorded call also says whether the sink delivered it
to the control plane (``sink: ok|failed|no_token``). A recorded call's
``result`` is the MCP envelope; the receipt carries the tool's decoded answer.

``midojo_capture/calls.jsonl`` is the mini-agents server's own log (``--midojo-log``,
mini-agents ``kernel/midojo_mode.py``): one line per call with ``call_id``, ``function``
and the outcome of ``load``, ``start``, ``push`` and ``report``. ``report`` is
``unfinished`` for a call that started in MiDojo and never reached its report. A log
written before MiDojo started calls has no ``call_id`` or ``start``.
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
SERVER_CALLS = Path("midojo_capture/calls.jsonl")
GATEWAY_ACCOUNTING = Path("boundary/gateway-accounting.json")
JUDGE_EXCHANGES = Path("boundary/judge-exchanges.jsonl")
CALL_FIELDS = ("name", "arguments", "result", "error", "status")
SINK_DELIVERED = "ok"
LOAD_FAILURES = ("failed", "no_token")
UNFINISHED = "unfinished"


@dataclass(frozen=True)
class Records:
    """The boundary records of one package; ``calls`` is None when none were recorded.

    ``arrivals`` holds each proxy call's ``sequence``: the proxy numbers the calls in the
    order their requests arrive. Only a sequential receipt reads it.
    """

    calls: list[dict[str, Any]] | None = None
    request_count: int | None = None
    judge_count: int | None = None
    malformed: bool = False
    undelivered: bool = False
    server: list[dict[str, Any]] | None = None
    arrivals: list[Any] | None = None


def read_records(directory: Path) -> Records:
    accounting = read_json(directory / GATEWAY_ACCOUNTING)
    count = accounting.get("responses_request_count") if isinstance(accounting, dict) else None
    exchanges = directory / JUDGE_EXCHANGES
    counts: dict[str, Any] = {
        "request_count": count_value(count),
        "judge_count": len(lines(exchanges)) if exchanges.is_file() else None,
    }
    server_file = directory / SERVER_CALLS
    if server_file.is_file():
        server = [_raw_call(line) for line in lines(server_file)]
        if not all(isinstance(line, dict) for line in server):
            return Records(malformed=True, **counts)
        counts["server"] = server
        counts["undelivered"] = any(_server_failure(line) for line in server)
    calls_file = directory / PROXY_CALLS
    if not calls_file.is_file():
        return Records(**counts)
    raw = [_raw_call(line) for line in lines(calls_file)]
    if not all(isinstance(call, dict) for call in raw):
        return Records(**{**counts, "calls": [], "malformed": True})
    return Records(
        **{
            **counts,
            "calls": [proxy_call(call) for call in raw],
            "arrivals": [call.get("sequence") for call in raw],
            "undelivered": counts.get("undelivered", False) or _undelivered(raw),
        }
    )


def _server_failure(line: dict[str, Any]) -> bool:
    """Say whether the mini-agents server could not load, start, push or report for one call.

    An unfinished call (``report: unfinished``) is no failure: ``parse`` makes its
    attempt inconclusive.
    """

    return line.get("load") in LOAD_FAILURES or "failed" in (
        line.get("start"),
        line.get("push"),
        line.get("report"),
    )


def server_unfinished(line: dict[str, Any]) -> bool:
    """Say whether the server started one call in MiDojo and never reported it."""

    return line.get("report") == UNFINISHED


def _undelivered(raw: list[dict[str, Any]]) -> bool:
    """Say whether the sink was on and left a call out of the control plane."""

    if not any("sink" in call for call in raw):
        return False
    return any(call.get("sink") != SINK_DELIVERED for call in raw)


def proxy_call(call: dict[str, Any]) -> dict[str, Any]:
    """Return a recorded call in the receipt's shape, in turn 0.

    A sequential receipt replaces the turn with the one MiDojo stamped on the matching
    control-plane call (``parse.sequential_calls``).
    """

    fields = {key: call.get(key) for key in CALL_FIELDS}
    fields["result"], decoded_error = decoded_result(fields["result"])
    fields["error"] = error_text(fields["error"]) or decoded_error
    for key in ("name", "status"):
        fields[key] = text(fields[key])
    return {**fields, "turn_index": 0}


def native_call(call: dict[str, Any]) -> dict[str, Any]:
    """Return a control-plane function call in the receipt's shape, in turn 0.

    A sequential receipt replaces the turn as for ``proxy_call``. An unfinished call
    (``complete: false``) has no result and the status ``unfinished``.
    """

    return {
        "name": text(call.get("function")),
        "arguments": call.get("args"),
        "result": decoded(call.get("result")),
        "error": text(call.get("error")),
        "status": UNFINISHED if is_unfinished(call) else None,
        "turn_index": 0,
    }


def is_unfinished(call: dict[str, Any]) -> bool:
    """Say whether MiDojo holds a control-plane call as started and not finished.

    A record from a MiDojo that predates started calls has no ``complete`` and is finished.
    """

    return call.get("complete") is False


def _raw_call(line: str) -> Any:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None
