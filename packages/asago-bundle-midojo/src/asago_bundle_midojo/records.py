"""What orch's boundary services recorded for the package, read for ``parse``.

``mcp_capture/calls.jsonl`` holds the tool calls the MCP recording proxy saw and
``boundary/gateway-accounting.json`` the generation request count. With the
MiDojo call sink on, each recorded call also says whether the sink delivered it
to the control plane (``sink: ok|failed|no_token``). A recorded call's
``result`` is the MCP envelope; the receipt carries the tool's decoded answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROXY_CALLS = Path("mcp_capture/calls.jsonl")
GATEWAY_ACCOUNTING = Path("boundary/gateway-accounting.json")
CALL_FIELDS = ("name", "arguments", "result", "error", "status")
SINK_DELIVERED = "ok"


@dataclass(frozen=True)
class Records:
    """The boundary records of one package; ``calls`` is None when none were recorded."""

    calls: list[dict[str, Any]] | None = None
    request_count: int | None = None
    malformed: bool = False
    undelivered: bool = False


def read_records(directory: Path) -> Records:
    accounting = read_json(directory / GATEWAY_ACCOUNTING)
    count = accounting.get("responses_request_count") if isinstance(accounting, dict) else None
    calls_file = directory / PROXY_CALLS
    if not calls_file.is_file():
        return Records(request_count=_count(count))
    raw = [_raw_call(line) for line in _lines(calls_file)]
    if not all(isinstance(call, dict) for call in raw):
        return Records(calls=[], request_count=_count(count), malformed=True)
    return Records(
        calls=[proxy_call(call) for call in raw],
        request_count=_count(count),
        undelivered=_undelivered(raw),
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


def envelope_texts(result: Any) -> list[str] | None:
    """Return the text blocks of an MCP tool result, or None for any other value."""

    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return None
    return [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]


def decoded_result(result: Any) -> tuple[Any, str | None]:
    """Return the ``(result, error)`` a recorded tool result stands for.

    The text blocks join with a newline and parse as JSON when they parse, else
    stay text. A result flagged ``isError`` is a failed call: no result, and the
    text as the error. Any other value stays as recorded.
    """

    texts = envelope_texts(result)
    if texts is None:
        return result, None
    if result.get("isError") is True:
        return None, "\n".join(texts) or "isError"
    if not texts:
        return result, None
    return decoded("\n".join(texts)), None


def error_text(error: Any) -> str | None:
    """Return a JSON-RPC error's message, or the error object as compact JSON."""

    if not isinstance(error, dict):
        return text(error)
    return text(error.get("message")) or json.dumps(
        error, separators=(",", ":"), ensure_ascii=False
    )


def decoded(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _raw_call(line: str) -> Any:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None
