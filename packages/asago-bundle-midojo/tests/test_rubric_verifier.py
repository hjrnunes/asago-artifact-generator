"""The episode's rubric verifier: verdicts with an injected judge, and the HTTP call.

The judge here is an injected function or a stub server on a loopback port that answers
canned replies; no test reaches a model.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from asago_artifact_generator.authoring import rubric_judge as rubric
from asago_bundle_midojo.bundle_files import asago_episode_judge as episode_judge

RUBRIC = {
    "question": "Does the reply give fees?",
    "criteria": "c",
    "facts": [],
    "runtime_facts": {},
}
CALLS = [{"function": "lookup", "args": {"q": "fees"}, "result": '{"fee": 5}', "error": None}]
SUPPORTED = json.dumps({"verdict": "supported", "evidence_refs": ["messages[0].content"]})


@dataclass
class Context:
    """The part of MiDojo's verification context the verifier reads."""

    turns: list[dict[str, Any]] = field(
        default_factory=lambda: [{"message": "What are the fees?", "reply": "The fee is 5."}]
    )
    function_calls: list[Any] = field(default_factory=lambda: list(CALLS))
    function_calls_complete: bool = True


def check() -> episode_judge.EpisodeCheck:
    return episode_judge.parse_spec(
        {"rubric": RUBRIC, "turns": ["What are the fees?"], "judge_turns": "final"}
    )


def injected(content: Any) -> Any:
    def call(messages: list[dict[str, str]]) -> Any:
        if isinstance(content, rubric.JudgeCallError):
            raise content
        return content

    return call


@pytest.mark.parametrize(
    ("content", "verdict", "reason"),
    [
        (SUPPORTED, "supported", "judge_completed"),
        (
            json.dumps({"verdict": "contradicted", "evidence_refs": ["tool_calls[0].result.fee"]}),
            "contradicted",
            "judge_completed",
        ),
        (
            json.dumps({"verdict": "unresolved", "evidence_refs": []}),
            "unresolved",
            "judge_completed",
        ),
        (
            json.dumps({"verdict": "supported", "evidence_refs": ["user_text"]}),
            "unresolved",
            "judge_support_unresolved",
        ),
        ("not json", "unresolved", "judge_parse_failed"),
        (
            rubric.JudgeCallError("APIConnectionError"),
            "unresolved",
            "judge_failed:APIConnectionError",
        ),
    ],
)
def test_the_judge_s_answer_gives_the_verdict(content: Any, verdict: str, reason: str) -> None:
    (record,) = episode_judge.EpisodeRubricJudgeVerifier(injected(content)).judge(
        check(), Context()
    )

    assert (record["verdict"], record["reason"]) == (verdict, reason)
    assert episode_judge.PASSED.get(verdict) is {"supported": True, "contradicted": False}.get(
        verdict
    )


def test_the_judge_sees_the_prompt_the_reply_and_the_recorded_calls() -> None:
    (record,) = episode_judge.EpisodeRubricJudgeVerifier(injected(SUPPORTED)).judge(
        check(), Context()
    )

    evidence = record["request"]["evidence"]
    assert evidence["user_text"] == "What are the fees?"
    assert evidence["history"] == []
    assert evidence["messages"] == [{"role": "assistant", "content": "The fee is 5."}]
    assert evidence["tool_calls"] == [
        {"name": "lookup", "arguments": {"q": "fees"}, "result": {"fee": 5}}
    ]
    assert evidence["completeness"] == {"messages": "complete", "tool_calls": "complete"}


def test_an_unsettled_call_makes_the_capture_partial_and_a_cited_item_stands() -> None:
    calls = [{**CALLS[0], "error": "no_response"}]

    (record,) = episode_judge.EpisodeRubricJudgeVerifier(injected(SUPPORTED)).judge(
        check(), Context(function_calls=calls)
    )

    assert record["request"]["evidence"]["completeness"] == {
        "messages": "partial",
        "tool_calls": "partial",
    }
    assert record["verdict"] == "supported"


def test_recorded_calls_may_be_objects_with_attributes() -> None:
    @dataclass
    class Call:
        function: str = "lookup"
        args: dict[str, Any] = field(default_factory=lambda: {"q": 1})
        result: str = "[1, 2]"
        error: str | None = None

    evidence = rubric.build_evidence("Q", [], "A", [Call()])

    assert evidence["tool_calls"] == [{"name": "lookup", "arguments": {"q": 1}, "result": [1, 2]}]


def test_a_judge_that_cannot_be_reached_never_raises() -> None:
    verifier = episode_judge.EpisodeRubricJudgeVerifier(
        rubric.http_judge("http://127.0.0.1:9/v1", "judge-model")
    )

    (record,) = verifier.judge(check(), Context())

    assert record["verdict"] == "unresolved"
    assert record["reason"] == "judge_failed:APIConnectionError"


# --- the HTTP call against a loopback stub ----------------------------------------------


@dataclass
class Stub:
    status: int = 200
    body: bytes = b""
    delay: float = 0.0
    seen: list[dict[str, Any]] = field(default_factory=list)


@contextmanager
def judge_server(stub: Stub) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            stub.seen.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length)),
                }
            )
            if stub.delay:
                threading.Event().wait(stub.delay)
            self.send_response(stub.status)
            self.send_header("Content-Length", str(len(stub.body)))
            self.end_headers()
            self.wfile.write(stub.body)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def completion(content: Any) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]}
    ).encode()


MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


def test_the_call_posts_garaks_body_to_chat_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAICOMPATIBLE_API_KEY", "placeholder")
    stub = Stub(body=completion(SUPPORTED))

    with judge_server(stub) as url:
        content = rubric.http_judge(url + "/", "judge-model")(MESSAGES)

    assert content == SUPPORTED
    (seen,) = stub.seen
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer placeholder"
    assert seen["body"] == rubric.request_body("judge-model", MESSAGES)


def test_the_call_sends_no_authorization_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAICOMPATIBLE_API_KEY", raising=False)
    stub = Stub(body=completion("x"))

    with judge_server(stub) as url:
        rubric.http_judge(url, "m")(MESSAGES)

    assert stub.seen[0]["auth"] is None


@pytest.mark.parametrize(
    ("stub", "label"),
    [
        (Stub(status=500, body=b"{}"), "InternalServerError"),
        (Stub(status=503, body=b"{}"), "InternalServerError"),
        (Stub(status=401, body=b"{}"), "AuthenticationError"),
        (Stub(status=403, body=b"{}"), "PermissionDeniedError"),
        (Stub(status=404, body=b"{}"), "NotFoundError"),
        (Stub(status=409, body=b"{}"), "ConflictError"),
        (Stub(status=422, body=b"{}"), "UnprocessableEntityError"),
        (Stub(status=429, body=b"{}"), "RateLimitError"),
        (Stub(status=418, body=b"{}"), "APIStatusError"),
        (Stub(body=b"not json"), "JSONDecodeError"),
        (Stub(body=b"\xff\xfe"), "JSONDecodeError"),
    ],
)
def test_a_failed_exchange_raises_the_label_garak_would_name(stub: Stub, label: str) -> None:
    with judge_server(stub) as url, pytest.raises(rubric.JudgeCallError) as raised:
        rubric.http_judge(url, "m")(MESSAGES)

    assert raised.value.label == label


def test_a_slow_judge_times_out() -> None:
    with (
        judge_server(Stub(body=completion("x"), delay=1.0)) as url,
        pytest.raises(rubric.JudgeCallError) as raised,
    ):
        rubric.http_judge(url, "m", timeout=0.2)(MESSAGES)

    assert raised.value.label == "APITimeoutError"


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"choices": []}',
        b'{"choices": [{"message": {}}]}',
        b'{"choices": [{"message": {"content": null}}]}',
        b'{"choices": "x"}',
        b"[]",
    ],
)
def test_a_completion_without_content_gives_none(body: bytes) -> None:
    with judge_server(Stub(body=body)) as url:
        assert rubric.http_judge(url, "m")(MESSAGES) is None


def test_a_bad_request_answer_gives_no_content_as_garak_does() -> None:
    with judge_server(Stub(status=400, body=b'{"error": "bad"}')) as url:
        assert rubric.http_judge(url, "m")(MESSAGES) is None
