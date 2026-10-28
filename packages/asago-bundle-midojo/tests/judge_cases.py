"""A fake model and the judge service on 127.0.0.1, and judge requests as MiDojo builds them.

``midojo_request`` builds the ``midojo.judge.v1`` body MiDojo's ``reply_judge`` check
(``midojo.verifiers.judge.ReplyJudge.request``, asago/main 4e5176a) sends for a recorded
conversation and its calls, with ``prior_turns`` and ``tool_calls`` on, as compile sets
them. No test reaches a model: the model is a stub that answers canned replies.
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from asago_bundle_midojo.judge_service import SCHEMA, make_server, model_calls

TOKEN = "service-token"
MODEL = "judge-model"
SUPPORTED = json.dumps({"verdict": "supported", "evidence_refs": ["messages[0].content"]})
CONTRADICTED = json.dumps({"verdict": "contradicted", "evidence_refs": ["messages[0].content"]})


@dataclass
class Model:
    """What the fake model answers, and every request it got."""

    status: int = 200
    body: bytes = b""
    delay: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)
    seen: list[dict[str, Any]] = field(default_factory=list)

    def bodies(self) -> list[dict[str, Any]]:
        return [request["body"] for request in self.seen]


def completion(content: Any) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]}
    ).encode()


@contextmanager
def loopback(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    """Serve ``handler`` on a free port of 127.0.0.1 and yield the port."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def model_server(model: Model) -> Iterator[str]:
    """Run the fake model; yield its OpenAI-compatible base URL."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            model.seen.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            if model.delay:
                threading.Event().wait(model.delay)
            self.send_response(model.status)
            for name, value in model.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(model.body)))
            self.end_headers()
            self.wfile.write(model.body)

        def log_message(self, *args: Any) -> None:
            return

    with loopback(Handler) as port:
        yield f"http://127.0.0.1:{port}/v1"


@contextmanager
def judge_service(model_url: str, token: str = TOKEN) -> Iterator[str]:
    """Run the judge service in this process in front of ``model_url``; yield its URL."""

    server = make_server(0, model_calls(model_url, MODEL), MODEL, token)
    server.RequestHandlerClass.log_message = lambda *args: None  # type: ignore[method-assign]
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/judge"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def post(url: str, body: Any, token: str | None = TOKEN) -> tuple[int, Any]:
    """POST ``body`` as JSON (bytes as they are) and return the status and decoded answer."""

    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def recorded_call(call: dict[str, Any]) -> dict[str, Any]:
    """A call as MiDojo sends it: every field, finished and not blocked unless it says."""

    return {
        "function": call["function"],
        "args": call.get("args", {}),
        "result": call.get("result"),
        "error": call.get("error"),
        "complete": call.get("complete", True),
        "blocked": call.get("blocked", False),
        "turn": call.get("turn"),
    }


def midojo_request(
    claim: str,
    rubric: Any,
    turns: list[dict[str, Any]],
    calls: list[dict[str, Any]] | None = None,
    complete: bool = True,
) -> dict[str, Any]:
    """The request MiDojo's ``reply_judge`` sends: the last turn's message, the judged reply
    (the last turn's), the earlier turns, every recorded call and whether they are all."""

    recorded = [recorded_call(call) for call in calls or []]
    body = {
        "claim": claim,
        "rubric": rubric,
        "message": turns[-1]["message"] if turns else None,
        "reply": turns[-1]["reply"] if turns else "",
        "prior_turns": [{"message": t["message"], "reply": t["reply"]} for t in turns[:-1]],
        "tool_calls": recorded,
        "tool_calls_complete": complete and all(call["complete"] for call in recorded),
    }
    text = json.dumps(
        {"schema": SCHEMA, **body}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return {"schema": SCHEMA, "id": "sha256:" + hashlib.sha256(text.encode()).hexdigest(), **body}


def rubric(
    turns: list[str], judge_turns: str = "final", runtime_facts: Any = None, **standard: Any
) -> dict[str, Any]:
    """The rubric compile writes for a reply episode."""

    return {
        "criteria": standard.get("criteria", "Yes when the reply says so."),
        "facts": standard.get("facts", []),
        "runtime_facts": runtime_facts if runtime_facts is not None else {},
        "turns": turns,
        "judge_turns": judge_turns,
    }
