"""Grade the sealed reply runs with MiDojo 4e5176a's ``reply_judge`` and this adapter's judge.

Run with MiDojo's interpreter at 4e5176a, naming that revision, since the interpreter may
run from an export with no Git history. With an export of 4e5176a in an empty directory
(``git -C <midojo> archive 4e5176a | tar -x -C <dir>``, then ``uv sync --frozen --extra
dev`` there), and the reply case compiled by this adapter (``tests/episode_cases.py``)::

    cd <that dir> && uv run --frozen --extra dev python \\
        <consumer>/tools/fixture-generators/reply_judge_4e5176a.py \\
        <the full 4e5176a SHA> <compiled reply-single dir> \\
        <consumer>/packages/asago-bundle-midojo/tests/fixtures/regraded-13d5487/reply-single \\
        <consumer>/.venv/bin/asago-bundle-midojo <empty dir>

then copy ``<empty dir>`` over ``fixtures/reply-judge-4e5176a/``.

It runs no episode. It builds ``midojo.episode.Episode`` from the compiled
``episode.json`` (the ``reply_judge`` check is parsed then), starts the adapter's judge
service with ``judge-serve --port 0`` in front of a fake OpenAI-compatible model on
127.0.0.1 that answers a scripted reply and keeps each request, sets ``JUDGE_URL`` and
``JUDGE_TOKEN`` as a grading process would, and grades each scenario's sealed snapshot
with ``midojo.grade``, reading it back as ``ControlClient.snapshot`` does. It writes the
compiled ``episode.json``, and per scenario ``snapshot.json``, ``verdict.json`` (the grade
with MiDojo's ``judgements``: each ``midojo.judge.v1`` request MiDojo sent and the
service's answer) and ``model-requests.json`` (the bodies the fake model got, in order).

- ``supported``: the sealed ``supported`` run, the model answering supported;
- ``contradicted``: the same run, the model answering contradicted;
- ``unresolved``: the sealed ``unresolved`` run, the model answering text that is not JSON;
- ``blocked-call``: the ``supported`` run with its ``withdraw`` call blocked (no result),
  named by MiDojo's encoding of its content. MiDojo reads the security check again without
  the blocked call, so it sends two requests and the service answers each;
- ``deep-args``: the ``supported`` run with its ``list_notes`` call's arguments nested 80
  objects deep, named the same way. The model sees them all, and MiDojo accepts the
  service's answer, whose ``details`` stay within its nesting limit of 64.

No target or real model is involved; nothing leaves the process but loopback connections.
``recording.json`` names the revision given and, per scenario, the security grade, the
judgements' request IDs and verdicts, and the number of model requests.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import midojo
from midojo.control_plane.client import snapshot_from_response
from midojo.control_plane.models import SnapshotResponse
from midojo.control_plane.state import _canonical, _dumps
from midojo.episode import Episode
from midojo_sdk.injection import InjectionPlan

SUPPORTED = json.dumps({"verdict": "supported", "evidence_refs": ["messages[0].content"]})
CONTRADICTED = json.dumps({"verdict": "contradicted", "evidence_refs": ["messages[0].content"]})
# How deep the deep-args scenario nests a call's arguments: past MiDojo's 64 for details.
DEEP = 80
# scenario -> (sealed run, the model's reply, the change made to the run, if any)
SCENARIOS = {
    "supported": ("supported", SUPPORTED, None),
    "contradicted": ("supported", CONTRADICTED, None),
    "unresolved": ("unresolved", "not json", None),
    "blocked-call": ("supported", SUPPORTED, "blocked"),
    "deep-args": ("supported", SUPPORTED, "deep"),
}
MODEL = "fixture-judge"
KEY = "fixture-model-key"
TOKEN = "fixture-service-token"


class FakeModel:
    """The scripted reply, and every request body the model got."""

    reply = SUPPORTED

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.auth: list[str | None] = []


@contextmanager
def model_server(model: FakeModel) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            model.bodies.append(body)
            model.auth.append(self.headers.get("Authorization"))
            content = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": model.reply}}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def judge_service(command: str, model_url: str) -> Iterator[str]:
    environ = {
        **os.environ,
        "JUDGE_MODEL_URL": model_url,
        "JUDGE_MODEL": MODEL,
        "OPENAICOMPATIBLE_API_KEY": KEY,
        "JUDGE_SERVICE_TOKEN": TOKEN,
    }
    process = subprocess.Popen(
        [command, "judge-serve", "--port", "0"], env=environ, stdout=subprocess.PIPE, text=True
    )
    try:
        ready = process.stdout.readline()
        assert ready.startswith("judge service on http://127.0.0.1:"), ready
        yield ready.split(" on ", 1)[1].strip() + "judge"
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
        assert process.returncode == 0, process.returncode


def episode(data: dict[str, Any]) -> Episode:
    return Episode(
        runtime=data["runtime"],
        turns=data["turns"],
        plan=InjectionPlan.from_data(data["plan"]),
        security=data["security"],
        utility=data["utility"],
        environment=data["environment"],
        suite_name=data["suite_name"],
        user_task_id=data["user_task_id"],
        injection_task_id=data["injection_task_id"],
    )


def changed_run(response: dict[str, Any], change: str) -> dict[str, Any]:
    """``response`` with its withdraw call blocked, or its list_notes call's arguments
    nested ``DEEP`` objects deep, named by its content."""

    changed = copy.deepcopy(response)
    for call in changed["function_calls"]:
        if change == "blocked" and call["function"] == "withdraw":
            call.update(blocked=True, disposition="blocked", result=None)
        if change == "deep" and call["function"] == "list_notes":
            nested: Any = "bottom"
            for _ in range(DEEP):
                nested = {"next": nested}
            call["args"] = {"filter": nested}
    content = {key: value for key, value in changed.items() if key != "snapshot_id"}
    changed["snapshot_id"] = (
        "sha256:" + hashlib.sha256(_dumps(_canonical(content)).encode()).hexdigest()
    )
    return changed


def write(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def main(revision: str, compiled: Path, sealed: Path, command: str, out: Path) -> None:
    document = json.loads((compiled / "episode.json").read_text())
    built = episode(document["episode"])
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(compiled / "episode.json", out / "episode.json")
    model = FakeModel()
    recorded: dict[str, Any] = {"revision": revision, "scenarios": {}}
    with model_server(model) as model_url, judge_service(command, model_url) as url:
        os.environ["JUDGE_URL"] = url
        os.environ["JUDGE_TOKEN"] = TOKEN
        for scenario, (run, reply, change) in SCENARIOS.items():
            response = json.loads((sealed / run / "snapshot.json").read_text())
            if change:
                response = changed_run(response, change)
            snapshot = snapshot_from_response(
                SnapshotResponse.model_validate(response), built.environment_type
            )
            model.reply = reply
            start = len(model.bodies)
            verdict = midojo.grade(built, snapshot)
            written = {
                "evaluation_id": verdict.evaluation_id,
                "snapshot_id": verdict.snapshot_id,
                "grade": verdict.grade.model_dump(mode="json"),
            }
            bodies = model.bodies[start:]
            assert set(model.auth) == {f"Bearer {KEY}"}
            assert KEY not in json.dumps(written)
            write(out / scenario / "snapshot.json", response)
            write(out / scenario / "verdict.json", written)
            write(out / scenario / "model-requests.json", bodies)
            recorded["scenarios"][scenario] = {
                "security": written["grade"]["security"],
                "judgements": [
                    {"request_id": item["request_id"], "verdict": item["verdict"]}
                    for item in written["grade"].get("judgements", [])
                ],
                "model_requests": len(bodies),
            }
    write(out / "recording.json", recorded)


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], Path(sys.argv[5]))
