"""MCP result envelopes, JSON-RPC errors and the record-file readers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from asago_bundle_core.mcp_records import (
    count_value,
    decoded,
    decoded_result,
    error_text,
    lines,
    read_json,
    text,
)


def envelope(*texts: str, **extra: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": t} for t in texts], **extra}


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param(envelope('{"ok": true}'), ({"ok": True}, None), id="json"),
        pytest.param(envelope("plain", "text"), ("plain\ntext", None), id="joined-text"),
        pytest.param(envelope("[1,", "2]"), ([1, 2], None), id="json-across-blocks"),
        pytest.param(envelope("denied", isError=True), (None, "denied"), id="is-error"),
        pytest.param(envelope(isError=True), (None, "isError"), id="is-error-without-text"),
        pytest.param(envelope(isError="true"), (envelope(isError="true"), None), id="truthy"),
        pytest.param(
            {"content": [{"type": "image", "data": "x"}], "structuredContent": {"a": 1}},
            ({"content": [{"type": "image", "data": "x"}], "structuredContent": {"a": 1}}, None),
            id="no-text-block",
        ),
        pytest.param({"content": "x"}, ({"content": "x"}, None), id="content-not-a-list"),
        pytest.param("raw", ("raw", None), id="not-an-envelope"),
        pytest.param(None, (None, None), id="none"),
    ],
)
def test_decoded_result(result: Any, expected: tuple[Any, str | None]) -> None:
    assert decoded_result(result) == expected


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ({"code": -32601, "message": "no such tool"}, "no such tool"),
        ({"code": -32601, "message": ""}, '{"code":-32601,"message":""}'),
        ({"code": 1, "data": "é"}, '{"code":1,"data":"é"}'),
        ("boom", "boom"),
        ("", None),
        (None, None),
        (7, None),
    ],
)
def test_error_text(error: Any, expected: str | None) -> None:
    assert error_text(error) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [('{"a": 1}', {"a": 1}), ("not json", "not json"), ({"a": 1}, {"a": 1}), (None, None)],
)
def test_decoded(value: Any, expected: Any) -> None:
    assert decoded(value) == expected


@pytest.mark.parametrize(("value", "expected"), [("x", "x"), ("", None), (None, None), (1, None)])
def test_text(value: Any, expected: str | None) -> None:
    assert text(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [(0, 0), (3, 3), (-1, None), (True, None), (1.0, None), ("2", None)]
)
def test_count_value(value: Any, expected: int | None) -> None:
    assert count_value(value) == expected


def test_lines_skip_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    path.write_text('{"a": 1}\n\n  \n{"b": 2}\n', encoding="utf-8")

    assert lines(path) == ['{"a": 1}', '{"b": 2}']


def test_read_json_returns_none_for_a_missing_or_malformed_file(tmp_path: Path) -> None:
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text('{"n": 1}', encoding="utf-8")
    bad.write_text("{not json", encoding="utf-8")

    assert read_json(good) == {"n": 1}
    assert read_json(bad) is None
    assert read_json(tmp_path / "missing.json") is None
