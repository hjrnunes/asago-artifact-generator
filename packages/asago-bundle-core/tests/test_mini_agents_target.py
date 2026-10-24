"""One setup call over SSE to a real mini-agents MCP server, started from a sibling checkout.

The mini-agents checkout (``ASAGO_MINI_AGENTS``, else ``mini-agents`` beside the directory
that holds this repository's checkout or one of its parents) serves its MCP tools over the
legacy SSE transport at ``/sse``. The test starts its klarna domain on a free local port with
that checkout's own environment, read only (``uv run --frozen --no-sync``, no bytecode
written), and looks up a seeded order: the tool reads in-memory state and calls no model. It
skips when the checkout, its environment or ``uv`` is absent.
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
def mini_agents_sse() -> Iterator[str]:
    """The klarna domain's MCP server running on a free port, as its SSE URL."""

    checkout = _checkout()
    if checkout is None:
        pytest.skip("no mini-agents checkout beside this one (set ASAGO_MINI_AGENTS)")
    if not (checkout / ".venv").is_dir():
        pytest.skip(f"the mini-agents checkout has no environment: {checkout / '.venv'}")
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    port = _free_port()
    server = subprocess.Popen(
        [
            *(uv, "run", "--frozen", "--no-sync", "--project", str(checkout)),
            *("python", "-m", "mini_agents", "--domain", "klarna"),
            *("--host", "127.0.0.1", "--port", str(port)),
        ],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + STARTUP_SECONDS
        while True:
            if server.poll() is not None:
                error = server.stderr.read().decode(errors="replace") if server.stderr else ""
                pytest.skip(f"the mini-agents server did not start: {error.strip()[-500:]}")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail("the mini-agents server did not listen in time")
                time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/sse"
    finally:
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
    # FastMCP reports the tool's JSON string both as text and wrapped as structured content.
    assert native["structuredContent"] == {"result": block["text"]}
