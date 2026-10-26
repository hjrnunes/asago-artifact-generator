"""One setup call over SSE to a real mini-agents MCP server, started from a sibling checkout.

The mini-agents checkout (``ASAGO_MINI_AGENTS``, else ``mini-agents`` beside the directory
that holds this repository's checkout or one of its parents) serves its MCP tools over the
legacy SSE transport at ``/sse``. The test starts its klarna domain on a free local port with
that checkout's own environment, read only (``uv run --frozen --no-sync``, no bytecode
written), and looks up a seeded order: the tool reads in-memory state and calls no model. It
skips when the checkout, its environment or ``uv`` is absent, and fails, showing the server's
output, when the server does not start. The free port is released before the server binds
it, so a server that exits while starting is tried again on a fresh port, a bounded number of
times.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from asago_bundle_core.preparation import ToolResult
from asago_bundle_core.target import mcp_call_target

HERE = Path(__file__).resolve()
STARTUP_SECONDS = 60.0
START_ATTEMPTS = 3


def _checkout() -> Path | None:
    named = os.environ.get("ASAGO_MINI_AGENTS")
    candidates = (
        [Path(named)] if named else [parent / "mini-agents" for parent in HERE.parents[1:]]
    )
    return next(
        (path for path in candidates if (path / "src/mini_agents/kernel/mcp_server.py").is_file()),
        None,
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def mini_agents_sse(tmp_path: Path) -> Iterator[str]:
    """The klarna domain's MCP server running on a free port, as its SSE URL."""

    checkout = _checkout()
    if checkout is None:
        pytest.skip("no mini-agents checkout beside this one (set ASAGO_MINI_AGENTS)")
    if not (checkout / ".venv").is_dir():
        pytest.skip(f"the mini-agents checkout has no environment: {checkout / '.venv'}")
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    output = ""
    for attempt in range(1, START_ATTEMPTS + 1):
        port = _free_port()
        log = tmp_path / f"server-{attempt}.log"
        with log.open("wb") as sink:
            server = subprocess.Popen(
                [
                    *(uv, "run", "--frozen", "--no-sync", "--project", str(checkout)),
                    *("python", "-m", "mini_agents", "--domain", "klarna"),
                    *("--host", "127.0.0.1", "--port", str(port)),
                ],
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                stdout=sink,
                stderr=subprocess.STDOUT,
            )
        try:
            if _listening(server, port, log):
                yield f"http://127.0.0.1:{port}/sse"
                return
        finally:
            _stop(server)
        output = log.read_text(errors="replace")
    pytest.fail(
        f"the mini-agents server exited while starting, {START_ATTEMPTS} times; "
        f"its last output:\n{output[-2000:]}"
    )


def _listening(server: subprocess.Popen[bytes], port: int, log: Path) -> bool:
    """Return whether ``server`` listens on ``port``; False when it exited first."""

    deadline = time.monotonic() + STARTUP_SECONDS
    while server.poll() is None:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return True
        except OSError:
            if time.monotonic() > deadline:
                pytest.fail(
                    "the mini-agents server did not listen in time; its output:\n"
                    + log.read_text(errors="replace")[-2000:]
                )
            time.sleep(0.1)
    return False


def _stop(server: subprocess.Popen[bytes]) -> None:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()


def test_a_setup_call_reaches_mini_agents_over_sse(mini_agents_sse: str) -> None:
    value = mcp_call_target(mini_agents_sse)("lookup_order", {"order_id": "ORD-101"})

    assert isinstance(value, ToolResult)
    assert value["order_id"] == "ORD-101"
    native = value.native_result
    assert native["isError"] is False
    (block,) = native["content"]
    assert block["type"] == "text"
    assert value == json.loads(block["text"])
    # FastMCP reports the result as text and as structured content: the object itself for a
    # tool that declares an output schema, else the JSON string wrapped as {"result": ...}.
    assert native["structuredContent"] in (dict(value), {"result": block["text"]})
