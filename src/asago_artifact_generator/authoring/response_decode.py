from __future__ import annotations

import json
import re
from typing import Any

from .core import (
    AuthoringError,
    Call1FramingError,
    Finding,
    ReviewResponseError,
    TransportResponse,
)

_MISSING = object()


def _provider_field(value: Any, name: str) -> Any:
    """Read a returned provider field without adding fields to the request."""

    if isinstance(value, dict):
        return value[name] if name in value else _MISSING
    return getattr(value, name, _MISSING)


def _captured_text_field(value: Any) -> dict[str, Any]:
    """Classify text content without collapsing absent, null, and empty values."""

    if value is _MISSING:
        return {"state": "absent"}
    if value is None:
        return {"state": "null"}
    if value == "":
        return {"state": "empty", "content": ""}
    if isinstance(value, str):
        return {"state": "text", "content": value}
    return {"state": "non_text", "value_type": type(value).__name__}


def _captured_scalar_field(value: Any) -> dict[str, Any]:
    """Classify optional scalar response metadata without inference."""

    if value is _MISSING:
        return {"state": "absent"}
    if value is None:
        return {"state": "null"}
    return {"state": "value", "value": value}


def _provider_response_capture(choice: Any, message: Any) -> dict[str, Any]:
    """Keep provider response fields separate from final-answer parsing."""

    reasoning_field = _MISSING
    reasoning_source = None
    for field_name in ("reasoning_content", "reasoning"):
        value = _provider_field(message, field_name)
        if value is not _MISSING:
            reasoning_field = value
            reasoning_source = field_name
            break
    reasoning = _captured_text_field(reasoning_field)
    if reasoning_source is not None:
        reasoning["source_field"] = reasoning_source
    return {
        "schema_version": "authoring-response-capture-v1",
        "final_answer": _captured_text_field(_provider_field(message, "content")),
        "reasoning": reasoning,
        "finish_reason": _captured_scalar_field(_provider_field(choice, "finish_reason")),
    }


def _response_parts(
    response: TransportResponse | str | bytes,
) -> tuple[
    bytes,
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    if isinstance(response, TransportResponse):
        return (
            response.raw,
            response.usage,
            response.controls,
            response.response_capture,
        )
    if isinstance(response, str):
        return response.encode("utf-8"), None, {"max_retries": 0}, None
    if isinstance(response, bytes):
        return response, None, {"max_retries": 0}, None
    raise TypeError("authoring transport returned an unsupported response")


def _readable_response(raw: bytes) -> tuple[str, str]:
    """Return one readable correction copy without changing evidence bytes."""

    try:
        return raw.decode("utf-8"), "utf-8-exact"
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), "utf-8-replacement-inexact"


def _decode_v2_json_response(raw: bytes) -> tuple[dict[str, Any], str | None]:
    """Decode exactly one v2 Call 1 object without changing response bytes."""

    return _decode_strict_single_json_response(
        raw,
        subject="Call 1",
        error=Call1FramingError,
        path="call1",
    )


def _decode_review_json_response(raw: bytes) -> tuple[dict[str, Any], str | None]:
    """Decode exactly one reviewer object without changing response bytes."""

    return _decode_strict_single_json_response(
        raw,
        subject="review response",
        error=ReviewResponseError,
        path="review",
    )


def _decode_strict_single_json_response(
    raw: bytes,
    *,
    subject: str,
    error: type[AuthoringError],
    path: str,
) -> tuple[dict[str, Any], str | None]:
    """Accept one bare JSON object or exactly one lowercase ```json fence."""

    text = raw.decode("utf-8").strip()
    if text.startswith("```"):
        first_line = text.splitlines()[0] if text.splitlines() else ""
        if first_line == "```":
            raise error(
                [Finding("bare_fence", f"{subject} does not allow an untagged fence", path)]
            )
        if first_line != "```json":
            raise error(
                [
                    Finding(
                        "unsupported_fence",
                        f"{subject} requires one lowercase ```json fence",
                        path,
                    )
                ]
            )
        closed = re.match(
            r"```json\r?\n(?P<body>.*?)\r?\n```(?P<tail>.*)\Z",
            text,
            re.DOTALL,
        )
        if closed is None:
            code = "truncated_fence"
            detail = f"{subject} lowercase ```json fence is not closed"
            raise error([Finding(code, detail, path)])
        tail = closed.group("tail").lstrip()
        if tail:
            code = "multiple_json_blocks" if tail.startswith("```") else "trailing_content"
            detail = (
                f"{subject} contains more than one fenced JSON block"
                if code == "multiple_json_blocks"
                else f"{subject} contains content outside its JSON fence"
            )
            raise error([Finding(code, detail, path)])
        return (
            _decode_strict_json_object(
                closed.group("body").strip(),
                subject=subject,
                error=error,
                path=path,
            ),
            "outer_fence_removed",
        )
    return _decode_strict_json_object(text, subject=subject, error=error, path=path), None


def _decode_strict_json_object(
    text: str,
    *,
    subject: str,
    error: type[AuthoringError],
    path: str,
) -> dict[str, Any]:
    """Decode one complete JSON object and classify framing-only failures."""

    try:
        decoder = json.JSONDecoder(parse_constant=_reject_json_constant)
        value, end = decoder.raw_decode(text)
    except (json.JSONDecodeError, ValueError) as exc:
        code = "invalid_json" if text.startswith("{") else "ambiguous_content"
        raise error([Finding(code, f"{subject} JSON object is invalid: {exc}", path)]) from exc
    trailing = text[end:].strip()
    if trailing:
        code = "multiple_json_objects" if trailing.startswith(("{", "[")) else "trailing_content"
        raise error(
            [
                Finding(
                    code,
                    f"{subject} must contain exactly one JSON object with no trailing content",
                    path,
                )
            ]
        )
    if not isinstance(value, dict):
        raise error(
            [Finding("json_object_required", f"{subject} must decode to one JSON object", path)]
        )
    return value


def _reject_json_constant(value: str) -> None:
    """Reject Python-only numeric constants that are not JSON values."""

    raise ValueError(f"invalid JSON constant: {value}")
