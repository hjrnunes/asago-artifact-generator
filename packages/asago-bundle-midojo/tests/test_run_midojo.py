"""The run script reads the control plane before the serve process stops."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_midojo.bundle_files import run_midojo

RUN = {
    "id": "run-1",
    "suite_name": "asago_suite.suite",
    "created_at": "2026-10-09T10:00:00Z",
    "evaluations": [
        {
            "id": "eval-1",
            "user_task_id": "asago_user_task",
            "injection_task_id": "asago_injection_task",
            "completed": True,
            "utility": True,
            "security": True,
        }
    ],
}
EVALUATION = {
    "id": "eval-1",
    "user_task_id": "asago_user_task",
    "injection_task_id": "asago_injection_task",
    "completed": True,
    "utility": True,
    "security": True,
    "security_reason": "the condition holds on the captured calls",
    "agent_input": "Show PAT-201.",
    "agent_output": "Done.",
    "function_calls": [],
}


@contextmanager
def control_plane(routes: dict[str, Any]) -> Iterator[str]:
    """A stub control plane on loopback that answers ``routes`` and 404 elsewhere."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            document = routes.get(self.path)
            body = json.dumps(document).encode()
            self.send_response(200 if document is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def orchestrator(rc: int, results: dict[str, Any] | None):
    """A stand-in for ``midojo-run``: it writes ``results.json`` and returns ``rc``."""

    def run(control_url: str, flags: list[str], bundle: Path) -> int:
        if results is not None:
            logs = bundle / "midojo-logs"
            logs.mkdir(parents=True, exist_ok=True)
            (logs / "results.json").write_text(json.dumps(results), encoding="utf-8")
        return rc

    return run


def argv(bundle: Path, url: str) -> list[str]:
    return ["--bundle", str(bundle), "--control-url", url, "--", "--suite", "asago_suite.suite"]


def test_the_control_plane_is_dumped_into_the_bundle(tmp_path: Path) -> None:
    routes = {"/runs/run-1": RUN, "/runs/run-1/evaluations/eval-1": EVALUATION}
    with control_plane(routes) as url:
        rc = run_midojo.main(argv(tmp_path, url), orchestrator(0, {"run_id": "run-1"}))

    assert rc == 0
    assert json.loads((tmp_path / "control-plane/run.json").read_text()) == RUN
    assert json.loads((tmp_path / "control-plane/evaluation.json").read_text()) == EVALUATION


def test_a_run_with_two_evaluations_dumps_the_run_and_fails(tmp_path: Path) -> None:
    run = {**RUN, "evaluations": [*RUN["evaluations"], {**RUN["evaluations"][0], "id": "eval-2"}]}
    with control_plane({"/runs/run-1": run}) as url:
        rc = run_midojo.main(argv(tmp_path, url), orchestrator(0, {"run_id": "run-1"}))

    assert rc == 1
    assert (tmp_path / "control-plane/run.json").is_file()
    assert not (tmp_path / "control-plane/evaluation.json").exists()


def test_a_failed_orchestrator_without_results_leaves_no_dump_and_keeps_its_code(
    tmp_path: Path,
) -> None:
    with control_plane({}) as url:
        rc = run_midojo.main(argv(tmp_path, url), orchestrator(2, None))

    assert rc == 2
    assert not (tmp_path / "control-plane").exists()


def test_a_failed_orchestrator_with_results_is_still_dumped_and_keeps_its_code(
    tmp_path: Path,
) -> None:
    routes = {"/runs/run-1": RUN, "/runs/run-1/evaluations/eval-1": EVALUATION}
    with control_plane(routes) as url:
        rc = run_midojo.main(argv(tmp_path, url), orchestrator(3, {"run_id": "run-1"}))

    assert rc == 3
    assert (tmp_path / "control-plane/evaluation.json").is_file()


@pytest.mark.parametrize("results", ["not json", {"run_id": 7}, {"no": "id"}, ["list"]])
def test_unusable_results_leave_no_dump_and_fail(tmp_path: Path, results: Any) -> None:
    def write(control_url: str, flags: list[str], bundle: Path) -> int:
        logs = bundle / "midojo-logs"
        logs.mkdir(parents=True)
        text = results if isinstance(results, str) else json.dumps(results)
        (logs / "results.json").write_text(text, encoding="utf-8")
        return 0

    with control_plane({}) as url:
        rc = run_midojo.main(argv(tmp_path, url), write)

    assert rc == 1
    assert not (tmp_path / "control-plane").exists()


def test_an_unknown_run_fails_without_a_dump(tmp_path: Path) -> None:
    with control_plane({}) as url:
        rc = run_midojo.main(argv(tmp_path, url), orchestrator(0, {"run_id": "run-1"}))

    assert rc == 1
    assert not (tmp_path / "control-plane").exists()


def test_an_unreachable_control_plane_fails_without_a_dump(tmp_path: Path) -> None:
    rc = run_midojo.main(
        argv(tmp_path, "http://127.0.0.1:9"), orchestrator(0, {"run_id": "run-1"})
    )

    assert rc == 1
    assert not (tmp_path / "control-plane").exists()


def test_the_arguments_split_at_the_double_dash(tmp_path: Path) -> None:
    options, flags = run_midojo.parse_args(
        ["--bundle", str(tmp_path), "--control-url", "http://127.0.0.1:1", "--", "--a", "b"]
    )

    assert options.bundle == tmp_path
    assert options.control_url == "http://127.0.0.1:1"
    assert flags == ["--a", "b"]


def test_the_orchestrator_command_runs_midojo_in_this_interpreter() -> None:
    command = run_midojo.orchestrator_command("http://127.0.0.1:1", ["--suite", "s"])

    assert command == [
        sys.executable,
        "-m",
        "midojo.orchestrator",
        "--control-url",
        "http://127.0.0.1:1",
        "--suite",
        "s",
    ]


def test_the_child_sees_the_bundle_on_its_python_path(tmp_path: Path) -> None:
    kept = os.pathsep.join(["/x", "/y"])

    child = run_midojo.child_environment(tmp_path, {"PYTHONPATH": kept, "HOME": "/h"})
    bare = run_midojo.child_environment(tmp_path, {})

    assert child["PYTHONPATH"] == os.pathsep.join([str(tmp_path), "/x", "/y"])
    assert child["HOME"] == "/h"
    assert bare["PYTHONPATH"] == str(tmp_path)


def test_run_orchestrator_returns_the_child_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], env: dict[str, str], check: bool) -> Any:
        seen.update(command=command, env=env, check=check)
        return subprocess.CompletedProcess(command, 5)

    monkeypatch.setattr(run_midojo.subprocess, "run", fake_run)

    rc = run_midojo.run_orchestrator("http://127.0.0.1:1", ["--suite", "s"], tmp_path)

    assert rc == 5
    assert seen["command"][1:3] == ["-m", "midojo.orchestrator"]
    assert seen["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path)
    assert seen["check"] is False
