"""The target configuration a caller hands an adapter, its authorization, and setup calls.

A target is described by ``target.yaml``, with ``tools-list.json`` and ``runtime-spec.yaml``
beside it, plus an ``endpoint`` block naming where the running target answers (``mcp_url``,
``agent_url``) and the commands that run it. The caller chooses the file, or hands over the
mapping it already loaded; no adapter looks for a target on its own.

A run makes real writes to whatever the configuration points at, so every adapter's
``instantiate`` refuses a configuration that does not state, in ``authorized_as``, that the
target is a ``mock`` (a stand-in built for testing) or a ``test_instance`` (a test deployment
of a real system). No other value runs: a production system is never a target.

``mcp_call_target`` is the target call package preparation's setup steps go through: one MCP
``tools/call`` sent straight to the target's MCP server, not through a testing tool. It speaks
either MCP transport, and ``endpoint.mcp_url`` chooses it: a URL whose path ends in ``/sse``
is the legacy SSE transport (a GET opens an event stream whose ``endpoint`` event names the URL
to POST to, and the answers arrive on the stream as ``message`` events); any other is
streamable HTTP (JSON-RPC messages POSTed to the URL).
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlsplit

import yaml

from .preparation import TargetCall, TargetCallFailed, ToolResult

AUTHORIZATION_FIELD = "authorized_as"
AUTHORIZED_KINDS = ("mock", "test_instance")
TARGET_FILE = "target.yaml"
# The endpoint URLs whose ``{port}`` placeholder the caller fills.
ENDPOINT_URLS = ("agent_url", "mcp_url")
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class TargetConfig:
    """A loaded target configuration; ``source`` is its ``target.yaml`` when read from one."""

    config: dict[str, Any]
    source: Path | None = None

    @property
    def authorized_as(self) -> Any:
        return self.config.get(AUTHORIZATION_FIELD)

    @property
    def mcp_url(self) -> str | None:
        """Return ``endpoint.mcp_url``, or None when the configuration names none."""

        endpoint = self.config.get("endpoint")
        url = endpoint.get("mcp_url") if isinstance(endpoint, dict) else None
        return url if isinstance(url, str) else None


def load_target_config(
    target: TargetConfig | Mapping[str, Any] | str | Path,
    error: type[Exception],
    *,
    port: int | None = None,
) -> TargetConfig:
    """Return the configuration ``target`` holds or names, raising ``error`` when unusable.

    ``target`` is a ``TargetConfig``, a mapping already loaded, or the path of a
    ``target.yaml`` (or of the directory holding one). ``port`` fills the ``{port}``
    placeholder of the endpoint URLs.
    """

    if isinstance(target, TargetConfig):
        loaded = target
    elif isinstance(target, Mapping):
        loaded = TargetConfig(dict(target))
    elif isinstance(target, str | Path):
        loaded = _read(Path(target), error)
    else:
        raise error(f"the target config is neither a mapping nor a path: {type(target).__name__}")
    if port is None:
        return loaded
    endpoint = loaded.config.get("endpoint")
    if not isinstance(endpoint, dict):
        raise error("the target config has no endpoint block to fill the port into")
    filled = {
        key: value.replace("{port}", str(port))
        if key in ENDPOINT_URLS and isinstance(value, str)
        else value
        for key, value in endpoint.items()
    }
    return TargetConfig({**loaded.config, "endpoint": filled}, loaded.source)


def authorized_target(
    target: TargetConfig | Mapping[str, Any] | str | Path,
    error: type[Exception],
    *,
    port: int | None = None,
) -> TargetConfig:
    """Return the loaded configuration when it marks the target as a mock or a test instance.

    Raises ``error`` for a configuration that lacks ``authorized_as`` or holds another value,
    before any adapter work starts.
    """

    loaded = load_target_config(target, error, port=port)
    if loaded.authorized_as not in AUTHORIZED_KINDS:
        where = f" ({loaded.source})" if loaded.source else ""
        found = (
            "lacks it"
            if AUTHORIZATION_FIELD not in loaded.config
            else f"has {loaded.authorized_as!r}"
        )
        raise error(
            f"the target config{where} is not authorized for a run: {AUTHORIZATION_FIELD} "
            f"must be one of {', '.join(AUTHORIZED_KINDS)}, and it {found}"
        )
    return loaded


def _read(path: Path, error: type[Exception]) -> TargetConfig:
    file = path / TARGET_FILE if path.is_dir() else path
    try:
        config = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise error(f"the target config is unreadable: {file}: {exc}") from exc
    if not isinstance(config, dict):
        raise error(f"the target config is not a mapping: {file}")
    return TargetConfig(config, file.resolve())


def mcp_call_target(mcp_url: str, *, timeout: float = MCP_TIMEOUT_SECONDS) -> TargetCall:
    """Return a setup call that sends one MCP ``tools/call`` to ``mcp_url`` and decodes it.

    A URL whose path ends in ``/sse`` speaks the legacy SSE transport, any other streamable
    HTTP. Each call opens its own session (``initialize``, then
    ``notifications/initialized``). Over streamable HTTP an answer is the POST's JSON or
    event-stream body, read up to the answer; over SSE it is a ``message`` event on the
    stream the session opened. ``timeout`` bounds each request, from sending it to reading
    its answer, however slowly the server sends.

    The call returns a ``ToolResult``: the tool's structured content (``_tool_value``), or
    else its one text block decoded as a JSON object, with the MCP result as received in
    ``native_result``.
    A tool error raises ``TargetCallFailed`` carrying that result; any other answer, a
    closed stream or a timeout raises. Preparation records each failure as ``setup_failed``.
    """

    session_type = _SseSession if _is_sse(mcp_url) else _McpSession

    def call(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        session = session_type(mcp_url, timeout)
        try:
            session.request(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "asago-bundle-core", "version": "1"},
                },
            )
            session.notify("notifications/initialized")
            result = session.request("tools/call", {"name": operation, "arguments": arguments})
        finally:
            session.close()
        return ToolResult(_tool_value(operation, result), result)

    return call


def _is_sse(mcp_url: str) -> bool:
    return urlsplit(mcp_url).path.rstrip("/").endswith("/sse")


class _McpSession:
    """One MCP session over streamable HTTP: JSON-RPC messages POSTed to one URL.

    An answer is the POST's JSON body, or a ``message`` event of its event-stream body; the
    stream is read only up to the message that answers the request.
    """

    def __init__(self, url: str, timeout: float) -> None:
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        self.next_id = 0

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        response = self._post(message)
        try:
            return _result(method, self._reply(response, f"the answer to MCP {method}"))
        finally:
            response.close()

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method}).close()

    def close(self) -> None:
        """Nothing stays open between requests."""

    def _post(self, message: dict[str, Any]) -> _Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        response = _Response(
            self.url,
            "POST",
            headers=headers,
            body=json.dumps(message).encode("utf-8"),
            deadline=monotonic() + self.timeout,
        )
        self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
        return response

    def _reply(self, response: _Response, waiting: str) -> dict[str, Any]:
        """Return the answer to the latest request from its JSON or event-stream body."""

        if response.headers.get("Content-Type", "").startswith("text/event-stream"):
            return _answer(response.events(), self.next_id, waiting)
        try:
            body = response.read()
        except TimeoutError as exc:
            raise RuntimeError(f"MCP answer timed out: {waiting}") from exc
        try:
            reply = json.loads(body)
        except json.JSONDecodeError:
            reply = None
        if isinstance(reply, dict) and reply.get("id") == self.next_id:
            return reply
        raise RuntimeError(f"no MCP answer to request {self.next_id}")


class _SseSession:
    """One MCP session over the legacy SSE transport.

    A GET of ``url`` opens the event stream; its first ``endpoint`` event names the URL
    (relative to ``url``) each JSON-RPC message is POSTed to, and each answer arrives on
    the stream as a ``message`` event.
    """

    def __init__(self, url: str, timeout: float) -> None:
        self.timeout = timeout
        self.next_id = 0
        self.stream = _Response(
            url,
            "GET",
            headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
            deadline=monotonic() + timeout,
        )
        try:
            self.events = self.stream.events()
            self.post_url = urljoin(url, _next(self.events, "endpoint", "the endpoint event"))
        except BaseException:
            self.stream.close()
            raise

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.next_id += 1
        self.stream.deadline = monotonic() + self.timeout
        self._post({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        return _result(method, _answer(self.events, self.next_id, f"the answer to MCP {method}"))

    def notify(self, method: str) -> None:
        self.stream.deadline = monotonic() + self.timeout
        self._post({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        self.stream.close()

    def _post(self, message: dict[str, Any]) -> None:
        response = _Response(
            self.post_url,
            "POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(message).encode("utf-8"),
            deadline=self.stream.deadline,
        )
        try:
            response.read()
        finally:
            response.close()


class _Response:
    """One HTTP request's response, read by chunks before an absolute deadline.

    ``deadline`` is a ``monotonic`` time; its owner may move it for each request on a
    stream. Each read waits at most the time left, and none starts once it has passed, so a
    server that sends its answer slowly cannot stretch a request past it.
    """

    def __init__(
        self,
        url: str,
        method: str,
        *,
        headers: dict[str, str],
        deadline: float,
        body: bytes | None = None,
    ) -> None:
        self.deadline = deadline
        parts = urlsplit(url)
        connection = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        self.connection = connection(parts.netloc, timeout=self._remaining())
        self.response: http.client.HTTPResponse | None = None
        try:
            # A response without a length takes the connection's socket over, so the socket
            # is kept here to bound each read by the time left.
            self.connection.connect()
            self.socket = self.connection.sock
            target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            self.connection.request(method, target, body=body, headers=headers)
            self.socket.settimeout(self._remaining())
            self.response = self.connection.getresponse()
            if self.response.status >= 400:
                raise RuntimeError(f"MCP {method} {url} answered HTTP {self.response.status}")
        except BaseException:
            self.close()
            raise
        self.headers = self.response.headers

    def read(self) -> bytes:
        return b"".join(self._chunks())

    def events(self) -> Iterator[tuple[str, str]]:
        """Yield each server-sent event of the body as (event type, data)."""

        kind, pending = "message", b""
        data: list[str] = []
        for chunk in self._chunks():
            *lines, pending = (pending + chunk).split(b"\n")
            for raw in lines:
                line = raw.decode("utf-8").rstrip("\r")
                if not line:
                    if data:
                        yield kind, "\n".join(data)
                    kind, data = "message", []
                elif not line.startswith(":"):
                    field, _, value = line.partition(":")
                    value = value[1:] if value.startswith(" ") else value
                    if field == "event":
                        kind = value
                    elif field == "data":
                        data.append(value)

    def close(self) -> None:
        if self.response is not None:
            self.response.close()
        self.connection.close()

    def _chunks(self) -> Iterator[bytes]:
        assert self.response is not None
        # A response closes its socket once it has read the whole of a body with a length.
        while not self.response.isclosed():
            self.socket.settimeout(self._remaining())
            chunk = self.response.read1(65536)
            if not chunk:
                return
            yield chunk

    def _remaining(self) -> float:
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("the MCP request's time ran out")
        return remaining


def _next(events: Iterator[tuple[str, str]], kind: str, waiting: str) -> str:
    """Return the data of the next ``kind`` event of ``events``, skipping any other."""

    try:
        for event, data in events:
            if event == kind:
                return data
    except TimeoutError as exc:
        raise RuntimeError(f"MCP stream timed out waiting for {waiting}") from exc
    raise RuntimeError(f"MCP stream closed before {waiting}")


def _answer(events: Iterator[tuple[str, str]], request_id: int, waiting: str) -> dict[str, Any]:
    """Return the ``message`` event answering ``request_id``.

    Notifications, answers to other ids and data that is no JSON are skipped.
    """

    while True:
        try:
            message = json.loads(_next(events, "message", waiting))
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message


def _result(method: str, reply: dict[str, Any]) -> Any:
    if "error" in reply:
        raise RuntimeError(f"MCP {method} failed: {reply['error']}")
    return reply.get("result")


def _tool_value(operation: str, result: Any) -> dict[str, Any]:
    """Return the object a ``tools/call`` result holds.

    Structured content is the value when present. FastMCP reports a tool whose return type
    is not an object as the wrapper ``{"result": <value>}``; when the value is a string
    holding a JSON object, that object is the value. Without structured content, the tool's
    one text block decoded as a JSON object is.
    """

    if not isinstance(result, dict):
        raise RuntimeError(f"{operation} returned no MCP result")
    texts = [
        item.get("text")
        for item in result.get("content") or []
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    if result.get("isError"):
        raise TargetCallFailed(
            f"{operation} failed: {' '.join(str(text) for text in texts)}", native_result=result
        )
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        if structured.keys() == {"result"} and isinstance(structured["result"], str):
            wrapped = _json_object(structured["result"])
            return structured if wrapped is None else wrapped
        return structured
    if len(texts) == 1 and isinstance(texts[0], str):
        value = _json_object(texts[0])
        if value is not None:
            return value
    raise TargetCallFailed(f"{operation} returned no JSON object", native_result=result)


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


__all__ = [
    "AUTHORIZATION_FIELD",
    "AUTHORIZED_KINDS",
    "TargetConfig",
    "authorized_target",
    "load_target_config",
    "mcp_call_target",
]
