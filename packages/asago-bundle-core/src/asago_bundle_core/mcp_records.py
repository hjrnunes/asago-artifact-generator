"""Reading the orchestrator's boundary records: MCP result envelopes, JSON values and record lines.

A recorded call's ``result`` is the MCP envelope the tool answered with; the
receipt carries the tool's decoded answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .text import jsonl_lines


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
    text as the error. Any other value, or a successful envelope with no text
    block, stays as recorded; ``structuredContent`` is never read.
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
    """Return a JSON-RPC error's message, or the error object as compact JSON.

    A call that failed at the protocol level did not run, so the receipt says
    so even when the error carries no message.
    """

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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def lines(path: Path) -> list[str]:
    return jsonl_lines(path.read_text(encoding="utf-8"))


def count_value(value: Any) -> int | None:
    """Return a non-negative integer; None for anything else, a bool included."""

    return value if type(value) is int and value >= 0 else None
