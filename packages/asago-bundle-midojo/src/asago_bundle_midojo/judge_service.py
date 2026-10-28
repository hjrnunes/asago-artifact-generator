"""The judge service of a reply episode: ``midojo.judge.v1`` in front of the rubric judge.

A reply episode's security check is MiDojo's built-in ``reply_judge`` predicate
(``episode_compile.py``). When MiDojo grades the sealed run it posts one
``midojo.judge.v1`` request to the URL in ``JUDGE_URL`` with the bearer token in
``JUDGE_TOKEN``: the check's ``claim`` (the rubric's question) and ``rubric``, the last
turn's ``message`` and the agent's ``reply``, the earlier turns (``prior_turns``) and the
recorded calls (``tool_calls``, each naming the turn it was made in, and
``tool_calls_complete``). This service answers it with the Garak-ported rubric judge
(``asago_artifact_generator.authoring.rubric_judge``): it sends the model the requests
Garak's ``RubricJudge`` sends for the same conversation, and answers ``supported``,
``contradicted`` or ``unresolved``, which MiDojo grades True, False and unknown.

The rubric holds what compile wrote: ``criteria``, ``facts`` and ``runtime_facts`` (the
judge's), ``turns`` (the user messages the episode sends) and ``judge_turns``. The judge
sees what Garak's judge sees for a turn: ``user_text`` is that turn's message,
``history`` the earlier messages and replies interleaved, the graded output that turn's
reply, and ``tool_calls`` every call made up to and including that turn. With
``judge_turns`` ``final`` the judge grades the last turn; with ``all`` it grades every
turn in order, as Garak does, and the last turn's verdict is the answer. A conversation
that does not hold exactly the episode's messages, each with a reply, or a call without a
turn the judge can place, is ``unresolved`` without a model request, with the reason as
the ``rationale``. Calls MiDojo holds as incomplete make the evidence ``partial``.

A model that fails or cannot be reached gives ``unresolved`` (``judge_failed:<label>``),
never ``contradicted``. The response's ``rationale`` is the last judged turn's reason,
``model`` the model asked, and ``details`` the rubric judge's record of each judged turn.

The service reads its model from its own environment only: ``JUDGE_MODEL_URL`` (the
OpenAI-compatible base URL; the judge posts to ``<url>/chat/completions``), ``JUDGE_MODEL``
and the key in ``OPENAICOMPATIBLE_API_KEY``, which goes only to that URL (no redirect is
followed and no proxy is read from the environment) and never into a response or a log
line. It requires the bearer token in ``JUDGE_SERVICE_TOKEN`` on every request, and
listens on 127.0.0.1 only.

Each judged turn is one model request, sent once, as the rubric judge's ``http_judge``
sends it. A model request may take up to 180 seconds (``TIMEOUT_SECONDS``, the old
judge's), and one ``midojo.judge.v1`` request's model requests share 180 seconds per
judged turn (``budget_seconds``): the check's ``timeout_seconds`` is that and a margin
(``episode_compile.judge_timeout_seconds``). The service keeps no state between
requests: MiDojo may ask twice in one grading, the second time without the blocked and
unfinished calls, and each request is judged on its own.
"""

from __future__ import annotations

import hmac
import json
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TextIO

from asago_artifact_generator.authoring.rubric_judge import (
    PROMPT_VERSION,
    TIMEOUT_SECONDS,
    JudgeCallError,
    _record,
    build_evidence,
    http_judge,
    is_complete,
    judge_output,
    request_count,
)

SCHEMA = "midojo.judge.v1"
VERDICTS = ("supported", "contradicted", "unresolved")
REQUEST_FIELDS = frozenset(
    {
        "schema",
        "id",
        "claim",
        "rubric",
        "message",
        "reply",
        "prior_turns",
        "tool_calls",
        "tool_calls_complete",
    }
)
REQUIRED_FIELDS = frozenset({"schema", "id", "claim", "rubric", "message", "reply"})
TURN_FIELDS = frozenset({"message", "reply"})
CALL_FIELDS = frozenset({"function", "args", "result", "error", "complete", "blocked", "turn"})
# The rubric compile writes: the judge's standard and the conversation the episode sends.
RUBRIC_FIELDS = frozenset({"criteria", "facts", "runtime_facts", "turns", "judge_turns"})
FINAL_TURN = "final"
ALL_TURNS = "all"
JUDGE_TURNS = frozenset({FINAL_TURN, ALL_TURNS})
# Why a request is answered ``unresolved`` without a model request.
RUBRIC_INVALID = "judge_rubric_invalid"
CONTEXT_MISSING = "judge_context_missing"
TURNS_MISSING = "judge_turns_missing"
TURN_COUNT_MISMATCH = "judge_turn_count_mismatch"
TURN_MESSAGE_MISMATCH = "judge_turn_message_mismatch"
TURN_REPLY_MISSING = "judge_turn_reply_missing"
CALL_TURN_INVALID = "judge_call_turn_invalid"
# The service's environment.
MODEL_URL_ENV = "JUDGE_MODEL_URL"
MODEL_ENV = "JUDGE_MODEL"
SERVICE_TOKEN_ENV = "JUDGE_SERVICE_TOKEN"
HOST = "127.0.0.1"
DEFAULT_PORT = 8780
READY = "judge service on"

JudgeCall = Callable[[list[dict[str, str]]], Any]
# Makes the call that asks the model, given the seconds it may take.
CallFactory = Callable[[float], JudgeCall]


class ServiceConfigError(Exception):
    """The service cannot start: its environment lacks a value it needs."""


@dataclass(frozen=True)
class Rubric:
    """The episode's rubric: the judge's standard, the user turns, and the judged turns."""

    standard: Mapping[str, Any]
    turns: tuple[str, ...]
    judge_turns: str


@dataclass(frozen=True)
class Turn:
    """One turn of the conversation MiDojo recorded: the user message and the agent's reply."""

    message: Any
    reply: Any


# --- the request ------------------------------------------------------------------------


def request_problem(request: Any) -> str | None:
    """Return why ``request`` is not a ``midojo.judge.v1`` request, or None."""

    if not isinstance(request, dict):
        return "the request is not a JSON object"
    if request.get("schema") != SCHEMA:
        return f"schema is not {SCHEMA}"
    if set(request) - REQUEST_FIELDS:
        return f"the request has {len(set(request) - REQUEST_FIELDS)} fields outside the schema"
    if missing := sorted(REQUIRED_FIELDS - set(request)):
        return f"the request has no {', '.join(missing)}"
    if not isinstance(request["id"], str):
        return "id is not a string"
    if not isinstance(request["claim"], str) or not request["claim"].strip():
        return "claim is not a non-empty string"
    if request["rubric"] is not None and not isinstance(request["rubric"], (str, dict)):
        return "rubric is not a string, an object or null"
    for name in ("message", "reply"):
        if not _optional_text(request[name]):
            return f"{name} is not a string or null"
    if "prior_turns" in request and not _list_of(request["prior_turns"], _turn_shape):
        return "prior_turns is not a list of turns"
    if ("tool_calls" in request) != ("tool_calls_complete" in request):
        return "tool_calls and tool_calls_complete come together"
    if "tool_calls" in request and not _list_of(request["tool_calls"], _call_shape):
        return "tool_calls is not a list of calls"
    if "tool_calls_complete" in request and type(request["tool_calls_complete"]) is not bool:
        return "tool_calls_complete is not a boolean"
    return None


def _optional_text(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _list_of(value: Any, shape: Callable[[Any], bool]) -> bool:
    return isinstance(value, list) and all(shape(item) for item in value)


def _turn_shape(turn: Any) -> bool:
    return (
        isinstance(turn, dict)
        and set(turn) == TURN_FIELDS
        and all(_optional_text(turn[name]) for name in TURN_FIELDS)
    )


def _call_shape(call: Any) -> bool:
    return (
        isinstance(call, dict)
        and set(call) == CALL_FIELDS
        and isinstance(call["function"], str)
        and _optional_text(call["error"])
        and type(call["complete"]) is bool
        and type(call["blocked"]) is bool
        and (call["turn"] is None or type(call["turn"]) is int)
    )


def parse_rubric(claim: str, rubric: Any) -> Rubric | None:
    """Return the episode's rubric with the claim as its question, or None when it is not one."""

    if (
        not isinstance(rubric, dict)
        or not {"turns", "judge_turns"} <= set(rubric) <= RUBRIC_FIELDS
    ):
        return None
    turns = rubric["turns"]
    if (
        not (isinstance(turns, list) and turns and all(isinstance(t, str) and t for t in turns))
        or rubric["judge_turns"] not in JUDGE_TURNS
    ):
        return None
    if not isinstance(rubric.get("runtime_facts", {}), dict):
        return None
    standard = {
        "question": claim,
        "criteria": rubric.get("criteria", []),
        "facts": rubric.get("facts", []),
        "runtime_facts": rubric.get("runtime_facts", {}),
    }
    return Rubric(standard, tuple(turns), rubric["judge_turns"])


# --- the conversation -------------------------------------------------------------------


def conversation(rubric: Rubric, request: Mapping[str, Any]) -> list[Turn] | str:
    """Return the recorded turns when they are the episode's, each replied, else why not.

    The turns are ``prior_turns`` and then the last one: ``message`` and the judged
    ``reply``. A request without a message recorded no turn. Garak grades no turn of a
    conversation whose turn got no reply text, so neither does this judge.
    """

    if request["message"] is None:
        return TURNS_MISSING
    recorded = [Turn(t["message"], t["reply"]) for t in request["prior_turns"]]
    recorded.append(Turn(request["message"], request["reply"]))
    if len(recorded) != len(rubric.turns):
        return TURN_COUNT_MISMATCH
    if [turn.message for turn in recorded] != list(rubric.turns):
        return TURN_MESSAGE_MISMATCH
    if not all(isinstance(turn.reply, str) and turn.reply for turn in recorded):
        return TURN_REPLY_MISSING
    return recorded


def placed_turns(calls: Sequence[Mapping[str, Any]], count: int) -> list[int] | None:
    """Return the turn each call was made in, or None when one names no turn of ``count``."""

    turns = [call["turn"] for call in calls]
    if all(type(turn) is int and 0 <= turn < count for turn in turns):
        return turns
    return None


def history_before(turns: Sequence[Turn], index: int) -> list[dict[str, str]]:
    """Return the messages and replies before turn ``index``, interleaved as Garak's history."""

    history: list[dict[str, str]] = []
    for turn in turns[:index]:
        history += [
            {"role": "user", "content": turn.message},
            {"role": "assistant", "content": turn.reply},
        ]
    return history


def judge_records(
    rubric: Rubric, request: Mapping[str, Any], call: JudgeCall
) -> list[dict[str, Any]]:
    """Return the rubric judge's record of each judged turn, in order, or the one record of
    why none is judged.

    Garak's attempt for a turn holds every call made so far, so an earlier turn takes the
    calls whose ``turn`` is at most its own; the last turn takes every call.
    """

    if "prior_turns" not in request or "tool_calls" not in request:
        return [_record("unresolved", [], CONTEXT_MISSING)]
    turns = conversation(rubric, request)
    if isinstance(turns, str):
        return [_record("unresolved", [], turns)]
    calls = list(request["tool_calls"])
    count = len(turns)
    placed = placed_turns(calls, count)
    if rubric.judge_turns == ALL_TURNS and count > 1 and placed is None:
        return [_record("unresolved", [], CALL_TURN_INVALID)]
    judged = range(count) if rubric.judge_turns == ALL_TURNS else [count - 1]
    complete = request["tool_calls_complete"] is True and is_complete(calls)
    records = []
    for index in judged:
        made = (
            calls
            if index == count - 1 or placed is None
            else [c for c, turn in zip(calls, placed, strict=True) if turn <= index]
        )
        evidence = build_evidence(
            turns[index].message,
            history_before(turns, index),
            turns[index].reply,
            made,
            complete=complete,
        )
        records.append(judge_output(rubric.standard, evidence, call))
    return records


def judged_count(judge_turns: str, turns: int) -> int:
    """Return how many turns the judge grades: the last, or each of ``turns``."""

    return turns if judge_turns == ALL_TURNS else 1


def budget_seconds(rubric: Rubric) -> float:
    """Return the seconds one request's model requests share: 180 per judged turn."""

    return TIMEOUT_SECONDS * judged_count(rubric.judge_turns, len(rubric.turns))


def judge(request: Mapping[str, Any], calls: CallFactory, model: str | None) -> dict[str, Any]:
    """Return the ``midojo.judge.v1`` response to a valid request, asking the model by a call
    from ``calls`` given the request's budget."""

    rubric = parse_rubric(request["claim"], request["rubric"])
    if rubric is None:
        records = [_record("unresolved", [], RUBRIC_INVALID)]
        judge_turns = None
    else:
        records = judge_records(rubric, request, calls(budget_seconds(rubric)))
        judge_turns = rubric.judge_turns
    final = records[-1]
    return {
        "schema": SCHEMA,
        "id": request["id"],
        "verdict": final["verdict"] if final["verdict"] in VERDICTS else "unresolved",
        "rationale": final["reason"],
        "model": model,
        "details": {
            "prompt_version": PROMPT_VERSION,
            "judge_turns": judge_turns,
            "request_count": sum(request_count(record) for record in records),
            "records": records,
        },
    }


# --- the model --------------------------------------------------------------------------


def model_calls(model_url: str, model: str) -> CallFactory:
    """Return the factory of calls that ask ``model`` at ``model_url`` within a budget.

    Each model request is the rubric judge's ``http_judge``: one send, no retry, no
    redirect, no proxy from the environment, the key from ``OPENAICOMPATIBLE_API_KEY``.
    """

    def calls(budget: float) -> JudgeCall:
        deadline = time.monotonic() + budget

        def call(messages: list[dict[str, str]]) -> Any:
            left = min(deadline - time.monotonic(), TIMEOUT_SECONDS)
            if left <= 0:
                raise JudgeCallError("APITimeoutError")
            return http_judge(model_url, model, timeout=left)(messages)

        return call

    return calls


# --- the server -------------------------------------------------------------------------


def make_server(
    port: int, calls: CallFactory, model: str | None, token: str
) -> ThreadingHTTPServer:
    """Return a server on 127.0.0.1 answering ``POST`` on any path with :func:`judge`, to
    ``token`` only; ``port`` 0 takes a free port."""

    if not token:
        raise ServiceConfigError("the judge service needs a bearer token")
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 -- http.server's name
            given = self.headers.get("Authorization", "")
            if not hmac.compare_digest(given.encode("utf-8", "replace"), expected):
                return self._send(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError, RecursionError):
                return self._send(400, {"error": "the request is not JSON"})
            if (problem := request_problem(request)) is not None:
                return self._send(400, {"error": problem})
            try:
                return self._send(200, judge(request, calls, model))
            except Exception as exc:  # the type only: a message may quote a header
                return self._send(500, {"error": f"the judge failed ({type(exc).__name__})"})

        def _send(self, status: int, body: dict[str, Any]) -> None:
            content = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    return server


def environment_config(environ: Mapping[str, str]) -> tuple[str, str, str]:
    """Return the model URL, the model and the service token from ``environ``."""

    values = {
        name: environ.get(name, "") for name in (MODEL_URL_ENV, MODEL_ENV, SERVICE_TOKEN_ENV)
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ServiceConfigError(f"the judge service needs {', '.join(missing)} set")
    token = values[SERVICE_TOKEN_ENV]
    if not (token.isascii() and token.isprintable()):
        raise ServiceConfigError(f"{SERVICE_TOKEN_ENV} is not printable ASCII")
    return values[MODEL_URL_ENV], values[MODEL_ENV], token


def serve(port: int = DEFAULT_PORT, out: TextIO = sys.stdout) -> None:
    """Serve until SIGINT or SIGTERM; print ``judge service on <url>`` once it listens."""

    model_url, model, token = environment_config(os.environ)
    server = make_server(port, model_calls(model_url, model), model, token)

    def stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    bound_host, bound_port = server.server_address[:2]
    print(f"{READY} http://{bound_host}:{bound_port}/", file=out, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
