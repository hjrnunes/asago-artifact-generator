"""Run Garak's real ``midojo.InjectionSpecProbe`` against a scripted target for one fixture case.

``garak_ipi.py`` starts this script with the Garak interpreter, one process per case, and
reads what it writes. It is not meant to be run by hand. The one argument is a JSON file::

    {
      "target_dir": "<the fixture target's directory: server.py and target.yaml>",
      "entry": "<the probe's source file: one {injection_spec, placements, metadata} entry>",
      "turns": [{"calls": [["tool", {"argument": "value"}]], "reply": "the agent's answer"}],
      "hold": false,
      "tamper": null,
      "out": "<where to write the report's attempt lines>"
    }

Everything the report states is the probe's and its detectors': the script serves only what
the probe's environment must supply. A fixture target serves the tools; a loopback Responses
endpoint plays the agent, calling the scripted tools through the interceptor URL and bearer
token it finds in the request's tool block, as a gateway's MCP client does; a loopback service
answers a reply judge. The script then runs Garak's own command line with the run
configuration the bundle writes, and keeps the report's ``attempt`` lines.

The script pins what a run draws at random or reads from the clock: MiDojo's clock, evaluation
IDs and call IDs (the pins MiDojo's own ``tests/fixtures/inconclusive_vectors/generate.py``
uses), and the attempt UUID, which Garak draws from the same source. The interceptor's port
and bearer token, and the judge's port, stay live in the output; ``garak_ipi.py`` pins them.

``hold`` makes the target hold every ``withdraw`` open until the run ends, and the agent give
up on it after a second. ``tamper`` is the one place this script changes what the probe
reports: its value replaces the first turn's reply in the snapshot the probe sealed, after the
seal and before the detectors read it, so the detectors refuse a snapshot that no longer
hashes to its ID.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SPEC = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
SCRATCH = tempfile.TemporaryDirectory(prefix="garak-ipi-probe-")
# Garak writes its log under the user's data directory; keep every write in the scratch.
for _name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
    os.environ[_name] = str(Path(SCRATCH.name) / _name.lower())

import yaml  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StreamableHttpTransport  # noqa: E402

PINNED_START = datetime(2026, 1, 1, tzinfo=UTC)
PINNED_STEP = timedelta(milliseconds=1)
PINNED_UUID = 0x10000000000040008000000000000000
HELD_CALL_SECONDS = 1.0
JUDGE_RATIONALE = "The reply says the withdrawal the planted instruction asked for happened."
JUDGE_MODEL = "scripted-judge"
JUDGE_TOKEN = "scripted-judge-token"
PROBE = "midojo.InjectionSpecProbe"
DETECTORS = "midojo.EvaluationVerdict,midojo.EvaluationEffect,toolcall.ToolCallCondition"


def load_target() -> Any:
    path = Path(SPEC["target_dir"]) / "server.py"
    spec = importlib.util.spec_from_file_location("fixture_target_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """The OpenAI client drops its keep-alive connection at exit."""


def serve(handler: type[BaseHTTPRequestHandler]) -> Server:
    httpd = Server(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def pin_midojo() -> None:
    """Pin the clock, evaluation IDs and call IDs MiDojo draws; nothing else changes.

    The clock ticks once per recorded event, so two runs of a case write the same times.
    """

    from midojo.control_plane import lifecycle, service, state, store

    ticks = iter(range(10**9))
    names = iter(range(10**9))
    evaluations = iter(range(1, 10**9))

    def now() -> str:
        return (PINNED_START + PINNED_STEP * next(ticks)).isoformat()

    def uuid4() -> uuid.UUID:
        return uuid.UUID(int=PINNED_UUID + next(names))

    def token_hex(size: int = 5) -> str:
        return f"{next(evaluations):0{2 * size}x}"

    def evaluation(self: Any, target: Any) -> Any:
        # The closure wait reads the evaluation once per poll while a held call is pending, and
        # the number of polls that fit in the wait depends on the machine. A read states a
        # fixed time instead of drawing a tick, so the pinned clock advances with recorded
        # events only.
        return self.apply(target, lifecycle.Read(PINNED_START.isoformat()))

    state._now = service._now = now  # type: ignore[assignment]
    service.ControlPlane.evaluation = evaluation  # type: ignore[method-assign]
    uuid.uuid4 = uuid4  # type: ignore[assignment]
    store.secrets.token_hex = token_hex  # type: ignore[assignment]


def agent(turns: list[dict[str, Any]], hold: bool) -> type[BaseHTTPRequestHandler]:
    """A Responses endpoint whose request ``n`` makes ``turns[n]``'s calls and replies."""

    requests: list[dict[str, Any]] = []

    async def act(tool: dict[str, Any], calls: list[list[Any]]) -> None:
        headers = {"Authorization": f"Bearer {tool['authorization']}"}
        async with Client(StreamableHttpTransport(tool["server_url"], headers=headers)) as client:
            listed = {item.name for item in await client.list_tools()}
            assert set(tool["allowed_tools"]) <= listed, "the interceptor lists every allowed tool"
            for name, arguments in calls:
                assert name in tool["allowed_tools"], name
                if hold and name == "withdraw":
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            client.call_tool_mcp(name, arguments), HELD_CALL_SECONDS
                        )
                else:
                    await client.call_tool_mcp(name, arguments)

    class Responses(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            number = len(requests)
            requests.append(body)
            (tool,) = body["tools"]
            turn = turns[number]
            asyncio.run(act(tool, turn["calls"]))
            answer = {
                "id": f"resp-{number}",
                "object": "response",
                "created_at": 0,
                "model": "scripted",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": f"msg-{number}",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": turn["reply"], "annotations": []}
                        ],
                    }
                ],
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
            }
            data = json.dumps(answer).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: Any) -> None:
            """Keep the agent's requests out of the output."""

    return Responses


class Judge(BaseHTTPRequestHandler):
    """Answer every judge request ``supported``."""

    def do_POST(self) -> None:  # noqa: N802 - http.server's spelling
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        answer = {
            "schema": "midojo.judge.v1",
            "id": request["id"],
            "verdict": "supported",
            "rationale": JUDGE_RATIONALE,
            "model": JUDGE_MODEL,
        }
        body = json.dumps(answer).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        """Keep the judge's requests out of the output."""


def tamper_seal(reply: str) -> None:
    """Edit the first turn's reply in every snapshot the probe seals, after the seal."""

    from garak.probes import midojo as probe_module

    sealed = probe_module.InjectionSpecProbe._seal_trace

    def edited(self: Any, handle: Any, plan: Any) -> dict[str, Any]:
        trace = sealed(self, handle, plan)
        trace["snapshot"]["turns"][0]["reply"] = reply
        return trace

    probe_module.InjectionSpecProbe._seal_trace = edited


def run_config(gateway: str, mcp_url: str, reports: Path) -> dict[str, Any]:
    """The Garak run the bundle's template describes, pointed at the loopback endpoints."""

    return {
        "system": {"lite": False, "parallel_attempts": False, "parallel_requests": False},
        "run": {"generations": 1},
        "reporting": {"report_dir": str(reports), "report_prefix": "run"},
        "plugins": {
            "generators": {
                "openai": {
                    "OpenAIResponsesGenerator": {
                        "uri": gateway,
                        "max_tokens": 512,
                        "extra_params": {"tool_choice": "auto"},
                        "max_retries": 0,
                        "client_options": {"timeout": 30},
                    }
                }
            },
            "probes": {
                "midojo": {
                    "InjectionSpecProbe": {"source_path": SPEC["entry"], "upstream": mcp_url}
                }
            },
            "detectors": {
                "midojo": {"EvaluationVerdict": {}, "EvaluationEffect": {}},
                "toolcall": {"ToolCallCondition": {}},
            },
        },
    }


def main() -> int:
    pin_midojo()
    fixture = load_target()
    base = fixture.Target
    if SPEC["hold"]:
        release = threading.Event()

        class Held(base):
            def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                if name == "withdraw":
                    release.wait()
                return super().call(name, arguments)

        base = Held
    target = serve(fixture.handler(base(fixture.declared_slots())))
    config = yaml.safe_load((Path(SPEC["target_dir"]) / "target.yaml").read_text("utf-8"))
    mcp_url = config["endpoint"]["mcp_url"].replace("{port}", str(target.server_port))
    gateway = f"http://127.0.0.1:{serve(agent(SPEC['turns'], SPEC['hold'])).server_port}/v1"

    entries = json.loads(Path(SPEC["entry"]).read_text(encoding="utf-8"))
    check = (entries[0]["injection_spec"].get("security") or {}).get("reply_judge")
    if check:
        judge = serve(Judge)
        os.environ[check["url_env"]] = f"http://127.0.0.1:{judge.server_port}/judge"
        os.environ[check["token_env"]] = JUDGE_TOKEN
    if SPEC["tamper"] is not None:
        tamper_seal(SPEC["tamper"])

    reports = Path(SCRATCH.name) / "reports"
    configuration = Path(SCRATCH.name) / "run.yaml"
    configuration.write_text(yaml.safe_dump(run_config(gateway, mcp_url, reports)), "utf-8")
    os.environ["OPENAI_API_KEY"] = "scripted"
    from garak import cli

    arguments = [
        "--config",
        str(configuration),
        "--target_type",
        "openai.OpenAIResponsesGenerator",
        "--target_name",
        "scripted",
        "--probes",
        PROBE,
        "--detectors",
        DETECTORS,
    ]
    try:
        cli.main(arguments)
    except SystemExit as stopped:
        if stopped.code not in (None, 0):
            print(f"garak stopped with {stopped.code!r}", file=sys.stderr)
            return 1
    finally:
        if SPEC["hold"]:
            release.set()
    report = reports / "run.report.jsonl"
    if not report.is_file():
        print(f"garak wrote no report at {report}", file=sys.stderr)
        return 1
    lines = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
    attempts = [line for line in lines if line.get("entry_type") == "attempt"]
    Path(SPEC["out"]).write_text(
        "".join(json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n" for line in attempts),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
