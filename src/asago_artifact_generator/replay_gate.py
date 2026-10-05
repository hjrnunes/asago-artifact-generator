"""Replay gate: prove a code change leaves recorded ``generate`` items unchanged.

An orch author stage directory (``runs/<id>/stages/author``) holds the exact
command line of every item in ``stage.json``, each item's console log under
``items/``, and the item's outputs under ``output/``.  Every item writes
``<ID>.failure-evidence.json``, which records each dispatched prompt and the
provider's response.  The gate re-runs every item with the current code::

    python -m asago_artifact_generator.replay_gate check STAGE_DIR [STAGE_DIR ...]

Each item runs in its own process, as under orch, with the recorded
arguments, inputs copied into a scratch directory, provider settings removed
from the environment, and outbound sockets refused.  Only the OpenAI client
inside ``PrivateModelAuthoringTransport`` is replaced: it checks every request
against the recorded prompt and answers with the recorded response.  Argument
parsing, profile loading, request controls, response capture, validation,
corrections, reviews, and detector controls (in Docker) all run for real.  The
gate then compares every output file and the item log with the recording.
Only the differences in :data:`ALLOWED_DIFFERENCES` are permitted.
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

STAGE_FILENAME = "stage.json"
FAILURE_EVIDENCE_SUFFIX = ".failure-evidence.json"
PROG_NAME = "asago-artifact-generator"

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


def _available(value: Any) -> Any:
    if isinstance(value, dict) and value.get("availability") == "available":
        return value.get("value")
    return None


def _recorded_call(attempt: dict[str, Any]) -> RecordedCall:
    index = attempt.get("dispatch_index")
    where = f"dispatch {index} ({attempt.get('stage')})"
    raw_record = attempt.get("raw_response") or {}
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
    prompt = attempt["prompt"]
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
    ``ConnectionError``, so the code under test sees a transport failure.
    """

    def __init__(self, calls: Sequence[RecordedCall]) -> None:
        self._calls = list(calls)
        self.served = 0
        self.mismatches: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=_Completions(self))

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
        call = self._calls.pop(0)
        detail = _request_mismatch(call, request)
        if detail is not None:
            self.mismatches.append(
                {
                    "dispatch_index": call.dispatch_index,
                    "stage": call.stage,
                    "prompt_version": call.prompt_version,
                    "detail": detail,
                }
            )
            raise ConnectionError(
                f"replay gate: prompt mismatch at dispatch {call.dispatch_index} ({call.stage})"
            )
        self.served += 1
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

    Unix sockets stay open; detector controls start Docker through its CLI,
    which runs in its own process.
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
    failure_evidence: Path, status_path: Path, network_log: Path, arguments: list[str]
) -> None:
    """Run one ``generate`` command with the recorded responses (child process)."""

    install_network_guard(network_log)
    client = ReplayChatClient(load_recorded_calls(failure_evidence))
    from asago_artifact_generator import cli

    class _ReplayTransport(cli.PrivateModelAuthoringTransport):  # type: ignore[name-defined]
        def __init__(self, **options: Any) -> None:
            super().__init__(**options)
            self._client = client

    cli.PrivateModelAuthoringTransport = _ReplayTransport  # type: ignore[misc]
    try:
        cli.app(args=arguments, prog_name=PROG_NAME)
    finally:
        status_path.write_text(
            json.dumps(
                {
                    "served": client.served,
                    "unused": client.unused,
                    "mismatches": client.mismatches,
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


def _compare_structured(
    left_text: str, right_text: str, relative: str, aliases: dict[str, str]
) -> str | None:
    left = _json_or_none(left_text)
    right = _json_or_none(right_text)
    if left is None or right is None:
        return "content is not JSON on both sides"
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
    recorded: Path, replayed: Path, relative: str, aliases: dict[str, str]
) -> str | None:
    """Return the first difference not covered by an allowance, or ``None``."""

    left = recorded.read_bytes()
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
        return _compare_structured(left_text, right_text, relative, aliases)
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
) -> tuple[int, list[Difference]]:
    """Compare one item's output files and console log with the recording.

    *recorded_output_label* is the output directory as the recording names it
    (default: *recorded_output*).
    """

    aliases = _location_aliases(replayed_output, recorded_output_label or str(recorded_output))
    left, right = _owned_files(recorded_output, item_id), _owned_files(replayed_output, item_id)
    differences = [Difference(name, "only in recording") for name in sorted(left - right)]
    differences += [Difference(name, "only in replay") for name in sorted(right - left)]
    shared = sorted(left & right)
    for relative in shared:
        detail = compare_file(
            recorded_output / relative, replayed_output / relative, relative, aliases
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
    return compared, differences


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
    network_attempts: list[str] = field(default_factory=list)
    files_compared: int = 0
    differences: list[Difference] = field(default_factory=list)
    error: str | None = None
    log_tail: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return (
            self.error is None
            and self.exit_code == self.recorded_exit_code
            and self.status == self.recorded_status
            and not self.mismatches
            and self.unused == 0
            and not self.network_attempts
            and not self.differences
        )


@dataclass
class GateResult:
    stage_dir: Path
    work: Path
    items: list[ItemResult] = field(default_factory=list)
    skipped: int = 0
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return bool(self.items) and all(item.passed for item in self.items)

    def report(self, limit: int = 10) -> str:
        failed = [item for item in self.items if not item.passed]
        lines = [
            f"stage:      {self.stage_dir}",
            f"scratch:    {self.work}",
            f"items:      {len(self.items)} replayed, {self.skipped} skipped by the recording",
            "statuses:   " + _status_counts(self.items, "recorded_status"),
            f"dispatches: {sum(item.served for item in self.items)} served",
            f"files:      {sum(item.files_compared for item in self.items)} compared",
            f"time:       {self.seconds:.1f}s",
            f"failed:     {len(failed)}",
        ]
        for item in failed[:limit]:
            lines += _failure_lines(item, limit)
        lines.append("PASS" if self.passed else "FAIL")
        return "\n".join(lines)


def _status_counts(items: Iterable[ItemResult], attribute: str) -> str:
    statuses = Counter(getattr(item, attribute) for item in items)
    return ", ".join(f"{name} {count}" for name, count in sorted(statuses.items(), key=str))


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
    prepared: _PreparedItem, *, stage_dir: Path, work: Path, output_dir: Path
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
        "-m",
        "asago_artifact_generator.replay_gate",
        "_replay-item",
        str(record),
        str(status_path),
        str(network_log),
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
    else:
        result.error = "the replayed item wrote no replay status"
    if network_log.is_file():
        result.network_attempts = network_log.read_text(encoding="utf-8").splitlines()
    result.files_compared, result.differences = compare_item_outputs(
        prepared.item_id,
        recorded_output=stage_dir / "output",
        recorded_log=recorded_log,
        replayed_output=output_dir,
        replayed_log=replay_log,
        recorded_output_label=prepared.recorded_output_label,
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
) -> GateResult:
    """Replay every recorded item of one author stage and compare the outputs.

    The recorded stage directory is only read.  *only* restricts the replay to
    the named item ids.
    """

    started = time.perf_counter()
    stage_dir = stage_dir.resolve()
    work = Path(tempfile.mkdtemp(prefix="replay-gate-")) if work is None else work
    work.mkdir(parents=True, exist_ok=True)
    work = work.resolve()
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
                    item, stage_dir=stage_dir, work=work, output_dir=output_dir
                ),
                prepared,
            )
        )
    result.seconds = time.perf_counter() - started
    return result


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
    return "\n".join(
        [
            "== summary",
            f"stages:          {len(results)}",
            f"items:           {len(items)} replayed, {len(items) - len(failed)} identical",
            "statuses:        " + _status_counts(items, "status"),
            f"dispatches:      {sum(item.served for item in items)} served",
            f"prompt mismatch: {mismatches}",
            f"wall time:       {seconds:.1f}s",
            "PASS" if results and not failed else "FAIL",
        ]
    )


def main(argv: Iterable[str] | None = None) -> int:
    items = list(sys.argv[1:] if argv is None else argv)
    if items[:1] == ["_replay-item"]:
        return _replay_item_command(items[1:])
    return _check(_parser().parse_args(items))


def _replay_item_command(items: list[str]) -> int:
    record, status, network, separator, *arguments = items
    if separator != "--":
        raise SystemExit("usage: _replay-item RECORD STATUS NETWORK_LOG -- AUTHOR_ARGS...")
    _replay_item(Path(record), Path(status), Path(network), arguments)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m asago_artifact_generator.replay_gate",
        description="Replay recorded author items offline and compare every output.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="replay recorded orch author stage directories")
    check.add_argument("stage_dirs", nargs="+", type=Path, help="runs/<id>/stages/author")
    check.add_argument("--jobs", type=int, default=1, help="items replayed in parallel")
    check.add_argument("--item", action="append", help="replay only this item id (repeatable)")
    check.add_argument("--work-dir", type=Path, help="scratch directory (default: a temp dir)")
    check.add_argument("--keep", action="store_true", help="keep the scratch directory on a pass")
    check.add_argument("--show", type=int, default=10, help="failures and differences to print")
    check.add_argument("--json", type=Path, help="also write per-item results to this file")
    return parser


def _check(options: argparse.Namespace) -> int:
    root = options.work_dir or Path(tempfile.mkdtemp(prefix="replay-gate-"))
    started = time.perf_counter()
    results = []
    for number, stage_dir in enumerate(options.stage_dirs):
        work = root / f"{number:02d}-{stage_dir.resolve().parent.parent.name}"
        result = run_gate(stage_dir, work=work, jobs=options.jobs, only=options.item)
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
