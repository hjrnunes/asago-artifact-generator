"""A tiny MCP target for the adapter tests: four tools over a dictionary of state.

``serve`` answers MCP's ``initialize``, ``tools/list`` and ``tools/call`` as JSON-RPC sent by
HTTP POST to ``/mcp`` on localhost, one JSON response per request, and ``GET /health``.
``target.yaml``, ``tools-list.json`` and ``runtime-spec.yaml`` beside this file declare it, and
a declared server also takes ``POST /seed`` (an ``artifact-seed-v1`` document, checked against
the seed slots of ``runtime-spec.yaml``) and ``POST /reset`` (back to the initial state).
``serve --bare`` is the same target without declaration files, seed or reset.

A seed slot's ``field`` is ``<collection>[].<attribute>``: the carrier's result lists the
records of that state collection, and the planted text goes into the record's attribute. A
carrier that returns one record, the one its argument names (``read_note``), has a plain
``<attribute>`` field: the record is in the collection ``LOOKUPS`` gives for the carrier.

The workspace members' pytest sessions share no import path, so each loads this file by path
(``Path(__file__).resolve().parents[3] / "tests/fixtures/targets/piggybank"`` from a member's
``tests/``, as they reach the other repository fixtures) and uses ``running()``. It imports the
standard library and, for the declared server, PyYAML.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SEED_VERSION = "artifact-seed-v1"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INITIAL_STATE: dict[str, Any] = {
    "accounts": {"ACC-1": {"account_id": "ACC-1", "balance": 120.0}},
    "notes": {
        "NOTE-1": {"note_id": "NOTE-1", "body": "Rent of 80.00 is due on the first."},
        "NOTE-2": {"note_id": "NOTE-2", "body": "Keep 20.00 aside for the gas bill."},
    },
}

_ACCOUNT = {
    "type": "object",
    "properties": {"account_id": {"type": "string"}, "balance": {"type": "number"}},
}
_NOTE = {
    "type": "object",
    "properties": {"note_id": {"type": "string"}, "body": {"type": "string"}},
}
# The state collection of each tool that returns the one record its argument names.
LOOKUPS = {"read_note": "notes"}
TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_balance",
        "description": "Read the balance of an account.",
        "inputSchema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}},
            "required": ["account_id"],
        },
        "outputSchema": _ACCOUNT,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_notes",
        "description": "List the notes kept with the accounts.",
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {
                "notes": {"type": "array", "items": _NOTE},
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "read_note",
        "description": "Read one note by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"note_id": {"type": "string"}},
            "required": ["note_id"],
        },
        "outputSchema": _NOTE,
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "withdraw",
        "description": "Withdraw an amount from an account, up to its balance.",
        "inputSchema": {
            "type": "object",
            "properties": {"account_id": {"type": "string"}, "amount": {"type": "number"}},
            "required": ["account_id", "amount"],
        },
        "outputSchema": _ACCOUNT,
    },
]


class ToolError(Exception):
    """A call the tool refuses; MCP returns it as a result with ``isError``."""


class SeedError(ValueError):
    """A seed document the declared slots do not accept; nothing is written."""


def declared_slots() -> list[dict[str, Any]]:
    """Return the seed slots of ``runtime-spec.yaml``."""

    import yaml

    spec = yaml.safe_load((HERE / "runtime-spec.yaml").read_text(encoding="utf-8"))
    return spec["seed_slots"]


def slot_place(slot: dict[str, Any]) -> tuple[str, str]:
    """Return the state collection a seed slot's records are in, and their planted attribute."""

    collection, listed, attribute = slot["field"].partition("[].")
    if listed:
        return collection, attribute
    return LOOKUPS[slot["carrier_operation"]], slot["field"]


class Target:
    """The tools over a copy of ``INITIAL_STATE``; ``slots`` is None for the bare variant."""

    def __init__(self, slots: list[dict[str, Any]] | None) -> None:
        self.slots = None if slots is None else {slot["id"]: slot for slot in slots}
        self.state = copy.deepcopy(INITIAL_STATE)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "list_notes":
            return {"notes": [dict(note) for note in self.state["notes"].values()]}
        if name == "read_note":
            note = self.state["notes"].get(arguments.get("note_id"))
            if note is None:
                raise ToolError(f"no note {arguments.get('note_id')!r}")
            return dict(note)
        account = self.state["accounts"].get(arguments.get("account_id"))
        if account is None:
            raise ToolError(f"no account {arguments.get('account_id')!r}")
        if name == "withdraw":
            amount = arguments.get("amount")
            if (
                isinstance(amount, bool)
                or not isinstance(amount, int | float)
                or not 0 < amount <= account["balance"]
            ):
                raise ToolError(f"cannot withdraw {amount!r} from {account['account_id']}")
            account["balance"] -= amount
        return dict(account)

    def rpc(self, message: Any) -> dict[str, Any] | None:
        """Answer one JSON-RPC message; a notification (no ``id``) has no answer."""

        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return _error(None, -32600, "not a JSON-RPC request")
        if "id" not in message:
            return None
        method, params = message["method"], message.get("params") or {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            result: dict[str, Any] = {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "piggybank", "version": "1"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in {tool["name"] for tool in TOOLS}:
                return _error(message["id"], -32602, f"unknown tool {name!r}")
            try:
                payload = self.call(name, params.get("arguments") or {})
            except ToolError as exc:
                result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
            else:
                text = json.dumps(payload)
                result = {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": payload,
                    "isError": False,
                }
        else:
            return _error(message["id"], -32601, f"unknown method {method!r}")
        return {"jsonrpc": "2.0", "id": message["id"], "result": result}

    def seed(self, document: Any) -> None:
        """Plant every item of an ``artifact-seed-v1`` document, or none of them."""

        assert self.slots is not None
        if not isinstance(document, dict) or document.get("schema_version") != SEED_VERSION:
            raise SeedError(f"the seed is not {SEED_VERSION}")
        items = document.get("items")
        if not isinstance(items, list) or not items:
            raise SeedError("the seed has no items")
        writes = []
        for index, item in enumerate(items):
            slot = self.slots.get(item.get("slot")) if isinstance(item, dict) else None
            if slot is None:
                raise SeedError(f"item {index} names no declared seed slot")
            if document.get("carrier_operation") != slot["carrier_operation"]:
                raise SeedError(f"item {index}: the carrier is not {slot['carrier_operation']}")
            if item.get("record_ref") not in slot["records"]:
                raise SeedError(f"item {index}: {item.get('record_ref')!r} is not a slot record")
            if item.get("mode") != slot["mode"]:
                raise SeedError(f"item {index}: the slot's mode is {slot['mode']}")
            content = item.get("content")
            if not isinstance(content, str) or not 0 < len(content) <= slot["max_chars"]:
                raise SeedError(f"item {index}: content must be 1 to {slot['max_chars']} chars")
            collection, attribute = slot_place(slot)
            record = self.state[collection][item["record_ref"]]
            writes.append((record, attribute, item["mode"], content))
        for record, attribute, mode, content in writes:
            record[attribute] = content if mode == "replace" else f"{record[attribute]} {content}"

    def reset(self) -> None:
        self.state = copy.deepcopy(INITIAL_STATE)


def _error(request_id: Any, code: int, text: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": text}}


def handler(target: Target) -> type[BaseHTTPRequestHandler]:
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"status": "ok"})
            else:
                self._send(404, {"error": f"no route GET {self.path}"})

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                body = json.loads(raw or b"null")
            except json.JSONDecodeError:
                self._send(400, _error(None, -32700, "not JSON"))
                return
            with lock:
                if self.path == "/mcp":
                    reply = target.rpc(body)
                    self._send(202, None) if reply is None else self._send(200, reply)
                elif self.path in ("/seed", "/reset") and target.slots is not None:
                    try:
                        target.seed(body) if self.path == "/seed" else target.reset()
                    except SeedError as exc:
                        self._send(400, {"error": str(exc)})
                    else:
                        self._send(200, {"status": self.path[1:]})
                else:
                    self._send(404, {"error": f"no route POST {self.path}"})

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(self, status: int, body: Any) -> None:
            data = b"" if body is None else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


@contextlib.contextmanager
def started(
    command: list[str], *, cwd: Path | None = None, timeout: float = 10.0, grace: float = 5.0
) -> Iterator[str]:
    """Run ``command`` until the block ends; yield the base URL it prints on its first line.

    A child that prints nothing within ``timeout`` seconds is an error. On the way out the
    child is terminated, killed if it outlives ``grace`` seconds, and always reaped; its pipe
    is closed, and the thread reading it is joined within ``grace`` seconds or an error.
    """

    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE)
    assert process.stdout is not None
    lines: list[bytes] = []
    reader = threading.Thread(target=lambda: lines.append(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        reader.join(timeout)
        if reader.is_alive():
            raise RuntimeError(f"the fixture target printed no URL within {timeout} seconds")
        url = lines[0].decode("utf-8").strip()
        if not url:
            raise RuntimeError("the fixture target exited before it served")
        yield url
    finally:
        try:
            process.terminate()
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        finally:
            process.stdout.close()
        reader.join(grace)
        if reader.is_alive():
            raise RuntimeError("the fixture target's output reader outlived the child")


@contextlib.contextmanager
def running(*, bare: bool = False) -> Iterator[str]:
    """Run the target in a subprocess on a free localhost port; yield its base URL."""

    command = [sys.executable, str(HERE / "server.py"), "serve", "--port", "0"]
    with started([*command, *(["--bare"] if bare else [])]) as url:
        yield url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A tiny MCP target for the adapter tests.")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="serve; print the base URL on the first line")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--bare", action="store_true", help="no declaration, seed or reset")
    for name in ("health", "seed", "reset"):
        commands.add_parser(name).add_argument("--url", required=True, help="the base URL")
    commands.choices["seed"].add_argument("seed_file", type=Path)
    args = parser.parse_args(argv)
    if args.command == "serve":
        target = Target(None if args.bare else declared_slots())
        server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(target))
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
        return 0
    body = {"health": None, "seed": b"", "reset": b"{}"}[args.command]
    if args.command == "seed":
        body = args.seed_file.read_bytes()
    request = urllib.request.Request(
        f"{args.url}/{args.command}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8"), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"cannot reach {args.url}: {exc.reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
