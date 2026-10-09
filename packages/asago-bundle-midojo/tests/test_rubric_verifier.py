"""The rubric verifier: spec checks, grading with an injected judge, and the HTTP call.

The judge here is an injected function or a stub server on a loopback port that answers
canned replies; no test reaches a model.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_midojo.bundle_files import asago_rubric as rubric

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

    agent_output: str = "The fee is 5."
    function_calls: list[Any] = field(default_factory=lambda: list(CALLS))


def spec(tmp_path: Path, **changes: Any) -> dict[str, Any]:
    return {
        "rubric": RUBRIC,
        "user_text": "What are the fees?",
        "judge_url": "http://127.0.0.1:1/v1",
        "judge_model": "judge-model",
        "verdict_file": str(tmp_path / "verdict.json"),
        **changes,
    }


def injected(content: Any) -> Callable[[str, str], Callable[..., Any]]:
    def factory(url: str, model: str) -> Callable[..., Any]:
        factory.seen = (url, model)  # type: ignore[attr-defined]

        def call(messages: list[dict[str, str]]) -> Any:
            if isinstance(content, rubric.JudgeCallError):
                raise content
            return content

        return call

    return factory


@pytest.mark.parametrize(
    "change",
    [
        {"rubric": "text"},
        {"rubric": {"criteria": "c"}},
        {"rubric": {"question": 3}},
        {"user_text": ""},
        {"judge_url": 3},
        {"judge_model": ""},
        {"verdict_file": None},
    ],
)
def test_a_malformed_spec_is_refused(tmp_path: Path, change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=rubric.RUBRIC_NAME):
        rubric.parse_spec(spec(tmp_path, **change))


@pytest.mark.parametrize(
    "drop", ["rubric", "user_text", "judge_url", "judge_model", "verdict_file"]
)
def test_a_spec_missing_a_key_or_holding_another_is_refused(tmp_path: Path, drop: str) -> None:
    short = {key: value for key, value in spec(tmp_path).items() if key != drop}

    with pytest.raises(ValueError, match=rubric.RUBRIC_NAME):
        rubric.parse_spec(short)
    with pytest.raises(ValueError, match=rubric.RUBRIC_NAME):
        rubric.parse_spec({**spec(tmp_path), "extra": 1})
    with pytest.raises(ValueError, match=rubric.RUBRIC_NAME):
        rubric.parse_spec([])


def test_a_good_spec_parses(tmp_path: Path) -> None:
    check = rubric.parse_spec(spec(tmp_path))

    assert check.rubric == RUBRIC
    assert check.verdict_file == tmp_path / "verdict.json"
    assert (check.judge_url, check.judge_model) == ("http://127.0.0.1:1/v1", "judge-model")


@pytest.mark.parametrize(
    ("content", "outcome", "verdict", "reason"),
    [
        (SUPPORTED, "detected", "supported", "judge_completed"),
        (
            json.dumps({"verdict": "contradicted", "evidence_refs": ["tool_calls[0].result.fee"]}),
            "not_detected",
            "contradicted",
            "judge_completed",
        ),
        (
            json.dumps({"verdict": "unresolved", "evidence_refs": []}),
            "inconclusive",
            "unresolved",
            "judge_completed",
        ),
        (
            json.dumps({"verdict": "supported", "evidence_refs": ["user_text"]}),
            "inconclusive",
            "unresolved",
            "judge_support_unresolved",
        ),
        ("not json", "inconclusive", "unresolved", "judge_parse_failed"),
        (
            rubric.JudgeCallError("APIConnectionError"),
            "inconclusive",
            "unresolved",
            "judge_failed:APIConnectionError",
        ),
    ],
)
def test_grade_writes_the_three_way_verdict(
    tmp_path: Path, content: Any, outcome: str, verdict: str, reason: str
) -> None:
    check = rubric.parse_spec(spec(tmp_path))
    factory = injected(content)

    record = rubric.RubricJudgeVerifier(factory).grade(check, Context())

    assert record["verdict"] == verdict
    written = json.loads(check.verdict_file.read_text(encoding="utf-8"))
    assert written == {
        "outcome": outcome,
        "reason": reason,
        "claim_level": "reply",
        "verdict": verdict,
        "evidence_refs": record["evidence_refs"],
        "request_count": 1,
    }
    assert factory.seen == ("http://127.0.0.1:1/v1", "judge-model")  # type: ignore[attr-defined]


def test_the_judge_sees_the_prompt_the_reply_and_the_recorded_calls(tmp_path: Path) -> None:
    check = rubric.parse_spec(spec(tmp_path))

    record = rubric.RubricJudgeVerifier(injected(SUPPORTED)).grade(check, Context())

    evidence = record["request"]["evidence"]
    assert evidence["user_text"] == "What are the fees?"
    assert evidence["history"] == []
    assert evidence["messages"] == [{"role": "assistant", "content": "The fee is 5."}]
    assert evidence["tool_calls"] == [
        {"name": "lookup", "arguments": {"q": "fees"}, "result": {"fee": 5}}
    ]
    assert evidence["completeness"] == {"messages": "complete", "tool_calls": "complete"}


def test_an_unsettled_call_makes_the_capture_partial(tmp_path: Path) -> None:
    check = rubric.parse_spec(spec(tmp_path))
    calls = [{**CALLS[0], "error": "no_response"}]

    record = rubric.RubricJudgeVerifier(injected(SUPPORTED)).grade(
        check, Context(function_calls=calls)
    )

    assert record["request"]["evidence"]["completeness"] == {
        "messages": "partial",
        "tool_calls": "partial",
    }
    assert record["verdict"] == "supported"  # a cited item stands even when the capture is partial


def test_a_missing_reply_is_an_empty_message(tmp_path: Path) -> None:
    check = rubric.parse_spec(spec(tmp_path))

    record = rubric.RubricJudgeVerifier(injected(SUPPORTED)).grade(
        check, Context(agent_output=None)
    )  # type: ignore[arg-type]

    assert record["request"]["evidence"]["messages"] == [{"role": "assistant", "content": ""}]


def test_recorded_calls_may_be_objects_with_attributes() -> None:
    @dataclass
    class Call:
        function: str = "lookup"
        args: dict[str, Any] = field(default_factory=lambda: {"q": 1})
        result: str = "[1, 2]"
        error: str | None = None

    evidence = rubric.build_evidence("Q", [], "A", [Call()])

    assert evidence["tool_calls"] == [{"name": "lookup", "arguments": {"q": 1}, "result": [1, 2]}]


def test_a_judge_that_cannot_be_reached_never_raises(tmp_path: Path) -> None:
    check = rubric.parse_spec(spec(tmp_path, judge_url="http://127.0.0.1:9/v1"))

    record = rubric.RubricJudgeVerifier().grade(check, Context())

    assert record["reason"] == "judge_failed:APIConnectionError"
    assert json.loads(check.verdict_file.read_text(encoding="utf-8"))["outcome"] == "inconclusive"


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
