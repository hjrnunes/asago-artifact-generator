"""The judge service's model call, and the service as its command starts it.

The model call is the rubric judge's ``http_judge``: Garak's body to
``<url>/chat/completions``, the key from ``OPENAICOMPATIBLE_API_KEY``, sent once, with no
proxy from the environment and no redirect followed. The model is a stub on a loopback
port that answers canned replies; no test reaches a model.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest
from judge_cases import (
    SUPPORTED,
    TOKEN,
    Model,
    completion,
    loopback,
    midojo_request,
    model_server,
    post,
    rubric,
)

from asago_artifact_generator.authoring import rubric_judge as rubric_judge

MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
KEY = "sk-judge-key-that-stays-here"
# The installed command, as an operator starts it.
COMMAND = [str(Path(sys.executable).parent / "asago-bundle-midojo"), "judge-serve", "--port", "0"]


def test_the_call_posts_garaks_body_to_chat_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAICOMPATIBLE_API_KEY", "placeholder")
    model = Model(body=completion(SUPPORTED))

    with model_server(model) as url:
        content = rubric_judge.http_judge(url + "/", "judge-model")(MESSAGES)

    assert content == SUPPORTED
    (seen,) = model.seen
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer placeholder"
    assert seen["body"] == rubric_judge.request_body("judge-model", MESSAGES)


def test_the_call_sends_no_authorization_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAICOMPATIBLE_API_KEY", raising=False)
    model = Model(body=completion("x"))

    with model_server(model) as url:
        rubric_judge.http_judge(url, "m")(MESSAGES)

    assert model.seen[0]["auth"] is None


@pytest.mark.parametrize(
    ("model", "label"),
    [
        (Model(status=500, body=b"{}"), "InternalServerError"),
        (Model(status=503, body=b"{}"), "InternalServerError"),
        (Model(status=401, body=b"{}"), "AuthenticationError"),
        (Model(status=403, body=b"{}"), "PermissionDeniedError"),
        (Model(status=404, body=b"{}"), "NotFoundError"),
        (Model(status=409, body=b"{}"), "ConflictError"),
        (Model(status=422, body=b"{}"), "UnprocessableEntityError"),
        (Model(status=429, body=b"{}"), "RateLimitError"),
        (Model(status=418, body=b"{}"), "APIStatusError"),
        (Model(body=b"not json"), "JSONDecodeError"),
        (Model(body=b"\xff\xfe"), "JSONDecodeError"),
    ],
)
def test_a_failed_exchange_raises_the_label_garak_would_name(model: Model, label: str) -> None:
    with model_server(model) as url, pytest.raises(rubric_judge.JudgeCallError) as raised:
        rubric_judge.http_judge(url, "m")(MESSAGES)

    assert raised.value.label == label
    assert len(model.seen) == 1, "a failed call is not retried"


def test_a_slow_judge_times_out() -> None:
    with (
        model_server(Model(body=completion("x"), delay=1.0)) as url,
        pytest.raises(rubric_judge.JudgeCallError) as raised,
    ):
        rubric_judge.http_judge(url, "m", timeout=0.2)(MESSAGES)

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
    with model_server(Model(body=body)) as url:
        assert rubric_judge.http_judge(url, "m")(MESSAGES) is None


def test_a_bad_request_answer_gives_no_content_as_garak_does() -> None:
    with model_server(Model(status=400, body=b'{"error": "bad"}')) as url:
        assert rubric_judge.http_judge(url, "m")(MESSAGES) is None


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_not_followed(status: int) -> None:
    elsewhere = Model(body=completion(SUPPORTED))
    with model_server(elsewhere) as other:
        model = Model(status=status, headers={"Location": other + "/chat/completions"})
        with model_server(model) as url, pytest.raises(rubric_judge.JudgeCallError) as raised:
            rubric_judge.http_judge(url, "m")(MESSAGES)

    assert raised.value.label == "APIStatusError"
    assert len(model.seen) == 1
    assert elsewhere.seen == []


def test_no_proxy_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    proxied: list[str] = []

    class Proxy(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            proxied.append(self.path)
            self.send_response(502)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            return

    model = Model(body=completion(SUPPORTED))
    with loopback(Proxy) as port, model_server(model) as url:
        for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{port}")
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        content = rubric_judge.http_judge(url, "m")(MESSAGES)

    assert content == SUPPORTED
    assert proxied == []
    assert len(model.seen) == 1


# --- the service as ``judge-serve`` starts it --------------------------------------------


def test_judge_serve_answers_on_loopback_and_keeps_the_key_to_the_model() -> None:
    """The command reads its model and token from its environment, says where it listens,
    answers MiDojo's request, and stops on SIGTERM; the key reaches the model alone."""

    model = Model(body=completion(SUPPORTED))
    with model_server(model) as url:
        environ = {
            **os.environ,
            "JUDGE_MODEL_URL": url,
            "JUDGE_MODEL": "judge-model",
            "OPENAICOMPATIBLE_API_KEY": KEY,
            "JUDGE_SERVICE_TOKEN": TOKEN,
        }
        process = subprocess.Popen(
            COMMAND,
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout is not None
            ready = process.stdout.readline()
            assert ready.startswith("judge service on http://127.0.0.1:"), ready
            service_url = ready.split(" on ", 1)[1].strip() + "judge"
            request = midojo_request(
                "Does the reply give fees?",
                rubric(["What are the fees?"]),
                [{"message": "What are the fees?", "reply": "The fee is 5."}],
            )
            answers = [
                post(service_url, request),
                post(service_url, request, token="wrong"),
                post(service_url, b"not json"),
            ]
        finally:
            process.send_signal(signal.SIGTERM)
            out, err = process.communicate(timeout=10)

    assert process.returncode == 0
    assert [status for status, _ in answers] == [200, 401, 400]
    assert answers[0][1]["verdict"] == "supported"
    assert [seen["auth"] for seen in model.seen] == [f"Bearer {KEY}"]
    assert KEY not in json.dumps(answers) + out + err
    assert TOKEN not in out + err


def test_judge_serve_without_its_environment_exits_before_listening() -> None:
    environ = {
        name: value
        for name, value in os.environ.items()
        if name not in ("JUDGE_MODEL_URL", "JUDGE_MODEL", "JUDGE_SERVICE_TOKEN")
    }

    result = subprocess.run(
        COMMAND,
        env=environ,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "JUDGE_SERVICE_TOKEN" in result.stderr
    assert result.stdout == ""
