"""Replay gate: prove a code change leaves recorded ``generate`` items unchanged.

An author stage directory recorded by the orchestrator holds the exact
command line of every item in ``stage.json``, each item's console log under
``items/``, and the item's outputs under ``output/``.  Each item that
dispatched a request writes ``<ID>.failure-evidence.json``, accepted or not,
which records each dispatched prompt and the provider's response.  The gate
re-runs every item with the current code::

    python scripts/replay_gate.py check STAGE_DIR [STAGE_DIR ...]

The gate is development tooling: it lives outside the shipped package,
imports the package, and is never imported by it.

Each item runs in its own process, as under the orchestrator, with the recorded
arguments, inputs copied into a scratch directory, provider settings removed
from the environment, and outbound sockets refused.  Only the OpenAI client
inside ``PrivateModelAuthoringTransport`` is replaced, through the CLI's
transport factory: it checks every request
against the recorded prompt and answers with the recorded response.  Argument
parsing, profile loading, request controls, response capture, validation,
corrections, and reviews all run for real.  The gate then compares every
output file and the item log with the recording.  Only the differences in
:data:`ALLOWED_DIFFERENCES` are permitted.

A prompt the code renders differently from the recording is a prompt mismatch,
unless :data:`EXPECTED_PROMPT_DIFFERENCES` declares the change.  The gate then
answers the request with the recorded response anyway, reports the difference
as declared, and compares the outputs the change produces.  A difference no
entry declares fails, and so does an entry whose from-version the replayed
recording carries but which matches no difference: it is stale.

An item the code intentionally no longer reproduces is declared superseded in
:data:`SUPERSEDED_ITEMS` instead.  The gate still replays the item, and it
counts as superseded only when its one prompt mismatch is the declared
dispatch and every earlier dispatch was served identically; its divergent exit
code, status and outputs then follow from that one change.  Each declaration
is pinned to the recording, item, dispatch, recorded stage and version, and the
recorded prompt digest, so a re-recorded or edited recording does not inherit
it.  A declaration whose recording no longer mismatches at the declared
dispatch is stale, and the gate fails.  When an expected prompt difference
declares the same dispatch as a text-identical version move, the declared
record takes precedence and the item compares as a normal replay.

The gate compares recorded files as written. :func:`declared_package_layout`
rewrites a recorded package only when the replay adds a declared advisory
finding; the report counts and names each package rewrite.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Child processes run this file by absolute path; their working directory is
# scratch space.
_SCRIPT = Path(__file__).resolve()
STAGE_FILENAME = "stage.json"
FAILURE_EVIDENCE_SUFFIX = ".failure-evidence.json"
PROG_NAME = "asago-artifact-generator"
# The declarations the gate replay applies, written for each child process.
_DECLARED_FILENAME = "declared-prompt-differences.json"

# Options whose value is a path the item writes; the gate redirects it.
_OUTPUT_OPTIONS = frozenset({"--output-dir"})
# Read in place: the profiles file holds endpoint credentials, and copying it
# into scratch space would spread them.  The profile's controls are recorded
# with every dispatch and compared through the outputs.
_IN_PLACE_OPTIONS = frozenset({"--profiles-file"})
_STRIPPED_ENV_PREFIXES = ("ASAGO_", "OPENAI_", "OPENROUTER_", "REDTEAM_")
_STRIPPED_ENV_NAMES = frozenset({"GEMINI_API_KEY", "GOOGLE_API_KEY", "FORCE_COLOR"})


@dataclass(frozen=True)
class AllowedDifference:
    """One documented difference between a recorded and a replayed file.

    *file* is a glob over paths relative to the stage's ``output/`` directory,
    or ``items/<ID>.log`` for item logs.  Each top-level field in *fields*
    holds a path inside the item's output directory; the replayed value must
    equal the recorded one once the scratch output directory is mapped back
    to the recorded output directory.  Nothing else in the file may differ.
    """

    file: str
    fields: tuple[str, ...]
    reason: str


_OUTPUT_LOCATION = "the replay writes into a scratch output directory, not the recorded one"

# Keep this list short.  Add an entry only for a value that legitimately
# differs between two executions of the same code on the same responses, and
# fix nondeterminism in the code instead of listing it here.
ALLOWED_DIFFERENCES: tuple[AllowedDifference, ...] = (
    AllowedDifference("*.failure-evidence.json", ("package_path",), _OUTPUT_LOCATION),
    AllowedDifference("*.blocked.json", ("package_path",), _OUTPUT_LOCATION),
    AllowedDifference("items/*.log", ("package",), _OUTPUT_LOCATION),
)


# --- declared prompt differences ---------------------------------------------------


@dataclass(frozen=True)
class PromptIdentity:
    """The stage and prompt version the code under test renders for one dispatch."""

    stage: str
    version: str


@dataclass(frozen=True)
class ExpectedPromptDifference:
    """One intended prompt change that a recording made before it may still show.

    *stage* is the recorded dispatch stage.  *template* is the prompt template
    family: a version names its family and build, ``authoring-call1-v27``.
    *from_prompt_version* is the version the recording carries and
    *to_prompt_version* the version the current code renders; both versions
    must name *template*.  *bead* is the backlog item that owns the change and
    *reason* says why the change is intended.

    A difference is declared when the recorded dispatch has the entry's stage
    and from-version, the replayed prompt has its to-version, and both versions
    name its template.  The recorded response then still answers the request,
    so the gate compares the outputs the change produces, and the difference is
    reported as declared, not as a mismatch.  An entry whose from-version a
    replayed recording carries but which matches no difference is stale: the
    recording is probably new enough that the change is in it, so remove the
    entry.  An entry is also stale-free while another declared change for the
    same stage leaves a version the recording carries: a prompt can advance by
    more than one version over unchanged text, and the middle versions belong
    to no recording.  An unlisted difference always fails.
    """

    stage: str
    template: str
    from_prompt_version: str
    to_prompt_version: str
    bead: str
    reason: str


# Add one entry per prompt change that a replayed recording predates;
# remove the entry once that recording is re-recorded.
EXPECTED_PROMPT_DIFFERENCES: tuple[ExpectedPromptDifference, ...] = ()


@dataclass(frozen=True)
class SupersededItem:
    """One recorded item a later code change intentionally no longer reproduces.

    *recording* is the recording's folder name under the corpus ``consumer/``
    directory, and *item* the item id inside it.  *dispatch_index* is the
    recorded dispatch the code no longer renders, with its *stage* and
    *prompt_version*; *prompt_sha256* is the recorded prompt digest over the
    stage, version and both messages.  *bead* owns the change and *reason*
    says why the item is superseded.

    The item is still replayed.  It counts as superseded only when its one
    prompt mismatch is the declared dispatch and every earlier dispatch was
    served identically, so its divergent exit code, status and outputs follow
    from that one change.  When an expected prompt difference declares the
    same dispatch as a text-identical version move, the declared record takes
    precedence and the item compares as a normal replay.  A declaration whose
    recording replays the item without that mismatch, or whose recorded
    dispatch carries another prompt digest, is stale: remove it.
    """

    recording: str
    item: str
    dispatch_index: int
    stage: str
    prompt_version: str
    prompt_sha256: str
    bead: str
    reason: str


# Add one entry per item affected by a prompt change its recording predates;
# remove the entry once the item is re-recorded.
SUPERSEDED_ITEMS: tuple[SupersededItem, ...] = ()


def prompt_template(version: str) -> str:
    """Return the template family a prompt version names (``X-v3`` -> ``X``)."""

    family, separator, number = version.rpartition("-v")
    if not separator or not number.isdigit():
        return version
    return family


class ReplayRecordError(ValueError):
    """A recorded dispatch cannot be served back to the current code."""


# --- recorded calls -----------------------------------------------------------


@dataclass(frozen=True)
class RecordedCall:
    """One recorded provider exchange, enough to rebuild the SDK response."""

    dispatch_index: int
    stage: str
    prompt_version: str
    system: str
    user: str
    requested_model: str
    returned_model: str | None
    raw: bytes
    final_answer: dict[str, Any]
    reasoning: dict[str, Any]
    finish_reason: dict[str, Any]
    usage: dict[str, Any] | None
    # The failures the transport retried before this response, oldest first;
    # each is a recorded ``retry_of`` ({"type", "status_code"}).
    retries: tuple[dict[str, Any], ...] = ()
    # The dispatch ended in a provider timeout: no response was recorded, and
    # the replay raises the timeout instead of serving one.
    timed_out: bool = False


_RETRYABLE_ERROR_TYPES = frozenset({"APIConnectionError", "InternalServerError", "APIStatusError"})
# The transport's reason for the request it sends on the fallback tier after a
# rate limit; a transport retry records no reason.
SERVICE_TIER_FALLBACK_REASON = "service_tier_fallback"
_FALLBACK_FAILURE = {"type": "RateLimitError", "status_code": 429}


def _rebuildable_retry(failure: dict[str, Any], reason: Any) -> bool:
    """Return whether the transport records this failure with this reason."""

    if reason == SERVICE_TIER_FALLBACK_REASON:
        return failure == _FALLBACK_FAILURE
    if reason is not None or failure.get("type") not in _RETRYABLE_ERROR_TYPES:
        return False
    is_connection = failure.get("type") == "APIConnectionError"
    return is_connection == (failure.get("status_code") is None)


def _recorded_retries(attempt: dict[str, Any], where: str) -> tuple[dict[str, Any], ...]:
    failures = []
    for retry in attempt.get("transport_retries") or []:
        failure = retry.get("retry_of") or {}
        if not _rebuildable_retry(failure, retry.get("reason")):
            raise ReplayRecordError(f"{where}: retry_of cannot be rebuilt: {failure}")
        failures.append({"type": failure["type"], "status_code": failure.get("status_code")})
    return tuple(failures)


def _replay_request() -> Any:
    import httpx2

    return httpx2.Request("POST", "https://replay.invalid/v1/chat/completions")


def _recorded_failure(failure: dict[str, Any]) -> Exception:
    """Rebuild the provider error a recorded retry followed."""

    import httpx2
    import openai

    request = _replay_request()
    if failure["status_code"] is None:
        return openai.APIConnectionError(message="replayed connection error", request=request)
    response = httpx2.Response(failure["status_code"], request=request)
    error_type = getattr(openai, failure["type"])
    return error_type("replayed provider error", response=response, body=None)


def _available(value: Any) -> Any:
    if isinstance(value, dict) and value.get("availability") == "available":
        return value.get("value")
    return None


# The transport's failure record keeps only the redacted message, and this is
# the one provider error whose message identifies its type.
_TIMEOUT_DETAIL = "Request timed out."


def _is_recorded_timeout(attempt: dict[str, Any], raw_record: dict[str, Any]) -> bool:
    failure = attempt.get("failure") or {}
    return (
        raw_record.get("availability") == "unavailable"
        and raw_record.get("reason") == "provider_failure"
        and failure.get("phase") == "invocation"
        and failure.get("detail") == _TIMEOUT_DETAIL
    )


def _recorded_response(
    attempt: dict[str, Any], raw_record: dict[str, Any], where: str
) -> tuple[bytes, dict[str, Any], dict[str, Any], dict[str, Any]]:
    if raw_record.get("availability") != "available":
        # Provider errors are recorded only as a failure code and detail, so
        # the exception the live call raised cannot be raised again.
        raise ReplayRecordError(f"{where}: raw_response is not available")
    raw = base64.b64decode(raw_record["base64"])
    if hashlib.sha256(raw).hexdigest() != raw_record.get("sha256"):
        raise ReplayRecordError(f"{where}: raw_response bytes do not match their sha256")
    final_answer, reasoning, finish_reason = _rebuildable_captures(attempt, where)
    content = final_answer.get("content") if final_answer["state"] in {"empty", "text"} else None
    expected_raw = content.encode("utf-8") if isinstance(content, str) else b""
    if raw != expected_raw:
        raise ReplayRecordError(f"{where}: raw_response differs from final_answer content")
    return raw, final_answer, reasoning, finish_reason


def _prompt_digest(stage: str, version: str, system: str, user: str) -> str:
    """Return the digest a recording states for one rendered prompt."""

    rendered = "\0".join((stage, version, system, user))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _check_recorded_prompt(stage: str, prompt: dict[str, Any], where: str) -> None:
    """Refuse a recording whose stated prompt digest does not match its prompt text.

    A recording made before the journal stated digests carries neither field
    and is accepted as it stands.  One that states a digest must state the
    digest of the prompt it carries: a declared difference trusts the recorded
    text, so a text change that leaves the digest behind cannot be replayed.
    """

    digest = _prompt_digest(stage, prompt["version"], prompt["system"], prompt["user"])
    for name in ("sha256", "hash"):
        stated = prompt.get(name)
        if stated is not None and stated != digest:
            raise ReplayRecordError(
                f"{where}: undeclared prompt mismatch: the recorded prompt does not"
                f" match its recorded {name}"
            )


def _recorded_call(attempt: dict[str, Any]) -> RecordedCall:
    index = attempt.get("dispatch_index")
    where = f"dispatch {index} ({attempt.get('stage')})"
    raw_record = attempt.get("raw_response") or {}
    timed_out = _is_recorded_timeout(attempt, raw_record)
    if timed_out:
        raw, final_answer, reasoning, finish_reason = b"", {}, {}, {}
    else:
        raw, final_answer, reasoning, finish_reason = _recorded_response(
            attempt, raw_record, where
        )
    prompt = attempt["prompt"]
    _check_recorded_prompt(attempt["stage"], prompt, where)
    identity = attempt["model_identity"]
    return RecordedCall(
        dispatch_index=index,
        stage=attempt["stage"],
        prompt_version=prompt["version"],
        system=prompt["system"],
        user=prompt["user"],
        requested_model=identity["requested_model"],
        returned_model=_available(identity.get("returned_model")),
        raw=raw,
        final_answer=final_answer,
        reasoning=reasoning,
        finish_reason=finish_reason,
        usage=_available(attempt.get("usage")),
        retries=_recorded_retries(attempt, where),
        timed_out=timed_out,
    )


_TEXT_STATES = frozenset({"absent", "null", "empty", "text"})
_CAPTURE_STATES = (
    ("final_answer", _TEXT_STATES, "final_answer state cannot be rebuilt"),
    ("reasoning", _TEXT_STATES, "reasoning state cannot be rebuilt"),
    ("finish_reason", frozenset({"absent", "null", "value"}), "finish_reason cannot be rebuilt"),
)


def _rebuildable_captures(attempt: dict[str, Any], where: str) -> list[dict[str, Any]]:
    capture = attempt.get("response_capture") or {}
    parts = []
    for name, states, problem in _CAPTURE_STATES:
        part = capture.get(name) or {}
        if part.get("state") not in states:
            raise ReplayRecordError(f"{where}: {problem}: {part}")
        parts.append(part)
    return parts


def load_recorded_calls(failure_evidence: Path) -> list[RecordedCall]:
    """Return the dispatches recorded in one item's failure evidence, in order."""

    document = json.loads(failure_evidence.read_text(encoding="utf-8"))
    attempts = sorted(document.get("attempts") or [], key=lambda item: item["dispatch_index"])
    return [_recorded_call(attempt) for attempt in attempts]


class _Completions:
    def __init__(self, client: ReplayChatClient) -> None:
        self._client = client

    def create(self, **request: Any) -> SimpleNamespace:
        return self._client.create(**request)


class ReplayChatClient:
    """Serve recorded responses in dispatch order to the real transport.

    It stands in for ``openai.OpenAI``.  Every request must carry the recorded
    model and the exact recorded system and user messages.  A mismatch, or a
    request beyond the recording, is reported and refused with a
    ``ConnectionError``, so the code under test sees a transport failure.  The
    one exception is a prompt difference an :data:`EXPECTED_PROMPT_DIFFERENCES`
    entry declares: the recorded response is served, and the difference is
    appended to :attr:`declared` instead of :attr:`mismatches`.  The prompt the
    code renders for the request is in :attr:`prompt`, which the transport
    wrapper in :func:`_replay_item` sets before every dispatch; without it no
    difference is declared.
    A dispatch recorded with a transport retry or a service-tier fallback
    first raises each recorded failure (a 429 ``RateLimitError`` before the
    fallback), then serves the response, so the transport's own retry and
    fallback run for real; a recording without either never sees one.  A dispatch recorded as a
    timeout raises ``openai.APITimeoutError`` in place of a response, which the
    transport never retries.
    """

    def __init__(self, calls: Sequence[RecordedCall]) -> None:
        self._calls = list(calls)
        self.served = 0
        self.mismatches: list[dict[str, Any]] = []
        self.declared: list[dict[str, Any]] = []
        self.prompt: PromptIdentity | None = None
        self.chat = SimpleNamespace(completions=_Completions(self))
        self._failures_raised = 0

    @property
    def unused(self) -> int:
        return len(self._calls)

    def create(self, **request: Any) -> SimpleNamespace:
        if not self._calls:
            self.mismatches.append(
                {
                    "dispatch_index": None,
                    "stage": None,
                    "prompt_version": None,
                    "detail": "request beyond the recorded dispatches",
                }
            )
            raise ConnectionError("replay gate: no recorded response for this request")
        call = self._calls[0]
        detail = _request_mismatch(call, request)
        if detail is None and self._failures_raised < len(call.retries):
            failure = call.retries[self._failures_raised]
            self._failures_raised += 1
            raise _recorded_failure(failure)
        self._calls.pop(0)
        self._failures_raised = 0
        if detail is not None:
            entry = (
                _declared_prompt_difference(call, self.prompt)
                if detail in _PROMPT_DETAILS
                else None
            )
            if entry is None:
                self.mismatches.append(
                    {
                        "dispatch_index": call.dispatch_index,
                        "stage": call.stage,
                        "prompt_version": call.prompt_version,
                        "detail": detail,
                    }
                )
                raise ConnectionError(
                    f"replay gate: prompt mismatch at dispatch {call.dispatch_index}"
                    f" ({call.stage})"
                )
            self.declared.append(_declared_difference(call, self.prompt, entry, detail))
        else:
            # A prompt whose two messages the code renders unchanged still
            # moves versions when the change is text-identical, and the
            # replayed evidence then records a prompt digest and version the
            # recording does not hold.  An entry declares that move the same
            # way, so the comparison masks the prompt record it already masks
            # for a differing text.
            entry = _declared_prompt_difference(call, self.prompt)
            if entry is not None:
                self.declared.append(_declared_difference(call, self.prompt, entry, detail))
        self.served += 1
        return _serve(call)


def _serve(call: RecordedCall) -> SimpleNamespace:
    if call.timed_out:
        import openai

        raise openai.APITimeoutError(request=_replay_request())
    return _rebuilt_response(call)


def _request_mismatch(call: RecordedCall, request: dict[str, Any]) -> str | None:
    if request.get("model") != call.requested_model:
        return "requested model differs from the recording"
    messages = request.get("messages")
    if not isinstance(messages, list) or [m.get("role") for m in messages] != ["system", "user"]:
        return "messages are not one system and one user message"
    if messages[0].get("content") != call.system:
        return "system message differs from the recording"
    if messages[1].get("content") != call.user:
        return "user message differs from the recording"
    return None


_PROMPT_DETAILS = frozenset(
    {"system message differs from the recording", "user message differs from the recording"}
)


def _declared_prompt_difference(
    call: RecordedCall, prompt: PromptIdentity | None
) -> ExpectedPromptDifference | None:
    """Return the entry that declares this dispatch's prompt difference, if one does.

    The entry must name the recorded stage and from-version, the replayed
    stage and to-version, and the template family both versions belong to.  A
    prompt rendered at the recorded version is never declared: a text change
    at an unchanged version is not the change an entry names.
    """

    if prompt is None or prompt.stage != call.stage:
        return None
    if prompt.version == call.prompt_version:
        return None
    for entry in EXPECTED_PROMPT_DIFFERENCES:
        if entry.from_prompt_version == entry.to_prompt_version:
            continue
        if (
            entry.stage == call.stage
            and entry.from_prompt_version == call.prompt_version
            and entry.to_prompt_version == prompt.version
            and entry.template == prompt_template(call.prompt_version)
            and entry.template == prompt_template(prompt.version)
        ):
            return entry
    return None


def _declared_difference(
    call: RecordedCall, prompt: PromptIdentity, entry: ExpectedPromptDifference, detail: str
) -> dict[str, Any]:
    """Return the report record for one declared prompt difference."""

    return {
        "dispatch_index": call.dispatch_index,
        "stage": call.stage,
        "template": entry.template,
        "from_prompt_version": call.prompt_version,
        "to_prompt_version": prompt.version,
        "bead": entry.bead,
        "reason": entry.reason,
        "detail": detail,
    }


def _captured_value(capture: dict[str, Any]) -> Any:
    state = capture["state"]
    if state == "null":
        return None
    if state == "empty":
        return ""
    return capture["content"]


def _rebuilt_response(call: RecordedCall) -> SimpleNamespace:
    """Rebuild the SDK fields the transport reads; absent fields stay absent."""

    message: dict[str, Any] = {}
    if call.final_answer["state"] != "absent":
        message["content"] = _captured_value(call.final_answer)
    if call.reasoning["state"] != "absent":
        message[call.reasoning["source_field"]] = _captured_value(call.reasoning)
    choice = SimpleNamespace(message=message)
    if call.finish_reason["state"] == "null":
        choice.finish_reason = None
    elif call.finish_reason["state"] == "value":
        choice.finish_reason = call.finish_reason["value"]
    return SimpleNamespace(model=call.returned_model, usage=call.usage, choices=[choice])


# --- environment and network ------------------------------------------------------


def replay_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return *base* without model endpoint, credential, or color settings."""

    env = dict(os.environ if base is None else base)
    for key in list(env):
        if (
            key.startswith(_STRIPPED_ENV_PREFIXES)
            or key.endswith("_API_KEY")
            or key in _STRIPPED_ENV_NAMES
        ):
            del env[key]
    return env


def install_network_guard(log_path: Path) -> None:
    """Refuse and log every outbound IP connection and name lookup in this process.

    Unix sockets stay open.
    """

    def refuse(target: object) -> None:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{target}\n")
        raise ConnectionRefusedError(f"replay gate refused network access: {target}")

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            refuse(address)
        return original_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            refuse(address)
        return original_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        refuse(f"resolve {host}")

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]


def _replay_item(
    failure_evidence: Path,
    status_path: Path,
    network_log: Path,
    arguments: list[str],
    declared_path: Path | None = None,
) -> None:
    """Run one ``generate`` command with the recorded responses (child process).

    *declared_path* holds the prompt differences the gate declares, which the
    parent passes because the checked-in list is only its default.
    """

    global EXPECTED_PROMPT_DIFFERENCES

    if declared_path is not None:
        EXPECTED_PROMPT_DIFFERENCES = _entries_from_document(
            json.loads(declared_path.read_text(encoding="utf-8"))
        )
    install_network_guard(network_log)
    client = ReplayChatClient(load_recorded_calls(failure_evidence))
    from asago_artifact_generator import cli
    from asago_artifact_generator.authoring.transport import PrivateModelAuthoringTransport

    class RecordedPromptTransport(PrivateModelAuthoringTransport):
        """Give the replay client the prompt the code renders for each dispatch."""

        def complete(self, packet: Any, session: Any = None) -> Any:
            client.prompt = PromptIdentity(packet.stage, packet.version)
            return super().complete(packet, session)

    def transport(**options: Any) -> PrivateModelAuthoringTransport:
        return RecordedPromptTransport(**options, client=client)

    try:
        cli.run(arguments, prog_name=PROG_NAME, transport_factory=transport)
    finally:
        status_path.write_text(
            json.dumps(
                {
                    "served": client.served,
                    "unused": client.unused,
                    "mismatches": client.mismatches,
                    "declared": client.declared,
                }
            ),
            encoding="utf-8",
        )


# --- comparison -------------------------------------------------------------------


@dataclass
class Difference:
    path: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}: {self.detail}"


def _allowed_fields(relative: str) -> tuple[str, ...]:
    return tuple(
        name
        for allowed in ALLOWED_DIFFERENCES
        if fnmatch.fnmatch(relative, allowed.file)
        for name in allowed.fields
    )


def _map_location(value: Any, aliases: dict[str, str]) -> Any:
    if not isinstance(value, str):
        return value
    for replayed, recorded in aliases.items():
        if value == replayed or value.startswith(replayed + "/"):
            return recorded + value[len(replayed) :]
    return value


def _apply_allowances(document: Any, relative: str, aliases: dict[str, str]) -> Any:
    if not isinstance(document, dict):
        return document
    names = _allowed_fields(relative)
    return {
        key: _map_location(value, aliases) if key in names else value
        for key, value in document.items()
    }


def _first_difference(left: Any, right: Any, where: str = "$") -> str | None:
    if type(left) is not type(right):
        return f"{where}: {_short(left)} != {_short(right)}"
    if isinstance(left, dict):
        return _first_dict_difference(left, right, where)
    if isinstance(left, list):
        return _first_list_difference(left, right, where)
    if left != right:
        return f"{where}: {_short(left)} != {_short(right)}"
    return None


def _first_dict_difference(left: dict[Any, Any], right: dict[Any, Any], where: str) -> str | None:
    for key in sorted(set(left) | set(right), key=str):
        if key not in left:
            return f"{where}.{key}: only in replay"
        if key not in right:
            return f"{where}.{key}: only in recording"
        found = _first_difference(left[key], right[key], f"{where}.{key}")
        if found:
            return found
    if list(left) != list(right):
        return f"{where}: key order {list(left)} != {list(right)}"
    return None


def _first_list_difference(left: list[Any], right: list[Any], where: str) -> str | None:
    for index, (a, b) in enumerate(zip(left, right, strict=False)):
        found = _first_difference(a, b, f"{where}[{index}]")
        if found:
            return found
    if len(left) != len(right):
        return f"{where}: length {len(left)} != {len(right)}"
    return None


def _short(value: Any, limit: int = 160) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _mask_attempt_duration(attempt: Any) -> Any:
    failure = attempt.get("failure") if isinstance(attempt, dict) else None
    duration = failure.get("elapsed_ms") if isinstance(failure, dict) else None
    if not isinstance(duration, int | float):
        return attempt
    return {**attempt, "failure": {**failure, "elapsed_ms": "<wall-clock milliseconds>"}}


def _mask_wall_clock(document: Any) -> Any:
    """Hide the measured time of each failed dispatch, the one value that differs.

    A replayed failure returns at once, so the time the live call took cannot
    recur.  Its presence and type still compare.
    """

    attempts = document.get("attempts") if isinstance(document, dict) else None
    if not isinstance(attempts, list):
        return document
    return {**document, "attempts": [_mask_attempt_duration(item) for item in attempts]}


# A review records the prompt it reviewed beside its decision; a declared
# difference changes that prompt's version, so these pins travel with it.  The
# reviewed input digest hashes the prompt version too, so it moves with the
# version even when the prompt's two messages do not.
_REVIEW_PROMPT_PINS = ("prompt_version", "prompt_sha256", "reviewed_input_sha256")


def _declared_version_pairs(
    entries: Sequence[ExpectedPromptDifference] | None = None,
) -> list[dict[str, str]]:
    """Return the version pair every expected prompt difference names."""

    declared = EXPECTED_PROMPT_DIFFERENCES if entries is None else entries
    return [
        {
            "from_prompt_version": entry.from_prompt_version,
            "to_prompt_version": entry.to_prompt_version,
        }
        for entry in declared
    ]


def _entries_document(entries: Sequence[ExpectedPromptDifference]) -> list[dict[str, str]]:
    """Return the declarations as the child process reads them."""

    return [asdict(entry) for entry in entries]


def _entries_from_document(document: Any) -> tuple[ExpectedPromptDifference, ...]:
    """Return the declarations a parent process passed to its child."""

    if not isinstance(document, list):
        raise ValueError("declared prompt differences must be a JSON list")
    return tuple(ExpectedPromptDifference(**record) for record in document)


def _mask_declared_prompts(
    recorded: Any, replayed: Any, declared: Sequence[dict[str, Any]]
) -> Any:
    """Return *replayed* with each declared version pair's prompt record as recorded.

    A declared difference is the one change a dispatch's prompt may show: its
    version, its two messages and their digests.  The recording keeps the
    prompt it dispatched and the replay writes the prompt it renders, so the
    replayed evidence's record of a dispatch that moved between a declared pair
    of versions is replaced by the recorded one; every other field of the
    attempt still compares, and a review's pins of the prompt it reviewed are
    replaced the same way.  A dispatch whose two messages did not change still
    moves to the declared version, so its record is masked as well: the replay
    served its recorded response only after the transport compared both
    messages.
    """

    pairs = {(item.get("from_prompt_version"), item.get("to_prompt_version")) for item in declared}
    if not pairs or not isinstance(recorded, dict) or not isinstance(replayed, dict):
        return replayed
    recorded_attempts = {
        attempt.get("dispatch_index"): attempt
        for attempt in recorded.get("attempts") or []
        if isinstance(attempt, dict)
    }
    attempts = []
    for attempt in replayed.get("attempts") or []:
        if not isinstance(attempt, dict):
            attempts.append(attempt)
            continue
        original = recorded_attempts.get(attempt.get("dispatch_index"))
        if not isinstance(original, dict):
            attempts.append(attempt)
            continue
        prompt, recorded_prompt = attempt.get("prompt"), original.get("prompt")
        if not isinstance(prompt, dict) or not isinstance(recorded_prompt, dict):
            attempts.append(attempt)
            continue
        if (recorded_prompt.get("version"), prompt.get("version")) not in pairs:
            attempts.append(attempt)
            continue
        masked = dict(attempt)
        masked["prompt"] = recorded_prompt
        if "reviewed_input_sha256" in original:
            masked["reviewed_input_sha256"] = original["reviewed_input_sha256"]
        reviewed = masked.get("review")
        if isinstance(reviewed, dict):
            pinned = original.get("review")
            if isinstance(pinned, dict):
                masked["review"] = {
                    **reviewed,
                    **{key: pinned[key] for key in _REVIEW_PROMPT_PINS if key in pinned},
                }
        attempts.append(masked)
    return {**replayed, "attempts": attempts}


def _compare_structured(
    left_text: str,
    right_text: str,
    relative: str,
    aliases: dict[str, str],
    declared: Sequence[dict[str, Any]] = (),
) -> str | None:
    left = _json_or_none(left_text)
    right = _json_or_none(right_text)
    if left is None or right is None:
        return "content is not JSON on both sides"
    if relative.endswith(FAILURE_EVIDENCE_SUFFIX):
        left, right = _mask_wall_clock(left), _mask_wall_clock(right)
        right = _mask_declared_prompts(left, right, declared)
    return _first_difference(left, _apply_allowances(right, relative, aliases))


def _compare_log(
    left_text: str, right_text: str, relative: str, aliases: dict[str, str]
) -> str | None:
    left_lines, right_lines = left_text.splitlines(), right_text.splitlines()
    for number, (a, b) in enumerate(zip(left_lines, right_lines, strict=False), start=1):
        if a == b:
            continue
        left, right = _json_or_none(a), _json_or_none(b)
        if isinstance(left, dict) and isinstance(right, dict):
            found = _first_difference(left, _apply_allowances(right, relative, aliases))
            if found:
                return f"line {number}: {found}"
            continue
        return f"line {number}: {a[:160]!r} != {b[:160]!r}"
    if len(left_lines) != len(right_lines):
        return f"{len(left_lines)} lines != {len(right_lines)} lines"
    return None


def compare_file(
    recorded: Path,
    replayed: Path,
    relative: str,
    aliases: dict[str, str],
    recorded_bytes: bytes | None = None,
    declared: Sequence[dict[str, Any]] = (),
) -> str | None:
    """Return the first difference not covered by an allowance, or ``None``.

    *recorded_bytes*, when given, stands in for the recorded file's content.
    *declared* holds the prompt version pairs the expected differences name,
    whose prompt records do not count as differences.
    """

    left = recorded.read_bytes() if recorded_bytes is None else recorded_bytes
    right = replayed.read_bytes()
    if left == right:
        return None
    try:
        left_text, right_text = left.decode("utf-8"), right.decode("utf-8")
    except UnicodeDecodeError:
        return "binary content differs"
    if relative.startswith("items/"):
        return _compare_log(left_text, right_text, relative, aliases)
    if recorded.suffix == ".json":
        return _compare_structured(left_text, right_text, relative, aliases, declared)
    for number, (a, b) in enumerate(
        zip(left_text.splitlines(), right_text.splitlines(), strict=False), start=1
    ):
        if a != b:
            return f"line {number}: {a[:160]!r} != {b[:160]!r}"
    return "content differs in line endings or length"


def _owned_files(root: Path, item_id: str) -> set[str]:
    """Return files under *root* that belong to *item_id* (``ID/…`` or ``ID.*``)."""

    if not root.is_dir():
        return set()
    owned: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        head = relative.parts[0]
        if head == item_id or head.startswith(item_id + "."):
            owned.add(relative.as_posix())
    return owned


# D21 (term.7) writes the advisory slot_party_mismatch finding into the package's
# checks.json.  A recording made before it carries that member without findings;
# the replay recomputes the same slot and writes one.  The finding is the only
# change, so the recorded member is rewritten with the replay's findings and the
# manifest member record and digest follow.  A recording that carries findings,
# as the re-recorded corpus does, comes back unchanged.
_ADVISORY_FINDING_CODES = frozenset({"slot_party_mismatch"})


def _advisory_findings(document: Any) -> list[Any] | None:
    """Return a package's findings when every one is a declared advisory finding."""

    if not isinstance(document, dict):
        return None
    findings = document.get("findings")
    if not isinstance(findings, list) or not findings:
        return None
    if not all(
        isinstance(item, dict) and item.get("code") in _ADVISORY_FINDING_CODES for item in findings
    ):
        return None
    return findings


def declared_package_layout(
    recorded_output: Path,
    replayed_output: Path,
    item_id: str,
    rewritten: dict[str, bytes],
) -> dict[str, bytes]:
    """Return recorded package files rewritten for a declared advisory finding.

    The rewrite applies only when the recorded ``checks.json`` has no findings,
    the replay adds advisory findings alone, and every other field matches.  The
    manifest's member record for ``checks.json`` and its digest are recomputed
    over the recorded manifest.
    """

    from asago_artifact_generator.contract_kit import canonical_json
    from asago_package.package_io import _manifest_digest, _member_record

    checks_name = f"{item_id}/checks.json"
    manifest_name = f"{item_id}/manifest.json"
    if (
        not (recorded_output / checks_name).is_file()
        or not (replayed_output / checks_name).is_file()
    ):
        return rewritten
    recorded = _json_or_none((recorded_output / checks_name).read_text(encoding="utf-8"))
    replayed = _json_or_none((replayed_output / checks_name).read_text(encoding="utf-8"))
    if not isinstance(recorded, dict) or "findings" in recorded:
        return rewritten
    if _advisory_findings(replayed) is None:
        return rewritten
    without_findings = {key: value for key, value in replayed.items() if key != "findings"}
    if recorded != without_findings:
        return rewritten
    replayed_bytes = (replayed_output / checks_name).read_bytes()
    rewritten = {**rewritten, checks_name: replayed_bytes}
    manifest_bytes = rewritten.get(manifest_name)
    if manifest_bytes is None:
        path = recorded_output / manifest_name
        if not path.is_file():
            return rewritten
        manifest_bytes = path.read_bytes()
    manifest = _json_or_none(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("members"), list):
        return rewritten
    manifest["members"] = [
        _member_record("checks.json", replayed_bytes)
        if isinstance(record, dict) and record.get("path") == "checks.json"
        else record
        for record in manifest["members"]
    ]
    manifest["manifest_digest"] = _manifest_digest(manifest)
    rewritten[manifest_name] = (canonical_json(manifest) + "\n").encode("utf-8")
    return rewritten


def _location_aliases(replayed_output: Path, recorded_label: str) -> dict[str, str]:
    aliases = {str(replayed_output): recorded_label}
    aliases.setdefault(str(replayed_output.resolve()), recorded_label)
    return aliases


def compare_item_outputs(
    item_id: str,
    *,
    recorded_output: Path,
    recorded_log: Path,
    replayed_output: Path,
    replayed_log: Path,
    recorded_output_label: str | None = None,
    declared: Sequence[dict[str, Any]] = (),
) -> tuple[int, list[Difference], bool]:
    """Compare one item's output files and console log with the recording.

    *recorded_output_label* is the output directory as the recording names it
    (default: *recorded_output*).  *declared* holds the prompt version pairs the
    expected differences name, whose prompt records do not count as
    differences.  The flag says whether :func:`declared_package_layout`
    rewrote the recording, which the gate reports so the in-memory rewrite is
    visible.
    """

    aliases = _location_aliases(replayed_output, recorded_output_label or str(recorded_output))
    left = _owned_files(recorded_output, item_id)
    rewritten: dict[str, bytes] = {}
    right = _owned_files(replayed_output, item_id)
    checks_name = f"{item_id}/checks.json"
    rewritten = declared_package_layout(recorded_output, replayed_output, item_id, rewritten)
    layout_declared = checks_name in rewritten
    differences = [Difference(name, "only in recording") for name in sorted(left - right)]
    differences += [Difference(name, "only in replay") for name in sorted(right - left)]
    shared = sorted(left & right)
    for relative in shared:
        detail = compare_file(
            recorded_output / relative,
            replayed_output / relative,
            relative,
            aliases,
            rewritten.get(relative),
            declared,
        )
        if detail:
            differences.append(Difference(relative, detail))
    compared = len(shared)
    log_name = f"items/{item_id}.log"
    if not replayed_log.is_file():
        differences.append(Difference(log_name, "only in recording"))
    else:
        compared += 1
        detail = compare_file(recorded_log, replayed_log, log_name, aliases)
        if detail:
            differences.append(Difference(log_name, detail))
    return compared, differences, layout_declared


# --- the gate ----------------------------------------------------------------------


@dataclass
class ItemResult:
    item_id: str
    recorded_exit_code: int | None
    recorded_status: str | None
    exit_code: int | None = None
    status: str | None = None
    served: int = 0
    unused: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    declared: list[dict[str, Any]] = field(default_factory=list)
    network_attempts: list[str] = field(default_factory=list)
    files_compared: int = 0
    differences: list[Difference] = field(default_factory=list)
    # The recorded checks.json had no findings and the replay adds
    # advisory findings alone.
    layout_declared: bool = False
    # The declaration that supersedes this item's one prompt mismatch, when
    # the gate matched it; see :class:`SupersededItem`.
    superseded: SupersededItem | None = None
    error: str | None = None
    log_tail: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        if self.superseded is not None:
            if _declared_covers(self.superseded, self):
                # A declared prompt difference covered the pinned dispatch, so
                # the replay continued past it and the item's outputs diverge
                # from the change the entry names.  The prefix guard still
                # applies: no error, no network attempt, no prompt mismatch,
                # and every recorded dispatch before the pinned one served.
                return (
                    self.error is None
                    and not self.network_attempts
                    and not self.mismatches
                    and self.served >= self.superseded.dispatch_index
                )
            # The replay stops at the declared dispatch, so its exit code,
            # status and outputs cannot match the recording.  The prefix guard
            # is the evidence that nothing else diverged; an error or a network
            # attempt still fails the item.
            return (
                self.error is None
                and not self.network_attempts
                and _superseded_mismatch(self) is not None
            )
        return (
            self.error is None
            and self.exit_code == self.recorded_exit_code
            and self.status == self.recorded_status
            and not self.mismatches
            and self.unused == 0
            and not self.network_attempts
            and not self.differences
        )


def _superseded_mismatch(item: ItemResult) -> dict[str, Any] | None:
    """Return the prompt mismatch a superseded declaration excuses, if it does.

    The declaration excuses only the declared dispatch: the item's one prompt
    mismatch must name its index, recorded stage and recorded version, and
    every earlier dispatch must have been served identically.  A declared
    prompt difference at an earlier dispatch does not block the declaration:
    a text-identical version move is declared, not mismatched, so the replay
    still served that dispatch's recorded response.
    """

    entry = item.superseded
    if entry is None or len(item.mismatches) != 1:
        return None
    if any(record.get("dispatch_index") >= entry.dispatch_index for record in item.declared):
        return None
    mismatch = item.mismatches[0]
    if (
        mismatch.get("dispatch_index") != entry.dispatch_index
        or mismatch.get("stage") != entry.stage
        or mismatch.get("prompt_version") != entry.prompt_version
        or mismatch.get("detail") not in _PROMPT_DETAILS
    ):
        return None
    if item.served != entry.dispatch_index - 1:
        return None
    return mismatch


@dataclass
class GateResult:
    stage_dir: Path
    work: Path
    items: list[ItemResult] = field(default_factory=list)
    stale: list[ExpectedPromptDifference] = field(default_factory=list)
    superseded: list[SupersededItem] = field(default_factory=list)
    stale_superseded: list[SupersededItem] = field(default_factory=list)
    skipped: int = 0
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return (
            bool(self.items)
            and not self.stale
            and not self.stale_superseded
            and all(item.passed for item in self.items)
        )

    def report(self, limit: int = 10) -> str:
        failed = [item for item in self.items if not item.passed]
        declared = [(item.item_id, record) for item in self.items for record in item.declared]
        layouts = [item.item_id for item in self.items if item.layout_declared]
        lines = [
            f"stage:      {self.stage_dir}",
            f"scratch:    {self.work}",
            f"items:      {len(self.items)} replayed, {self.skipped} skipped by the recording",
            "statuses:   " + _status_counts(self.items, "recorded_status"),
            f"dispatches: {sum(item.served for item in self.items)} served",
            f"declared:   {len(declared)} prompt differences",
            f"layout:     {len(layouts)} declared package layouts",
            f"superseded: {len(self.superseded)} declared superseded items",
            f"files:      {sum(item.files_compared for item in self.items)} compared",
            f"time:       {self.seconds:.1f}s",
            f"failed:     {len(failed)}",
        ]
        for item_id in layouts[:limit]:
            lines.append(f"  layout declared: {item_id}")
        for entry in self.superseded[:limit]:
            lines.append(
                f"  superseded: {entry.recording} {entry.item} dispatch {entry.dispatch_index}"
                f" ({entry.stage}, {entry.prompt_version}): bead {entry.bead}: {entry.reason}"
            )
        for entry in self.stale_superseded[:limit]:
            lines.append(
                f"  stale superseded item: {entry.recording} {entry.item}"
                f" dispatch {entry.dispatch_index} ({entry.stage}, {entry.prompt_version}):"
                f" bead {entry.bead}: the recording no longer mismatches at the"
                " declared dispatch"
            )
        for item_id, record in declared[:limit]:
            lines.append(
                f"  declared: {item_id} dispatch {record['dispatch_index']}"
                f" ({record['stage']}): {record['detail']},"
                f" {record['from_prompt_version']} -> {record['to_prompt_version']}"
                f" (bead {record['bead']})"
            )
        for entry in self.stale[:limit]:
            lines.append(
                f"  stale declared prompt difference: {entry.stage}"
                f" {entry.from_prompt_version} -> {entry.to_prompt_version}"
                f" (bead {entry.bead}): the recording carries its from-version"
                " but no prompt difference matched it"
            )
        for item in failed[:limit]:
            lines += _failure_lines(item, limit)
        lines.append("PASS" if self.passed else "FAIL")
        return "\n".join(lines)


def _status_counts(items: Iterable[ItemResult], attribute: str) -> str:
    statuses = Counter(getattr(item, attribute) for item in items)
    return ", ".join(f"{name} {count}" for name, count in sorted(statuses.items(), key=str))


def recording_id(stage_dir: Path) -> str:
    """Return the corpus recording id of an author stage directory.

    A recording sits at ``consumer/<recording>/stages/author``, so the folder
    two levels above the stage directory names it.
    """

    return Path(stage_dir).resolve().parent.parent.name


def _recorded_dispatch(stage_dir: Path, entry: SupersededItem) -> RecordedCall | None:
    """Return the recorded dispatch a superseded declaration pins, if it exists."""

    evidence = stage_dir / "output" / f"{entry.item}{FAILURE_EVIDENCE_SUFFIX}"
    try:
        calls = load_recorded_calls(evidence)
    except (OSError, ReplayRecordError, KeyError, ValueError):
        return None
    return next(
        (call for call in calls if call.dispatch_index == entry.dispatch_index),
        None,
    )


def _apply_superseded_items(
    stage_dir: Path,
    items: Sequence[ItemResult],
    entries: Sequence[SupersededItem],
) -> tuple[list[SupersededItem], list[SupersededItem]]:
    """Attach each matching superseded declaration to its item.

    Returns the declarations that matched and the stale ones.  A declaration
    applies to the recording whose folder name it names.  It is stale when that
    recording replays its item and the declared dispatch is not the item's one
    prompt mismatch, or when the recorded dispatch carries another prompt
    digest.  An item this run did not replay is left alone, so ``--item``
    narrows the replay without making the other declarations stale.

    A declaration also matches when an :data:`EXPECTED_PROMPT_DIFFERENCES`
    entry declared the pinned dispatch as a text-identical version move: the
    recording's prompt text and the rendered prompt text are the same and only
    the version moved, so the replay serves the recorded response and
    continues, and the item compares as a normal replay.  The declared
    difference is the narrower statement of that move, so the superseded
    declaration stays matched rather than stale.
    """

    recording = recording_id(stage_dir)
    by_id = {item.item_id: item for item in items}
    matched: list[SupersededItem] = []
    stale: list[SupersededItem] = []
    for entry in entries:
        if entry.recording != recording:
            continue
        item = by_id.get(entry.item)
        if item is None:
            continue
        call = _recorded_dispatch(stage_dir, entry)
        digest_matches = (
            call is not None
            and call.stage == entry.stage
            and call.prompt_version == entry.prompt_version
            and _prompt_digest(call.stage, call.prompt_version, call.system, call.user)
            == entry.prompt_sha256
        )
        if digest_matches:
            if _declared_covers(entry, item):
                item.superseded = entry
                matched.append(entry)
                continue
            item.superseded = entry
            if _superseded_mismatch(item) is not None:
                matched.append(entry)
                continue
            item.superseded = None
        stale.append(entry)
    return matched, stale


def _declared_covers(entry: SupersededItem, item: ItemResult) -> bool:
    """Return whether a declared prompt difference covered the pinned dispatch.

    The declared record must name the entry's dispatch, recorded stage and
    recorded version, whether or not its prompt text differs: the declared
    version move and the change the entry pins are the same divergence, and
    the replay continued past the dispatch the entry pins either way.  The
    item then compares as a normal replay, so an output difference the entry's
    change produces still fails the item unless the entry is matched for that
    reason; see :meth:`ItemResult.passed`.
    """

    return any(
        record.get("dispatch_index") == entry.dispatch_index
        and record.get("stage") == entry.stage
        and record.get("from_prompt_version") == entry.prompt_version
        for record in item.declared
    )


def _failure_lines(item: ItemResult, limit: int) -> list[str]:
    lines = [
        f"  {item.item_id}: exit {item.exit_code} (recorded {item.recorded_exit_code}),"
        f" status {item.status} (recorded {item.recorded_status}),"
        f" unused records {item.unused}"
    ]
    if item.error:
        lines.append(f"    error: {item.error}")
    lines += [f"    prompt mismatch: {mismatch}" for mismatch in item.mismatches]
    lines += [f"    network: {attempt}" for attempt in item.network_attempts[:limit]]
    lines += [f"    {difference}" for difference in item.differences[:limit]]
    lines += [f"    log: {line[:300]}" for line in item.log_tail]
    return lines


@dataclass
class _PreparedItem:
    item_id: str
    arguments: list[str]
    recorded_output_label: str
    recorded_exit_code: int | None


def _generate_arguments(argv: Sequence[str]) -> list[str]:
    try:
        start = list(argv).index("generate")
    except ValueError as error:
        raise ValueError(f"recorded argv has no 'generate' command: {list(argv)}") from error
    return list(argv[start:])


class _InputCopies:
    """Copy each recorded input once into scratch space, keeping its file name."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.copies: dict[Path, Path] = {}

    def copy(self, source: Path) -> Path:
        if source not in self.copies:
            target_dir = self.root / f"{len(self.copies):03d}"
            target_dir.mkdir(parents=True)
            target = target_dir / source.name
            shutil.copy2(source, target)
            # The handoff loader reads the Gherkin companion beside the scenario.
            companion = source.with_suffix(".feature")
            if companion != source and companion.is_file():
                shutil.copy2(companion, target_dir / companion.name)
            self.copies[source] = target
        return self.copies[source]


def _prepare_item(item: dict[str, Any], copies: _InputCopies, output_dir: Path) -> _PreparedItem:
    arguments = _generate_arguments(item["argv"])
    rewritten: list[str] = []
    recorded_output: str | None = None
    for index, value in enumerate(arguments):
        previous = arguments[index - 1] if index else None
        if value.startswith("--") and "=" in value:
            raise ValueError(f"unsupported '--option=value' form in recorded argv: {value}")
        if previous in _OUTPUT_OPTIONS:
            recorded_output = value
            rewritten.append(str(output_dir))
        else:
            rewritten.append(_rewritten_argument(value, previous, copies))
    if recorded_output is None:
        raise ValueError(f"recorded argv for {item['id']} has no --output-dir")
    return _PreparedItem(
        item_id=item["id"],
        arguments=rewritten,
        recorded_output_label=recorded_output,
        recorded_exit_code=item.get("exit_code"),
    )


def _rewritten_argument(value: str, previous: str | None, copies: _InputCopies) -> str:
    if previous in _IN_PLACE_OPTIONS or value.startswith("-"):
        return value
    if Path(value).is_absolute() and Path(value).is_file():
        return str(copies.copy(Path(value)))
    return value


def _logged_status(log: Path) -> str | None:
    if not log.is_file():
        return None
    lines = log.read_text(encoding="utf-8").splitlines()
    first = _json_or_none(lines[0]) if lines else None
    return first.get("status") if isinstance(first, dict) else None


def _run_item(
    prepared: _PreparedItem,
    *,
    stage_dir: Path,
    work: Path,
    output_dir: Path,
    declared: Sequence[ExpectedPromptDifference],
    declared_path: Path,
) -> ItemResult:
    recorded_log = stage_dir / "items" / f"{prepared.item_id}.log"
    result = ItemResult(
        item_id=prepared.item_id,
        recorded_exit_code=prepared.recorded_exit_code,
        recorded_status=_logged_status(recorded_log),
    )
    started = time.perf_counter()
    evidence = stage_dir / "output" / f"{prepared.item_id}{FAILURE_EVIDENCE_SUFFIX}"
    state = work / "state" / prepared.item_id
    state.mkdir(parents=True)
    record = state / evidence.name
    try:
        shutil.copy2(evidence, record)
        load_recorded_calls(record)
    except (OSError, ReplayRecordError, KeyError, ValueError) as error:
        result.error = f"cannot load the recorded dispatches: {error}"
        return result
    status_path = state / "status.json"
    network_log = state / "network.log"
    replay_log = work / "items" / f"{prepared.item_id}.log"
    command = [
        sys.executable,
        str(_SCRIPT),
        "_replay-item",
        str(record),
        str(status_path),
        str(network_log),
        "--declared",
        str(declared_path),
        "--",
        *prepared.arguments,
    ]
    with replay_log.open("w", encoding="utf-8") as log:
        result.exit_code = subprocess.run(
            command,
            cwd=state,
            env=replay_environment(),
            stdout=log,
            stderr=subprocess.STDOUT,
        ).returncode
    result.seconds = time.perf_counter() - started
    result.status = _logged_status(replay_log)
    if status_path.is_file():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        result.served = status["served"]
        result.unused = status["unused"]
        result.mismatches = status["mismatches"]
        result.declared = status.get("declared") or []
    else:
        result.error = "the replayed item wrote no replay status"
    if network_log.is_file():
        result.network_attempts = network_log.read_text(encoding="utf-8").splitlines()
    result.files_compared, result.differences, result.layout_declared = compare_item_outputs(
        prepared.item_id,
        recorded_output=stage_dir / "output",
        recorded_log=recorded_log,
        replayed_output=output_dir,
        replayed_log=replay_log,
        recorded_output_label=prepared.recorded_output_label,
        declared=_declared_version_pairs(declared),
    )
    if result.exit_code != result.recorded_exit_code or result.error:
        result.log_tail = replay_log.read_text(encoding="utf-8").splitlines()[-5:]
    return result


def run_gate(
    stage_dir: Path,
    *,
    work: Path | None = None,
    jobs: int = 1,
    only: Iterable[str] | None = None,
    declared: Sequence[ExpectedPromptDifference] | None = None,
    superseded: Sequence[SupersededItem] | None = None,
) -> GateResult:
    """Replay every recorded item of one author stage and compare the outputs.

    The recorded stage directory is only read.  *only* restricts the replay to
    the named item ids.  *declared* holds the prompt differences the replay
    declares and *superseded* the items it declares superseded; both default to
    the checked-in lists.
    """

    started = time.perf_counter()
    stage_dir = stage_dir.resolve()
    entries = EXPECTED_PROMPT_DIFFERENCES if declared is None else tuple(declared)
    superseded_entries = SUPERSEDED_ITEMS if superseded is None else tuple(superseded)
    work = Path(tempfile.mkdtemp(prefix="replay-gate-")) if work is None else work
    work.mkdir(parents=True, exist_ok=True)
    work = work.resolve()
    declared_path = work / _DECLARED_FILENAME
    declared_path.write_text(json.dumps(_entries_document(entries)), encoding="utf-8")
    document = json.loads((stage_dir / STAGE_FILENAME).read_text(encoding="utf-8"))
    result = GateResult(stage_dir=stage_dir, work=work)
    authored, result.skipped = _authored_items(document, only)
    output_dir = work / "output"
    output_dir.mkdir(exist_ok=True)
    (work / "items").mkdir(exist_ok=True)
    copies = _InputCopies(work / "inputs")
    prepared = [_prepare_item(item, copies, output_dir) for item in authored]
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        result.items = list(
            pool.map(
                lambda item: _run_item(
                    item,
                    stage_dir=stage_dir,
                    work=work,
                    output_dir=output_dir,
                    declared=entries,
                    declared_path=declared_path,
                ),
                prepared,
            )
        )
    result.stale = _stale_entries(
        _recorded_prompt_identities(stage_dir, result.items),
        [record for item in result.items for record in item.declared],
        entries,
    )
    result.superseded, result.stale_superseded = _apply_superseded_items(
        stage_dir, result.items, superseded_entries
    )
    result.seconds = time.perf_counter() - started
    return result


def _recorded_prompt_identities(
    stage_dir: Path, items: Iterable[ItemResult]
) -> set[tuple[str, str]]:
    """Return the stage and prompt version of every dispatch the item recordings carry."""

    identities = set()
    for item in items:
        evidence = stage_dir / "output" / f"{item.item_id}{FAILURE_EVIDENCE_SUFFIX}"
        try:
            calls = load_recorded_calls(evidence)
        except (OSError, ReplayRecordError, KeyError, ValueError):
            continue
        identities.update((call.stage, call.prompt_version) for call in calls)
    return identities


def _stale_entries(
    recorded: set[tuple[str, str]],
    declared: Sequence[dict[str, Any]],
    entries: Sequence[ExpectedPromptDifference] | None = None,
) -> list[ExpectedPromptDifference]:
    """Return the entries a replayed recording carries but no difference matched.

    An entry applies to a recording that dispatched its stage at its
    from-version.  If the replay of that recording declared no such change, the
    entry is stale: the recording is probably new enough that the change is in
    it, so remove the entry instead of keeping a declaration nothing uses.  A
    recording whose prompt text the code renders unchanged still declares the
    version move alone, because the recorded version and the rendered version
    differ; only a recording the code renders at the entry's from-version with
    the same text leaves an entry stale.
    """

    matched = {
        (record["stage"], record["from_prompt_version"], record["to_prompt_version"])
        for record in declared
    }
    candidates = EXPECTED_PROMPT_DIFFERENCES if entries is None else entries
    return [
        entry
        for entry in candidates
        if (entry.stage, entry.from_prompt_version) in recorded
        and (entry.stage, entry.from_prompt_version, entry.to_prompt_version) not in matched
    ]


def _authored_items(
    document: dict[str, Any], only: Iterable[str] | None
) -> tuple[list[dict[str, Any]], int]:
    """Return the recorded items that ran ``generate`` and how many others the stage holds."""

    selected = set(only) if only is not None else None
    recorded = document.get("items") or []
    authored = [item for item in recorded if "argv" in item]
    skipped = len(recorded) - len(authored)
    if selected is not None:
        authored = [item for item in authored if item["id"] in selected]
    return authored, skipped


def _summary(results: Sequence[GateResult], seconds: float) -> str:
    items = [item for result in results for item in result.items]
    failed = [item for item in items if not item.passed]
    mismatches = sum(len(item.mismatches) for item in items)
    declared = sum(len(item.declared) for item in items)
    layouts = [item.item_id for item in items if item.layout_declared]
    superseded = [entry for result in results for entry in result.superseded]
    stale = [entry for result in results for entry in result.stale]
    stale += [entry for result in results for entry in result.stale_superseded]
    layout_line = f"layout declared: {len(layouts)}"
    if layouts:
        layout_line += f" ({', '.join(layouts)})"
    return "\n".join(
        [
            "== summary",
            f"stages:          {len(results)}",
            f"items:           {len(items)} replayed, {len(items) - len(failed)} identical",
            "statuses:        " + _status_counts(items, "status"),
            f"dispatches:      {sum(item.served for item in items)} served",
            f"prompt declared: {declared}",
            layout_line,
            f"superseded:      {len(superseded)}",
            f"prompt mismatch: {mismatches}",
            f"stale entries:   {len(stale)}",
            f"wall time:       {seconds:.1f}s",
            "PASS" if results and not failed and not stale else "FAIL",
        ]
    )


def main(argv: Iterable[str] | None = None) -> int:
    items = list(sys.argv[1:] if argv is None else argv)
    if items[:1] == ["_replay-item"]:
        return _replay_item_command(items[1:])
    return _check(_parser().parse_args(items))


def _replay_item_command(items: list[str]) -> int:
    record, status, network, *rest = items
    declared = None
    if rest[:1] == ["--declared"]:
        declared = Path(rest[1])
        rest = rest[2:]
    separator, *arguments = rest
    if separator != "--":
        raise SystemExit(
            "usage: _replay-item RECORD STATUS NETWORK_LOG [--declared PATH] -- AUTHOR_ARGS..."
        )
    _replay_item(Path(record), Path(status), Path(network), arguments, declared)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/replay_gate.py",
        description="Replay recorded author items offline and compare every output.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="replay recorded author stage directories")
    check.add_argument("stage_dirs", nargs="+", type=Path, help="recorded author stage directory")
    check.add_argument("--jobs", type=int, default=1, help="items replayed in parallel")
    check.add_argument("--item", action="append", help="replay only this item id (repeatable)")
    check.add_argument("--work-dir", type=Path, help="scratch directory (default: a temp dir)")
    check.add_argument("--keep", action="store_true", help="keep the scratch directory on a pass")
    check.add_argument("--show", type=int, default=10, help="failures and differences to print")
    check.add_argument("--json", type=Path, help="also write per-item results to this file")
    return parser


def _declared_superseded() -> tuple[SupersededItem, ...]:
    """Return the superseded declarations the gate applies, read at call time."""

    return SUPERSEDED_ITEMS


def _check(options: argparse.Namespace) -> int:
    root = options.work_dir or Path(tempfile.mkdtemp(prefix="replay-gate-"))
    started = time.perf_counter()
    results = []
    for number, stage_dir in enumerate(options.stage_dirs):
        work = root / f"{number:02d}-{stage_dir.resolve().parent.parent.name}"
        result = run_gate(
            stage_dir,
            work=work,
            jobs=options.jobs,
            only=options.item,
            superseded=_declared_superseded(),
        )
        print(f"== {stage_dir}")
        print(result.report(options.show), flush=True)
        results.append(result)
    print(_summary(results, time.perf_counter() - started))
    passed = bool(results) and all(result.passed for result in results)
    if options.json:
        _write_results_json(options.json, results)
    if passed and not options.keep and options.work_dir is None:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if passed else 1


def _write_results_json(path: Path, results: list[GateResult]) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "stage_dir": str(result.stage_dir),
                    "items": [asdict(item) for item in result.items],
                    "stale": [asdict(entry) for entry in result.stale],
                    "superseded": [asdict(entry) for entry in result.superseded],
                    "stale_superseded": [asdict(entry) for entry in result.stale_superseded],
                }
                for result in results
            ],
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
