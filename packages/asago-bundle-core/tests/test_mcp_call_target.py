"""The MCP setup call over both transports, against a scripted MCP server on a local socket.

``ScriptedMcp`` serves streamable HTTP at ``/mcp`` and the legacy SSE transport at ``/sse``
(the stream's ``endpoint`` event names ``/messages/?session_id=...``, or the path-relative
``messages/?session_id=...``, and each answer is a ``message`` event on the stream). Its tools
answer from their name: ``lookup`` returns an object the way a FastMCP tool returning a JSON
string does, ``fail`` is a tool error.
"""

from __future__ import annotations

import json
import queue
import threading
import time
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
# What a stream carries before the answer when ``noise`` is set: a notification, the answer
# to another request and data that is no JSON.
NOISE = [
    {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "working"}},
    {"jsonrpc": "2.0", "id": 999, "result": {"other": True}},
    "not json",
]
CLOSE = object()
DRIP = object()
DRIP_INTERVAL = 0.02


class ScriptedMcp:
    """A local MCP server; its options script how it answers.

    ``sse_mode``: ``ok`` answers every request; ``no_endpoint`` closes the stream before any
    event; ``silent`` never answers ``tools/call``; ``close`` closes the stream instead;
    ``drip`` answers ``tools/call`` with one byte at a time and never a whole line.
    ``http_mode``: ``json`` answers with a JSON body; ``event_stream`` with an event stream
    of known length; ``held_open`` with an event stream that stays open after the answer;
    ``drip`` drips ``tools/call``'s event stream as ``sse_mode`` does. ``relative_endpoint``
    names the SSE POST URL relative to ``/sse``; ``noise`` sends ``NOISE`` before each answer
    on a stream.
    """

    def __init__(
        self,
        sse_mode: str = "ok",
        http_mode: str = "json",
        relative_endpoint: bool = False,
        noise: bool = False,
    ) -> None:
        self.sse_mode = sse_mode
        self.http_mode = http_mode
        self.relative_endpoint = relative_endpoint
        self.noise = noise
        self.received: list[dict[str, Any]] = []
        self.streams: dict[str, queue.Queue[Any]] = {}
        self.closing = threading.Event()

    def __enter__(self) -> ScriptedMcp:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.scripted = self  # type: ignore[attr-defined]
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.closing.set()
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

    def events(self, answer: dict[str, Any]) -> str:
        """Return the event-stream text that carries ``answer``, after ``NOISE`` if set."""

        messages = [*NOISE, answer] if self.noise else [answer]
        data = [item if isinstance(item, str) else json.dumps(item) for item in messages]
        return "".join(f"event: message\r\ndata: {item}\r\n\r\n" for item in data)


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
        prefix = "" if self.scripted.relative_endpoint else "/"
        self._write(f"event: endpoint\r\ndata: {prefix}messages/?session_id={session}\r\n\r\n")
        while True:
            try:
                message = stream.get(timeout=0.1)
            except queue.Empty:
                # A keep-alive comment while the stream waits, as an SSE server sends.
                self._write(": ping\r\n\r\n")
                continue
            if message is CLOSE:
                return
            if message is DRIP:
                self._drip()
                return
            self._write(self.scripted.events(message))

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
            mode = self.scripted.sse_mode if body["method"] == "tools/call" else "ok"
            if mode == "close":
                stream.put(CLOSE)
            elif mode == "drip":
                stream.put(DRIP)
            elif mode != "silent":
                stream.put(answer)
        elif parts.path == "/mcp":
            answer = self.scripted.answer(body)
            if answer is None:
                self.send_response(202)
                self.end_headers()
                return
            mode = self.scripted.http_mode
            if mode == "drip" and body["method"] != "tools/call":
                mode = "event_stream"
            if mode == "json":
                self._answer("application/json", json.dumps(answer))
            elif mode == "event_stream":
                self._answer("text/event-stream", self.scripted.events(answer))
            else:
                # No length: the body ends only when the server closes the connection.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if mode == "drip":
                    self._drip()
                    return
                self._write(self.scripted.events(answer))
                self.scripted.closing.wait(10)
        else:
            self.send_error(404)

    def _answer(self, content_type: str, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Mcp-Session-Id", "scripted-session")
        self.end_headers()
        self.wfile.write(data)

    def _drip(self) -> None:
        """Send part of a line one byte at a time, never ending it, until the client goes."""

        try:
            self._write("data: ")
            while not self.scripted.closing.is_set():
                time.sleep(DRIP_INTERVAL)
                self._write("x")
        except OSError:
            return

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
        pytest.param("/sse", {"relative_endpoint": True}, id="sse-relative-endpoint"),
        pytest.param("/sse", {"noise": True}, id="sse-noise-before-the-answer"),
        pytest.param("/mcp", {}, id="http-json"),
        pytest.param("/mcp", {"http_mode": "event_stream"}, id="http-event-stream"),
        pytest.param(
            "/mcp", {"http_mode": "event_stream", "noise": True}, id="http-noise-before-the-answer"
        ),
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


@pytest.mark.parametrize("scripted", [{"http_mode": "held_open"}], indirect=True)
def test_an_event_stream_answer_is_read_up_to_its_message(scripted: ScriptedMcp) -> None:
    # Each answer's stream stays open after it; the call reads only up to the answer.
    started = time.monotonic()

    value = mcp_call_target(scripted.base + "/mcp", timeout=5.0)("lookup", {})

    assert value == {"order_id": "ORD-1", "status": "paid"}
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
    ("path", "scripted"),
    [
        pytest.param("/sse", {"sse_mode": "drip"}, id="sse"),
        pytest.param("/mcp", {"http_mode": "drip"}, id="http-event-stream"),
    ],
    indirect=["scripted"],
)
def test_a_server_that_drips_its_answer_times_out_at_the_deadline(
    scripted: ScriptedMcp, path: str
) -> None:
    timeout = 0.5
    call = mcp_call_target(scripted.base + path, timeout=timeout)
    started = time.monotonic()

    with pytest.raises(RuntimeError, match="timed out waiting for the answer to MCP tools/call"):
        call("lookup", {})

    # A byte arrives every DRIP_INTERVAL, far inside the deadline, but the call still ends
    # once the deadline has passed.
    assert timeout <= time.monotonic() - started < timeout + 0.5


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
            "MCP stream closed before the endpoint event",
            id="no-endpoint-event",
        ),
        pytest.param(
            {"sse_mode": "close"},
            "MCP stream closed before the answer to MCP tools/call",
            id="closed-stream",
        ),
        pytest.param(
            {"sse_mode": "silent"},
            "MCP stream timed out waiting for the answer to MCP tools/call",
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
