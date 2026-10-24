"""The target config an adapter receives, its authorization, and the MCP setup call."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_core.errors import BundleError
from asago_bundle_core.preparation import TargetCallFailed
from asago_bundle_core.target import (
    TargetConfig,
    _answer,
    _is_sse,
    _tool_value,
    authorized_target,
    load_target_config,
)

PIGGYBANK = Path(__file__).resolve().parents[3] / "tests/fixtures/targets/piggybank"


class Refused(BundleError):
    pass


@pytest.mark.parametrize("kind", ["mock", "test_instance"])
def test_a_mock_or_a_test_instance_is_authorized(kind: str) -> None:
    assert authorized_target({"authorized_as": kind}, Refused).authorized_as == kind


@pytest.mark.parametrize(
    ("config", "found"),
    [
        pytest.param({}, "it lacks it", id="missing"),
        pytest.param({"authorized_as": "production"}, "it has 'production'", id="other"),
        pytest.param({"authorized_as": ""}, "it has ''", id="empty"),
        pytest.param({"authorized_as": True}, "it has True", id="boolean"),
    ],
)
def test_any_other_config_is_refused(config: dict[str, Any], found: str) -> None:
    with pytest.raises(Refused, match="must be one of mock, test_instance, and " + found):
        authorized_target(config, Refused)


def test_the_fixture_target_is_a_mock_read_from_its_file_or_directory() -> None:
    from_file = authorized_target(PIGGYBANK / "target.yaml", Refused)
    from_directory = authorized_target(str(PIGGYBANK), Refused)

    assert from_file == from_directory
    assert from_file.authorized_as == "mock"
    assert from_file.source == (PIGGYBANK / "target.yaml").resolve()


def test_a_refusal_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "target.yaml").write_text("target: x\n", encoding="utf-8")

    with pytest.raises(Refused, match=str(tmp_path)):
        authorized_target(tmp_path, Refused)


@pytest.mark.parametrize(
    ("text", "message"), [("- a list\n", "not a mapping"), ("a: [\n", "unreadable")]
)
def test_an_unusable_file_is_refused(tmp_path: Path, text: str, message: str) -> None:
    (tmp_path / "target.yaml").write_text(text, encoding="utf-8")

    with pytest.raises(Refused, match=message):
        load_target_config(tmp_path / "target.yaml", Refused)


def test_the_port_fills_the_endpoint_urls_only() -> None:
    config = load_target_config(PIGGYBANK / "target.yaml", Refused, port=4321)

    assert config.mcp_url == "http://127.0.0.1:4321/mcp"
    assert config.config["endpoint"]["agent_url"] == "http://127.0.0.1:4321/agent"
    assert "{port}" in config.config["endpoint"]["start"]
    assert load_target_config(PIGGYBANK, Refused).mcp_url == "http://127.0.0.1:{port}/mcp"


def test_a_port_needs_an_endpoint_block() -> None:
    with pytest.raises(Refused, match="no endpoint block"):
        load_target_config(TargetConfig({"authorized_as": "mock"}), Refused, port=1)


@pytest.mark.parametrize(
    ("url", "sse"),
    [
        pytest.param("http://127.0.0.1:1/sse", True, id="sse-path"),
        pytest.param("http://127.0.0.1:1/x/sse/", True, id="sse-path-slash"),
        pytest.param("http://127.0.0.1:1/mcp", False, id="other-path"),
        pytest.param("http://127.0.0.1:1/ssex", False, id="not-sse"),
    ],
)
def test_an_mcp_url_whose_path_ends_in_sse_is_the_sse_transport(url: str, sse: bool) -> None:
    assert _is_sse(url) is sse


def test_an_event_stream_answer_is_its_message_with_the_request_id() -> None:
    events = iter(
        [
            ("message", '{"jsonrpc": "2.0", "method": "notifications/progress"}'),
            ("message", "not json"),
            ("ping", '{"jsonrpc": "2.0", "id": 2, "result": {"other": true}}'),
            ("message", '{"jsonrpc": "2.0", "id": 1, "result": {"other": true}}'),
            ("message", '{"jsonrpc": "2.0", "id": 2, "result": {"ok": true}}'),
        ]
    )

    assert _answer(events, 2, "the answer") == {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}
    with pytest.raises(RuntimeError, match="MCP stream closed before the answer"):
        _answer(events, 3, "the answer")


@pytest.mark.parametrize(
    ("result", "value"),
    [
        pytest.param(
            {"content": [{"type": "text", "text": "{}"}], "structuredContent": {"a": 1}},
            {"a": 1},
            id="structured-content",
        ),
        pytest.param(
            {"content": [{"type": "text", "text": json.dumps({"b": 2})}]}, {"b": 2}, id="text"
        ),
        pytest.param(
            # How FastMCP reports a tool that returns a JSON string.
            {
                "content": [{"type": "text", "text": json.dumps({"c": 3})}],
                "structuredContent": {"result": json.dumps({"c": 3})},
            },
            {"c": 3},
            id="fastmcp-wrapped-json-object",
        ),
        pytest.param(
            # The wrapper of a string that holds no JSON object is the object itself.
            {
                "content": [{"type": "text", "text": "[1]"}],
                "structuredContent": {"result": "[1]"},
            },
            {"result": "[1]"},
            id="fastmcp-wrapped-other-string",
        ),
        pytest.param(
            # Only a wrapper of one string key is unwrapped.
            {
                "content": [{"type": "text", "text": json.dumps({"c": 3})}],
                "structuredContent": {"result": json.dumps({"c": 3}), "more": 1},
            },
            {"result": json.dumps({"c": 3}), "more": 1},
            id="not-a-wrapper",
        ),
    ],
)
def test_a_tool_result_decodes_to_its_object(result: dict[str, Any], value: Any) -> None:
    assert _tool_value("op", result) == value


@pytest.mark.parametrize(
    ("result", "message"),
    [
        pytest.param(
            {"content": [{"type": "text", "text": "no such account"}], "isError": True},
            "op failed: no such account",
            id="tool-error",
        ),
        pytest.param({"content": [{"type": "text", "text": "plain"}]}, "no JSON", id="plain"),
        pytest.param(None, "no MCP result", id="no-result"),
    ],
)
def test_any_other_tool_result_raises(result: Any, message: str) -> None:
    with pytest.raises(RuntimeError, match=message) as raised:
        _tool_value("op", result)

    # A failure on an answer keeps that answer.
    if result is not None:
        assert isinstance(raised.value, TargetCallFailed)
        assert raised.value.native_result == result
