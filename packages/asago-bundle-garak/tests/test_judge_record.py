"""The judge writes one exchange per model request, including failed calls."""

from __future__ import annotations

import errno
import io
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from judge_cases import (
    MODEL,
    SUPPORTED,
    Model,
    completion,
    judge_service,
    midojo_request,
    model_server,
    post,
    rubric,
)
from midojo_spec.judge_exchange import (
    ExchangeFormatError,
    ExchangeLog,
    digests_hold,
    read_exchanges,
)
from midojo_spec.rubric_judge import PROMPT_VERSION

from asago_bundle_garak import judge_service as service

TURN_MESSAGES = ["First question?", "Second question?", "Last question?"]


def request(mode: str = "final", suffix: str = "") -> dict[str, Any]:
    turns = [
        {"message": message, "reply": f"Answer {i}. {suffix}"}
        for i, message in enumerate(TURN_MESSAGES)
    ]
    return midojo_request("Does the reply answer?", rubric(TURN_MESSAGES, mode), turns)


@pytest.mark.parametrize(("mode", "count"), [("final", 1), ("all", 3)])
def test_every_turn_has_one_row_and_the_sealed_grade_digests(
    tmp_path: Path, mode: str, count: int
) -> None:
    path = tmp_path / "nested" / "exchanges.jsonl"
    path.parent.mkdir()
    incoming = request(mode)
    model = Model(body=completion(SUPPORTED))
    with model_server(model) as url, judge_service(url, record_path=path) as judge:
        status, answer = post(judge, incoming)

    rows = read_exchanges(path)
    assert (status, answer["verdict"]) == (200, "supported")
    assert len(model.seen) == len(rows) == answer["details"]["request_count"] == count
    assert [row.ask for row in rows] == list(range(count))
    assert [row.sequence for row in rows] == list(range(count))
    for row, details, sent in zip(rows, answer["details"]["records"], model.bodies(), strict=True):
        assert row.request_id == incoming["id"]
        assert row.model == MODEL
        assert row.prompt_version == details["prompt_version"] == PROMPT_VERSION
        assert row.messages == tuple((m["role"], m["content"]) for m in sent["messages"])
        assert row.reply == SUPPORTED
        assert row.error is None and row.http_status == 200
        assert row.request_sha256 == details["request_sha256"]
        assert row.response_sha256 == details["response_sha256"]
        assert digests_hold(row) is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_appending_to_existing_public_log_does_not_leak_new_exchange(tmp_path: Path) -> None:
    path = tmp_path / "existing.jsonl"
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)
    incoming = midojo_request(
        "Does the reply answer?",
        rubric(["Question?"]),
        [{"message": "Question?", "reply": "Answer."}],
    )
    model = Model(body=completion(SUPPORTED))
    with model_server(model) as url, judge_service(url, record_path=path) as judge:
        status, answer = post(judge, incoming)
    assert status == 200 and answer["verdict"] == "supported"
    assert len(model.seen) == len(read_exchanges(path)) == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("model", "error", "status"),
    [
        (Model(status=503, body=b"{}"), "InternalServerError", 503),
        (
            Model(status=302, headers={"Location": "http://127.0.0.1:9/"}, body=b""),
            "APIStatusError",
            302,
        ),
    ],
)
def test_failed_http_calls_leave_a_row(
    tmp_path: Path, model: Model, error: str, status: int
) -> None:
    path = tmp_path / "failed.jsonl"
    with model_server(model) as url, judge_service(url, record_path=path) as judge:
        code, answer = post(judge, request())
    (row,) = read_exchanges(path)
    assert (code, answer["verdict"], answer["rationale"]) == (
        200,
        "unresolved",
        f"judge_failed:{error}",
    )
    assert row.error == error and row.http_status == status
    assert row.reply is row.response_sha256 is None
    assert row.request_sha256 == answer["details"]["records"][0]["request_sha256"]


def test_timeout_class_failure_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "timeout.jsonl"
    monkeypatch.setattr(service, "TIMEOUT_SECONDS", 0.1)
    with (
        model_server(Model(body=completion(SUPPORTED), delay=0.4)) as url,
        judge_service(url, record_path=path) as judge,
    ):
        status, answer = post(judge, request())
    (row,) = read_exchanges(path)
    assert (status, answer["rationale"]) == (200, "judge_failed:APITimeoutError")
    assert row.error == "APITimeoutError" and row.reply is None


def test_an_interrupted_model_call_notifies_its_observer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from midojo_spec import rubric_judge

    def interrupted(*_args: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(rubric_judge, "_exchange", interrupted)
    path = tmp_path / "interrupted.jsonl"
    log = ExchangeLog(path)
    incoming = request()
    ask = 0

    def observe(messages: list[dict[str, str]], observed: Any) -> None:
        nonlocal ask
        log.append(
            service.Exchange.observed(
                observed,
                request_id=incoming["id"],
                ask=ask,
                model=MODEL,
                prompt_version=PROMPT_VERSION,
                messages=messages,
            )
        )
        ask += 1

    with pytest.raises(KeyboardInterrupt):
        rubric_judge.http_judge("http://127.0.0.1:9/v1", MODEL, observer=observe)(
            [{"role": "user", "content": "a question"}]
        )
    (row,) = read_exchanges(path)
    assert row.error == "KeyboardInterrupt" and row.reply is None and ask == 1


def test_redaction_keeps_original_digests_without_leaking_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secret.jsonl"
    key = "placeholder-model-key"
    token = "placeholder-service-token"
    monkeypatch.setenv("OPENAICOMPATIBLE_API_KEY", key)
    incoming = request(suffix=f"{key} {token}")

    def echo(headers: Any, body: dict[str, Any]) -> bytes:
        return completion(
            json.dumps({"verdict": "supported", "evidence_refs": []})
            + f" {headers['Authorization']} {token}"
        )

    with (
        model_server(Model(respond=echo)) as url,
        judge_service(url, token=token, model_key=key, record_path=path) as judge,
    ):
        status, answer = post(judge, incoming, token=token)

    (row,) = read_exchanges(path)
    content = path.read_text(encoding="utf-8")
    assert status == 200 and row.redacted and "[redacted]" in content
    assert key not in content and token not in content
    assert row.request_sha256 == answer["details"]["records"][0]["request_sha256"]
    assert row.response_sha256 == answer["details"]["records"][0]["response_sha256"]
    assert digests_hold(row) is None


def test_concurrent_requests_have_distinct_rising_sequences_and_local_ask_indices(
    tmp_path: Path,
) -> None:
    path = tmp_path / "parallel.jsonl"
    model = Model(body=completion(SUPPORTED))
    incoming = [request("all", suffix=str(i)) for i in range(12)]
    with model_server(model) as url, judge_service(url, record_path=path) as judge:
        with ThreadPoolExecutor(max_workers=4) as pool:
            answers = list(pool.map(lambda body: post(judge, body), incoming))

    rows = read_exchanges(path)
    assert all(code == 200 for code, _ in answers)
    assert len(rows) == 36 and len(model.seen) == 36
    assert [row.sequence for row in rows] == list(range(36))
    for body, (_, answer) in zip(incoming, answers, strict=True):
        own = sorted(
            (row for row in rows if row.request_id == body["id"]), key=lambda row: row.ask
        )
        assert [row.ask for row in own] == [0, 1, 2]
        assert [(row.request_sha256, row.response_sha256) for row in own] == [
            (record["request_sha256"], record["response_sha256"])
            for record in answer["details"]["records"]
        ]


@pytest.mark.parametrize("error", [ExchangeFormatError("invalid"), OSError("unwritable")])
def test_a_record_failure_returns_500_without_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(_self: ExchangeLog, _row: Any) -> None:
        raise error

    monkeypatch.setattr(ExchangeLog, "append", fail)
    path = tmp_path / "unwritten.jsonl"
    model = Model(body=completion(SUPPORTED))
    with model_server(model) as url, judge_service(url, record_path=path) as judge:
        status, answer = post(judge, request())
    assert status == 500
    assert answer == {"error": f"the judge failed ({type(error).__name__})"}
    assert len(model.seen) == 1
    assert path.read_bytes() == b""
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("missing_parent", "reason"),
    [(False, "Is a directory"), (True, "No such file or directory")],
)
def test_unusable_record_path_refuses_server_before_binding(
    tmp_path: Path, missing_parent: bool, reason: str
) -> None:
    path = tmp_path / "missing" / "log.jsonl" if missing_parent else tmp_path
    with pytest.raises(service.ServiceConfigError, match=reason) as error:
        service.make_server(
            0,
            service.model_calls("http://127.0.0.1:9/v1", MODEL),
            MODEL,
            "token",
            record_path=path,
        )
    assert str(path) not in str(error.value)


def test_fifo_record_path_is_not_a_regular_file(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("this platform does not support FIFOs")
    path = tmp_path / "pipe"
    os.mkfifo(path)
    with pytest.raises(service.ServiceConfigError, match="the record path is not a regular file"):
        service.make_server(
            0,
            service.model_calls("http://127.0.0.1:9/v1", MODEL),
            MODEL,
            "placeholder-service-token",
            record_path=path,
        )


def test_serve_with_unusable_record_path_prints_no_ready_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JUDGE_MODEL_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("JUDGE_MODEL", MODEL)
    monkeypatch.setenv("JUDGE_SERVICE_TOKEN", "placeholder-service-token")
    out = io.StringIO()
    path = tmp_path / "missing" / "log.jsonl"
    with pytest.raises(service.ServiceConfigError, match="the record file cannot be created"):
        service.serve(0, out=out, record_path=path)
    assert out.getvalue() == ""


def test_chmod_failure_refuses_server_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "public.jsonl"
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)

    def fail(_descriptor: int, _mode: int) -> None:
        raise PermissionError(errno.EPERM, "placeholder-model-key")

    monkeypatch.setattr(service.os, "fchmod", fail)
    with pytest.raises(service.ServiceConfigError, match="Operation not permitted"):
        service.make_server(
            0,
            service.model_calls("http://127.0.0.1:9/v1", MODEL),
            MODEL,
            "token",
            record_path=path,
        )
    assert path.read_bytes() == b""


@pytest.mark.parametrize("bad_id", ["not-a-digest", "sha256:" + "A" * 64, "sha256:" + "0" * 63])
def test_bad_id_is_refused_before_asking_a_model(tmp_path: Path, bad_id: str) -> None:
    path = tmp_path / "unused.jsonl"
    model = Model(body=completion(SUPPORTED))
    incoming = request() | {"id": bad_id}
    with model_server(model) as url, judge_service(url, record_path=path) as judge:
        assert post(judge, incoming) == (400, {"error": "id is not a sha256 digest"})
    assert model.seen == [] and path.read_bytes() == b""


def test_without_record_an_old_style_id_is_accepted_and_no_file_is_written(tmp_path: Path) -> None:
    path = tmp_path / "absent.jsonl"
    model = Model(body=completion(SUPPORTED))
    with model_server(model) as url, judge_service(url) as judge:
        status, answer = post(judge, request() | {"id": "legacy-id"})
    assert status == 200 and answer["id"] == "legacy-id"
    assert len(model.seen) == 1 and not path.exists()
