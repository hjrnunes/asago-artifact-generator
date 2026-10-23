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
``tools/call`` sent straight to the target's MCP server, not through a testing tool.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .preparation import TargetCall

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

    Each call opens its own session (``initialize``, then ``notifications/initialized``) over
    MCP's HTTP transport, reading a JSON or an event-stream answer. The call returns the
    tool's structured content, or else its one text block decoded as a JSON object; a tool
    error or any other answer raises, which preparation records as ``setup_failed``.
    """

    def call(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        session = _McpSession(mcp_url, timeout)
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
        return _tool_value(operation, result)

    return call


class _McpSession:
    """One MCP session over HTTP: JSON-RPC messages POSTed to one URL."""

    def __init__(self, url: str, timeout: float) -> None:
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        self.next_id = 0

    def request(self, method: str, params: dict[str, Any]) -> Any:
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        reply = self._answer(self._post(message), self.next_id)
        if "error" in reply:
            raise RuntimeError(f"MCP {method} failed: {reply['error']}")
        return reply.get("result")

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    def _post(self, message: dict[str, Any]) -> tuple[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url, data=json.dumps(message).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
            body = response.read().decode("utf-8")
            return response.headers.get("Content-Type", ""), body

    @staticmethod
    def _answer(response: tuple[str, str], request_id: int) -> dict[str, Any]:
        """Return the JSON-RPC answer to ``request_id`` from a JSON or event-stream body."""

        content_type, body = response
        if content_type.startswith("text/event-stream"):
            candidates = [
                line[len("data:") :].strip()
                for line in body.splitlines()
                if line.startswith("data:")
            ]
        else:
            candidates = [body]
        for candidate in candidates:
            try:
                message = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
        raise RuntimeError(f"no MCP answer to request {request_id}")


def _tool_value(operation: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError(f"{operation} returned no MCP result")
    texts = [
        item.get("text")
        for item in result.get("content") or []
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    if result.get("isError"):
        raise RuntimeError(f"{operation} failed: {' '.join(str(text) for text in texts)}")
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    if len(texts) == 1 and isinstance(texts[0], str):
        try:
            value = json.loads(texts[0])
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"{operation} returned no JSON object")


__all__ = [
    "AUTHORIZATION_FIELD",
    "AUTHORIZED_KINDS",
    "TargetConfig",
    "authorized_target",
    "load_target_config",
    "mcp_call_target",
]
