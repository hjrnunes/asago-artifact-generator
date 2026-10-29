"""What the judge service keeps out of its answers and logs, and how long it waits.

A model may echo its own key, or text holding the service's token, and a request may carry
deeply nested tool arguments: the answer holds neither secret in any field, and its
``details`` stay within MiDojo's nesting limit for a ``midojo.judge.v1`` response (64
objects and arrays deep, ``midojo.verifiers.judge.MAX_DETAILS_DEPTH``). The access log
names the method and the status only. A model whose answer trickles in is cut off at the
request's budget.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlsplit

import pytest
from judge_cases import (
    SUPPORTED,
    TOKEN,
    Model,
    completion,
    judge_service,
    loopback,
    midojo_request,
    model_server,
    post,
    rubric,
)

from asago_bundle_midojo import judge_service as service

PLAIN_KEY = "sk-model-key-0123456789"
# A key whose JSON spelling differs from its text: a quote, a backslash, a slash, non-ASCII.
ESCAPED_KEY = 'sk-"model"\\key/é-0123'
KEYS = [pytest.param(PLAIN_KEY, id="plain-key"), pytest.param(ESCAPED_KEY, id="escaped-key")]
# A key a reference path can name (letters, digits and underscores).
NAMEABLE_KEY = "sk_model_key_0123456789"
MAX_DETAILS_DEPTH = 64
TURN = [{"message": "What are the fees?", "reply": "The fee is 5."}]


def request(
    reply: str = "The fee is 5.", calls: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return midojo_request(
        "Does the reply give fees?",
        rubric(["What are the fees?"]),
        [{"message": "What are the fees?", "reply": reply}],
        calls,
    )


def depth(value: Any) -> int:
    """MiDojo's ``_depth``: how many objects and arrays deep ``value`` nests, level by level."""

    count, level = 0, [value]
    while containers := [item for item in level if type(item) in (dict, list)]:
        count += 1
        level = [
            child
            for item in containers
            for child in (item.values() if type(item) is dict else item)
        ]
    return count


def texts(value: Any) -> list[str]:
    """Every string in ``value``, keys included."""

    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (*texts(key), *texts(item))]
    if isinstance(value, list):
        return [text for item in value for text in texts(item)]
    return []


def echoing_key(headers: Any, body: dict[str, Any]) -> bytes:
    """A model that answers with its own key, as text and as a cited reference."""

    key = headers["Authorization"].removeprefix("Bearer ")
    return completion(
        json.dumps({"verdict": "supported", "evidence_refs": [f"messages[0].{key}"]})
        + f" my key is {key}"
    )


def echoing_request(headers: Any, body: dict[str, Any]) -> bytes:
    """A model that answers with the judge request it got, word for word."""

    return completion(body["messages"][-1]["content"])


def leaked(answer: Any, secret: str) -> bool:
    spellings = {secret, json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]}
    encoded = json.dumps(answer) + json.dumps(answer, ensure_ascii=False)
    return any(secret in text for text in texts(answer)) or any(s in encoded for s in spellings)


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("respond", [echoing_key, echoing_request], ids=["key", "request"])
def test_a_model_that_echoes_its_key_does_not_put_it_in_the_answer(
    monkeypatch: pytest.MonkeyPatch, key: str, respond: Any
) -> None:
    monkeypatch.setenv("OPENAICOMPATIBLE_API_KEY", key)
    model = Model(respond=respond)
    with model_server(model) as url, judge_service(url, model_key=key) as judge:
        status, answer = post(judge, request(reply=f"The fee is 5. {key}"))

    assert status == 200
    assert model.seen[0]["auth"] == f"Bearer {key}"
    assert answer["verdict"] == "unresolved"
    assert not leaked(answer, key)


def test_a_model_that_echoes_text_holding_the_token_does_not_put_it_in_the_answer() -> None:
    model = Model(respond=echoing_request)
    with model_server(model) as url, judge_service(url) as judge:
        status, answer = post(judge, request(reply=f"Your session token is {TOKEN}."))

    assert status == 200
    assert TOKEN in json.dumps(model.bodies()), "the model was asked about the token"
    assert not leaked(answer, TOKEN)


def test_a_decided_answer_cites_no_secret_in_its_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reference resolves in the evidence, so a reply holding the key can be cited: the
    reference is a path, and the answer carries it with the key in no field."""

    monkeypatch.setenv("OPENAICOMPATIBLE_API_KEY", NAMEABLE_KEY)
    calls = [{"function": "f", "args": {}, "result": json.dumps({NAMEABLE_KEY: 5}), "turn": 0}]
    cited = json.dumps(
        {"verdict": "supported", "evidence_refs": [f"tool_calls[0].result.{NAMEABLE_KEY}"]}
    )
    model = Model(body=completion(cited))
    with model_server(model) as url, judge_service(url, model_key=NAMEABLE_KEY) as judge:
        status, answer = post(judge, request(calls=calls))

    assert (status, answer["verdict"]) == (200, "supported")
    assert answer["details"]["records"][-1]["evidence_refs"] == ["[redacted]"]
    assert not leaked(answer, NAMEABLE_KEY)


def test_the_details_hold_each_judged_turn_s_summary_only() -> None:
    model = Model(body=completion(SUPPORTED))
    with model_server(model) as url, judge_service(url) as judge:
        _, answer = post(judge, request())

    (record,) = answer["details"]["records"]
    assert set(answer["details"]) == {"prompt_version", "judge_turns", "request_count", "records"}
    assert set(record) == {
        "verdict",
        "evidence_refs",
        "reason",
        "prompt_version",
        "request_sha256",
        "response_sha256",
    }
    sent = model.bodies()[0]["messages"][-1]["content"]
    assert record["request_sha256"] == service.sha256_text(sent)
    assert record["response_sha256"] == service.sha256_text(SUPPORTED)


def test_a_deeply_nested_tool_argument_keeps_the_answer_within_midojo_s_depth() -> None:
    nested: Any = "bottom"
    for _ in range(80):
        nested = {"next": nested}
    calls = [{"function": "lookup", "args": {"q": nested}, "result": '{"fee": 5}', "turn": 0}]
    model = Model(body=completion(SUPPORTED))
    with model_server(model) as url, judge_service(url) as judge:
        status, answer = post(judge, request(calls=calls))

    assert (status, answer["verdict"]) == (200, "supported")
    assert depth(answer["details"]) <= MAX_DETAILS_DEPTH
    (asked,) = [json.loads(body["messages"][1]["content"]) for body in model.bodies()]
    assert asked["evidence"]["tool_calls"][0]["arguments"] == {"q": nested}, "the model sees all"


def raw_exchange(url: str, data: bytes) -> bytes:
    """Send ``data`` as it is to the service and return what it answered."""

    address = urlsplit(url)
    with socket.create_connection((address.hostname, address.port), timeout=5) as connection:
        connection.sendall(data)
        connection.shutdown(socket.SHUT_WR)
        answer = b""
        while chunk := connection.recv(65536):
            answer += chunk
    return answer


def test_the_access_log_names_the_method_and_the_status_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = Model(body=completion(SUPPORTED))
    with model_server(model) as url, judge_service(url) as judge:
        accepted = post(f"{judge}?token={TOKEN}", request())
        refused = post(f"{judge}?token={TOKEN}", request(), token="wrong")
        answers = [
            raw_exchange(judge, f"GET /judge?token={TOKEN} HTTP/1.1\r\n\r\n".encode()),
            raw_exchange(judge, f"{TOKEN} /judge HTTP/1.1\r\n\r\n".encode()),
            raw_exchange(judge, f"POST /{TOKEN} HTTP/9.{TOKEN}\r\n\r\n".encode()),
            raw_exchange(judge, f"garbage {TOKEN}\r\n\r\n".encode()),
        ]
    logged = capsys.readouterr().err

    assert (accepted[0], refused[0]) == (200, 401)
    assert all(answers), "each request line is answered (an HTTP/0.9 one without headers)"
    assert TOKEN not in logged
    assert "POST 200" in logged.splitlines()
    assert "POST 401" in logged.splitlines()
    assert all(TOKEN.encode() not in answer for answer in answers)


def test_a_trickling_answer_is_cut_off_at_the_budget() -> None:
    model = Model(body=completion(SUPPORTED), trickle=0.02)
    with model_server(model) as url:
        call = service.model_calls(url, "m")(0.3)
        started = time.monotonic()
        with pytest.raises(service.JudgeCallError) as raised:
            call([{"role": "user", "content": "u"}])
        elapsed = time.monotonic() - started

    assert raised.value.label == "APITimeoutError"
    assert elapsed < 0.5, f"waited {elapsed:.3f} s on a 0.3 s budget (the body takes 2 s)"


# --- redaction reads values only ------------------------------------------------------

# The words the answer and its details are made of: field names, verdicts, the schema.
RESERVED = [
    "schema",
    "id",
    "verdict",
    "rationale",
    "model",
    "details",
    "supported",
    "contradicted",
    "unresolved",
    "midojo.judge.v1",
    "records",
    "evidence_refs",
    "reason",
    "prompt_version",
    "judge_turns",
    "request_count",
    "request_sha256",
    "response_sha256",
]
RECORD_FIELDS = {
    "verdict",
    "evidence_refs",
    "reason",
    "prompt_version",
    "request_sha256",
    "response_sha256",
}


@pytest.mark.parametrize("role", ["token", "model_key"])
@pytest.mark.parametrize("credential", RESERVED)
def test_a_credential_that_is_a_reserved_word_leaves_the_answer_s_shape_alone(
    monkeypatch: pytest.MonkeyPatch, credential: str, role: str
) -> None:
    token = credential if role == "token" else TOKEN
    key = credential if role == "model_key" else PLAIN_KEY
    monkeypatch.setenv("OPENAICOMPATIBLE_API_KEY", key)
    model = Model(body=completion(SUPPORTED))
    with model_server(model) as url, judge_service(url, token=token, model_key=key) as judge:
        status, answer = post(judge, request(), token=token)

    assert status == 200
    assert set(answer) == {"schema", "id", "verdict", "rationale", "model", "details"}
    assert (answer["schema"], answer["verdict"]) == ("midojo.judge.v1", "supported")
    assert answer["id"] == request()["id"]
    assert answer["rationale"] == "judge_completed"
    assert set(answer["details"]) == {"prompt_version", "judge_turns", "request_count", "records"}
    (record,) = answer["details"]["records"]
    assert set(record) == RECORD_FIELDS
    assert (record["verdict"], record["reason"]) == ("supported", "judge_completed")
    assert record["evidence_refs"] == ["messages[0].content"]


def test_a_model_name_holding_a_credential_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAICOMPATIBLE_API_KEY", PLAIN_KEY)
    answer = service.judge(
        request(), service.model_calls("http://127.0.0.1:9/v1", "m"), f"m-{PLAIN_KEY}", [PLAIN_KEY]
    )

    assert answer["model"] == "m-[redacted]"


# --- an evidence reference that encodes a credential ------------------------------------

SLASHED_KEY = "sk/private~key"
POINTER = "/tool_calls/0/result/sk~1private~0key"


@pytest.mark.parametrize("verdict", ["supported", "contradicted"])
@pytest.mark.parametrize(
    "reference",
    [POINTER, f"$.tool_calls[0].result.{NAMEABLE_KEY}", f"tool_calls[0].result.{NAMEABLE_KEY}"],
    ids=["pointer", "dollar-dotted", "dotted"],
)
def test_a_reference_whose_decoded_path_holds_a_credential_is_redacted(
    monkeypatch: pytest.MonkeyPatch, verdict: str, reference: str
) -> None:
    key = SLASHED_KEY if reference == POINTER else NAMEABLE_KEY
    monkeypatch.setenv("OPENAICOMPATIBLE_API_KEY", key)
    calls = [{"function": "f", "args": {}, "result": json.dumps({key: 5}), "turn": 0}]
    cited = json.dumps({"verdict": verdict, "evidence_refs": [reference, "messages[0].content"]})
    model = Model(body=completion(cited))
    with model_server(model) as url, judge_service(url, model_key=key) as judge:
        status, answer = post(judge, request(calls=calls))

    assert (status, answer["verdict"]) == (200, verdict), "the reference resolves"
    refs = answer["details"]["records"][-1]["evidence_refs"]
    assert refs == ["[redacted]", "messages[0].content"]
    assert not leaked(answer, key)


@pytest.mark.parametrize(
    ("reference", "secret", "holds"),
    [
        (POINTER, SLASHED_KEY, True),
        ("/tool_calls/0/result/sk~1private", "sk/private", True),
        ("/a/b~0c", "b~c", True),
        ("/x/a/b", "a/b", True),
        ("tool_calls[0].result.fee", SLASHED_KEY, False),
        ("messages[0].content", "messages[0]", True),
    ],
)
def test_a_reference_holds_a_secret_in_its_text_or_its_decoded_path(
    reference: str, secret: str, holds: bool
) -> None:
    assert service.reference_holds(reference, [secret]) is holds


# --- the deadline covers every wait ----------------------------------------------------


def raw_model(script: list[tuple[bytes, float]]) -> Any:
    """A model that writes ``script``'s bytes as they are, pausing after each."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            for data, pause in script:
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                except OSError:
                    return
                threading.Event().wait(pause)
            self.close_connection = True

        def log_message(self, *args: Any) -> None:
            return

    return loopback(Handler)


def trickled(data: bytes, pause: float) -> list[tuple[bytes, float]]:
    return [(data[index : index + 1], pause) for index in range(len(data))]


CONTENT = completion(SUPPORTED)
CHUNKED_HEAD = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTrailer: X-Pad\r\n\r\n"
CHUNKED_BODY = f"{len(CONTENT):x}\r\n".encode() + CONTENT + b"\r\n0\r\n"
SLOW = {
    "headers": trickled(
        f"HTTP/1.1 200 OK\r\nContent-Length: {len(CONTENT)}\r\n\r\n".encode(), 0.02
    )
    + [(CONTENT, 0)],
    "chunked-trailers": [(CHUNKED_HEAD + CHUNKED_BODY, 0)]
    + trickled(b"X-Pad: " + b"p" * 40 + b"\r\n\r\n", 0.02),
    "chunk-framing": [(CHUNKED_HEAD, 0)]
    + trickled(f"{len(CONTENT):x}\r\n".encode() + CONTENT + b"\r\n0\r\n\r\n", 0.01),
}


@pytest.mark.parametrize("name", sorted(SLOW))
def test_a_model_that_answers_slowly_anywhere_is_cut_off_at_the_budget(name: str) -> None:
    with raw_model(SLOW[name]) as port:
        call = service.model_calls(f"http://127.0.0.1:{port}/v1", "m")(0.15)
        started = time.monotonic()
        with pytest.raises(service.JudgeCallError) as raised:
            call([{"role": "user", "content": "u"}])
        elapsed = time.monotonic() - started

    assert raised.value.label == "APITimeoutError"
    assert elapsed < 0.4, f"waited {elapsed:.3f} s on a 0.15 s budget"


@pytest.mark.parametrize("name", sorted(SLOW))
def test_the_same_answer_in_time_is_read_whole(name: str) -> None:
    script = [(b"".join(data for data, _ in SLOW[name]), 0)]
    with raw_model(script) as port:
        content = service.model_calls(f"http://127.0.0.1:{port}/v1", "m")(5)(
            [{"role": "user", "content": "u"}]
        )

    assert content == SUPPORTED


def test_a_bad_request_answer_cut_off_at_the_budget_is_a_timeout() -> None:
    # The 400 status line arrives in time but its headers do not: the socket is shut down at
    # the deadline, the client takes that for the end of the headers and reports a 400.
    script = [(b"HTTP/1.1 400 Bad Request\r\n", 0)] + [(b"X-Pad: p\r\n", 0.05)] * 20
    with raw_model(script) as port:
        call = service.model_calls(f"http://127.0.0.1:{port}/v1", "m")(0.15)
        with pytest.raises(service.JudgeCallError) as raised:
            call([{"role": "user", "content": "u"}])

    assert raised.value.label == "APITimeoutError"


def test_a_body_cut_short_is_a_connection_error() -> None:
    head = f"HTTP/1.1 200 OK\r\nContent-Length: {len(CONTENT) + 10}\r\n\r\n".encode()
    with raw_model([(head + CONTENT, 0)]) as port:
        call = service.model_calls(f"http://127.0.0.1:{port}/v1", "m")(5)
        with pytest.raises(service.JudgeCallError) as raised:
            call([{"role": "user", "content": "u"}])

    assert raised.value.label == "APIConnectionError"
