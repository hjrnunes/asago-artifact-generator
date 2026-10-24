"""The MCP setup call over both transports, against a scripted MCP server on a local socket.

``ScriptedMcp`` serves streamable HTTP at ``/mcp`` and the legacy SSE transport at ``/sse``
(the stream's ``endpoint`` event names ``/messages/?session_id=...``, and each answer is a
``message`` event on the stream). Its tools answer from their name: ``lookup`` returns an
object the way a FastMCP tool returning a JSON string does, ``fail`` is a tool error.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from asago_bundle_core.preparation import TargetCallFailed, ToolResult
from asago_bundle_core.target import mcp_call_target

LOOKUP_TEXT = json.dumps({"order_id": "ORD-1", "status": "paid"})
LOOKUP = {
    "content": [{"type": "text", "text": LOOKUP_TEXT}],
    "structuredContent": {"result": LOOKUP_TEXT},
    "isError": False,
}
FAIL = {"content": [{"type": "text", "text": "no such order"}], "isError": True}
CLOSE = object()


class ScriptedMcp:
    """A local MCP server; ``sse_mode`` scripts how its SSE stream misbehaves.

    ``ok`` answers every request; ``no_endpoint`` closes the stream before any event;
    ``silent`` never answers ``tools/call``; ``close`` closes the stream instead.
    """

    def __init__(self, sse_mode: str = "ok", event_stream_answers: bool = False) -> None:
        self.sse_mode = sse_mode
        self.event_stream_answers = event_stream_answers
        self.received: list[dict[str, Any]] = []
        self.streams: dict[str, queue.Queue[Any]] = {}

    def __enter__(self) -> ScriptedMcp:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.scripted = self  # type: ignore[attr-defined]
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        for stream in self.streams.values():
            stream.put(CLOSE)
        self.server.shutdown()
        self.server.server_close()

    def answer(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Return the JSON-RPC answer to ``message``, or None for a notification."""

        self.received.append(message)
        if "id" not in message:
            return None
        if message["method"] == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "scripted", "version": "1"},
            }
        elif message["params"]["name"] == "lookup":
            result = LOOKUP
        else:
            result = FAIL
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:
        pass

    @property
    def scripted(self) -> ScriptedMcp:
        return self.server.scripted  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        if urlsplit(self.path).path != "/sse":
            self.send_error(404)
            return
        session = f"s{len(self.scripted.streams) + 1}"
        stream: queue.Queue[Any] = queue.Queue()
        self.scripted.streams[session] = stream
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.scripted.sse_mode == "no_endpoint":
            self._write(": ping\r\n\r\n")
            return
        self._write(f"event: endpoint\r\ndata: /messages/?session_id={session}\r\n\r\n")
        while True:
            try:
                message = stream.get(timeout=0.1)
            except queue.Empty:
                # A keep-alive comment while the stream waits, as an SSE server sends.
                self._write(": ping\r\n\r\n")
                continue
            if message is CLOSE:
                return
            self._write(f"event: message\r\ndata: {json.dumps(message)}\r\n\r\n")

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        parts = urlsplit(self.path)
        if parts.path == "/messages/":
            stream = self.scripted.streams[parse_qs(parts.query)["session_id"][0]]
            answer = self.scripted.answer(body)
            self.send_response(202)
            self.end_headers()
            if answer is None:
                return
            mode = self.scripted.sse_mode
            if body["method"] == "tools/call" and mode == "close":
                stream.put(CLOSE)
            elif not (body["method"] == "tools/call" and mode == "silent"):
                stream.put(answer)
        elif parts.path == "/mcp":
            answer = self.scripted.answer(body)
            if answer is None:
                self.send_response(202)
                self.end_headers()
                return
            if self.scripted.event_stream_answers:
                kind, text = "text/event-stream", f"event: message\ndata: {json.dumps(answer)}\n\n"
            else:
                kind, text = "application/json", json.dumps(answer)
            data = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Mcp-Session-Id", "scripted-session")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_error(404)

    def _write(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()


@pytest.fixture
def scripted(request: pytest.FixtureRequest) -> Iterator[ScriptedMcp]:
    with ScriptedMcp(**getattr(request, "param", {})) as server:
        yield server


@pytest.mark.parametrize(
    ("path", "scripted"),
    [
        pytest.param("/sse", {}, id="sse"),
        pytest.param("/mcp", {}, id="http-json"),
        pytest.param("/mcp", {"event_stream_answers": True}, id="http-event-stream"),
    ],
    indirect=["scripted"],
)
def test_a_tool_call_returns_its_object_and_the_mcp_result(
    scripted: ScriptedMcp, path: str
) -> None:
    call = mcp_call_target(scripted.base + path)

    value = call("lookup", {"order_id": "ORD-1"})

    assert isinstance(value, ToolResult)
    assert value == {"order_id": "ORD-1", "status": "paid"}
    assert value.native_result == LOOKUP
    assert [message["method"] for message in scripted.received] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert scripted.received[-1]["params"] == {
        "name": "lookup",
        "arguments": {"order_id": "ORD-1"},
    }


@pytest.mark.parametrize("path", ["/sse", "/mcp"])
def test_a_tool_error_raises_with_the_mcp_result(scripted: ScriptedMcp, path: str) -> None:
    with pytest.raises(TargetCallFailed, match="refund failed: no such order") as raised:
        mcp_call_target(scripted.base + path)("refund", {})

    assert raised.value.native_result == FAIL


@pytest.mark.parametrize(
    ("scripted", "message"),
    [
        pytest.param(
            {"sse_mode": "no_endpoint"},
            "MCP SSE stream closed before the endpoint event",
            id="no-endpoint-event",
        ),
        pytest.param(
            {"sse_mode": "close"},
            "MCP SSE stream closed before the answer to MCP tools/call",
            id="closed-stream",
        ),
        pytest.param(
            {"sse_mode": "silent"},
            "MCP SSE stream timed out waiting for the answer to MCP tools/call",
            id="timeout",
        ),
    ],
    indirect=["scripted"],
)
def test_an_sse_stream_that_does_not_answer_raises(scripted: ScriptedMcp, message: str) -> None:
    call = mcp_call_target(scripted.base + "/sse", timeout=0.5)

    with pytest.raises(RuntimeError, match=message):
        call("lookup", {"order_id": "ORD-1"})


def test_an_sse_url_that_serves_no_stream_raises(scripted: ScriptedMcp) -> None:
    with pytest.raises(RuntimeError, match="answered HTTP 404"):
        mcp_call_target(scripted.base + "/elsewhere/sse")("lookup", {})
