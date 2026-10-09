"""parse: turn Garak's report for one concrete bundle into ``execution-receipt-v1``.

A usable report holds exactly one finished attempt (status 2) with one output
whose response completed. Anything else yields a ``failed`` receipt whose
``incomplete_reason`` names what was wrong; the receipt is still written.

A sequential bundle's report holds one finished attempt per user turn, each
naming its turn in ``notes.sequence``. The receipt lists every planned turn
with the calls that turn made; the final turn's detections grade the
conversation. A turn the probe marked failed, or a report that ends before the
last turn, yields a ``failed`` receipt with ``incomplete_reason``
``turn_failed:<position>`` (counted from 1), as orch names it.

When orch's boundary services recorded the package, their records replace
the report's evidence: ``mcp_capture/calls.jsonl`` the tool calls,
``boundary/gateway-accounting.json`` the generation request count, and
``boundary/judge-exchanges.jsonl`` the judge request count. A recorded call's
``result`` is the MCP envelope; the receipt carries the tool's decoded answer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asago_bundle_core.errors import BundleError
from asago_bundle_core.text import canonical_text

from .compiler import (
    BUNDLE_FILE,
    CONVERSATIONS,
    RUN_CONFIG,
    SEQUENTIAL,
    TOOL,
)

SCHEMA_VERSION = "execution-receipt-v1"
ATTEMPT_COMPLETE = 2
OUTCOMES = {1.0: "detected", 0.0: "not_detected"}
INCONCLUSIVE = "inconclusive"
DRIVERS = {"command_attempt": "ToolCallCondition", "reply": "RubricJudge"}
RECORD_NOTES = {
    "ToolCallCondition": "tool_call_condition_results",
    "RubricJudge": "rubric_judge_results",
}
PROXY_CALLS = Path("mcp_capture/calls.jsonl")
GATEWAY_ACCOUNTING = Path("boundary/gateway-accounting.json")
JUDGE_EXCHANGES = Path("boundary/judge-exchanges.jsonl")
RUNTIME_LISTS = (
    "processes",
    "network_calls_allowed",
    "network_calls_blocked",
    "files_created",
    "files_modified",
    "files_deleted",
    "security_findings",
)
CALL_FIELDS = ("name", "arguments", "result", "error", "status")
MANIFEST_KEYS = frozenset(
    {"scenario_id", "package_digest", "target_mode", "claim_level", "native_outputs"}
)


class ParseError(BundleError):
    """The bundle cannot be read, so no receipt can name its package."""


@dataclass(frozen=True)
class Records:
    """What the boundary services recorded for the package, when present."""

    calls: list[dict[str, Any]] | None = None
    request_count: int | None = None
    judge_count: int | None = None


@dataclass(frozen=True)
class Bundle:
    root: Path
    manifest: dict[str, Any]
    messages: list[dict[str, str]]


def parse_report(
    bundle_dir: str | Path,
    out_path: str | Path,
    *,
    report: str | Path | None = None,
    records_dir: str | Path | None = None,
    tool_revision: str | None = None,
    adapter_revision: str | None = None,
) -> dict[str, Any]:
    """Write the receipt for one bundle's report to ``out_path`` and return it."""

    bundle = read_bundle(Path(bundle_dir))
    out = Path(out_path)
    report_file = Path(report) if report else bundle.root / bundle.manifest["native_outputs"][0]
    records = read_records(Path(records_dir)) if records_dir else Records()
    if bundle.manifest.get("delivery") == SEQUENTIAL:
        attempt, reason = sequential_attempt(report_file, bundle, records)
    else:
        finished, reason = finished_attempt(report_file)
        attempt = build_attempt(finished, bundle, records) if finished else None
        reason = reason or generation_failure(finished)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"id": TOOL, "revision": tool_revision, "adapter_revision": adapter_revision},
        "package": {
            "id": bundle.manifest["scenario_id"],
            "digest": bundle.manifest["package_digest"],
        },
        "bundle_digest": bundle_digest(bundle.root),
        "target": {
            "mode": bundle.manifest["target_mode"],
            "image_digest": None,
            "policy_digest": None,
            "sandbox_id": None,
        },
        "execution_status": "failed" if reason else "completed",
        "runtime_status": None if reason else "completed",
        "incomplete_reason": reason,
        "attempts": [attempt] if attempt else [],
        "native": native_files(report_file, out.parent),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_text(receipt), encoding="utf-8")
    return receipt


def read_bundle(root: Path) -> Bundle:
    try:
        manifest = json.loads((root / BUNDLE_FILE).read_text(encoding="utf-8"))
        entry = json.loads((root / CONVERSATIONS).read_text(encoding="utf-8").splitlines()[0])
        messages = [
            {"role": item["role"], "content": item["content"]} for item in entry["messages"]
        ]
        missing = MANIFEST_KEYS - manifest.keys()
    except (OSError, IndexError, KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ParseError(f"bundle is unreadable: {exc}") from exc
    if missing or not manifest["native_outputs"]:
        raise ParseError(f"bundle manifest lacks {sorted(missing) or ['native_outputs']}")
    return Bundle(root, manifest, messages)


def read_records(directory: Path) -> Records:
    calls_file = directory / PROXY_CALLS
    accounting = _read_json(directory / GATEWAY_ACCOUNTING)
    exchanges = directory / JUDGE_EXCHANGES
    return Records(
        calls=[_proxy_call(line) for line in _lines(calls_file)] if calls_file.is_file() else None,
        request_count=_count(
            accounting.get("responses_request_count") if isinstance(accounting, dict) else None
        ),
        judge_count=len(_lines(exchanges)) if exchanges.is_file() else None,
    )


def read_entries(path: Path) -> tuple[list[Any] | None, str | None]:
    if not path.is_file():
        return None, "report_missing"
    entries = []
    for line in _lines(path):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            return None, "report_malformed"
    return entries, None


def finished_attempt(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Return the report's one finished attempt with one output, or why not."""

    entries, reason = read_entries(path)
    if entries is None:
        return None, reason
    finished = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("entry_type") == "attempt"
        and entry.get("status") == ATTEMPT_COMPLETE
    ]
    if len(finished) != 1:
        return None, "multiple_finished_attempts" if finished else "no_finished_attempt"
    if single_output(finished[0]) is None:
        return None, "no_output"
    return finished[0], None


def single_output(attempt: dict[str, Any]) -> dict[str, Any] | None:
    outputs = attempt.get("outputs")
    if isinstance(outputs, list) and len(outputs) == 1 and isinstance(outputs[0], dict):
        return outputs[0]
    return None


def output_notes(attempt: dict[str, Any]) -> dict[str, Any]:
    notes = single_output(attempt).get("notes")
    return notes if isinstance(notes, dict) else {}


def generation_failure(attempt: dict[str, Any] | None) -> str | None:
    if attempt is None:
        return None
    notes = output_notes(attempt)
    if isinstance(notes.get("error"), dict):
        return "generation_error"
    if notes.get("response_status") != "completed":
        return "response_not_completed"
    return None


def build_attempt(attempt: dict[str, Any], bundle: Bundle, records: Records) -> dict[str, Any]:
    output = single_output(attempt)
    notes = output_notes(attempt)
    source, calls = observed_calls(notes, records)
    replies = [output["text"]] if isinstance(output.get("text"), str) else []
    detections = detections_of(attempt)
    error = notes.get("error")
    return {
        "index": 0,
        "generation": {
            "request_count": records.request_count,
            "response_status": _text(notes.get("response_status")),
            "error_type": _text(error.get("type")) if isinstance(error, dict) else None,
        },
        "turns": [
            {
                "index": 0,
                "request": {"messages": bundle.messages},
                "response": {
                    "assistant_messages": replies,
                    "tool_call_indices": list(range(len(calls))),
                },
            }
        ],
        "result": result_of(detections, bundle.manifest["claim_level"]),
        "observation": {"source": source, "assistant_messages": replies, "tool_calls": calls},
        "runtime_observations": {"source": None, **{name: [] for name in RUNTIME_LISTS}},
        "detections": detections,
        "judge": judge_of(attempt, records),
    }


def sequential_attempt(
    report_file: Path, bundle: Bundle, records: Records
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the receipt attempt of a sequential report and why it failed, if it did.

    Only a conversation whose every turn finished is graded: it carries the
    final turn's detections and verdict.
    """

    turns, reason = finished_turns(report_file)
    if turns is None:
        return None, reason
    reason, error_type = turn_failure(turns, len(bundle.messages))
    graded = reason is None
    detections = detections_of(turns[-1]) if graded else []
    source, calls = sequential_calls(turns, records, len(bundle.messages))
    replies = turn_replies(turns)
    status = _text(output_notes(turns[-1]).get("response_status"))
    attempt = {
        "index": 0,
        "generation": {
            "request_count": records.request_count,
            "response_status": status if graded else None,
            "error_type": error_type,
        },
        "turns": receipt_turns(bundle.messages, replies, calls),
        "result": result_of(detections, bundle.manifest["claim_level"]),
        "observation": {
            "source": source,
            "assistant_messages": [replies[index] for index in sorted(replies)],
            "tool_calls": calls,
        },
        "runtime_observations": {"source": None, **{name: [] for name in RUNTIME_LISTS}},
        "detections": detections,
        "judge": sequential_judge(turns, records) if graded else None,
    }
    return attempt, reason


def turn_replies(turns: list[dict[str, Any]]) -> dict[int, str]:
    """Map each turn index to the reply text its output holds."""

    texts = {_sequence(turn)["turn_index"]: single_output(turn).get("text") for turn in turns}
    return {index: text for index, text in texts.items() if isinstance(text, str)}


def receipt_turns(
    messages: list[dict[str, str]], replies: dict[int, str], calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return one receipt turn per planned user message: its request, reply, and calls."""

    return [
        {
            "index": index,
            "request": {"messages": [message]},
            "response": {
                "assistant_messages": [replies[index]] if index in replies else [],
                "tool_call_indices": [
                    position for position, call in enumerate(calls) if call["turn_index"] == index
                ],
            },
        }
        for index, message in enumerate(messages)
    ]


def finished_turns(path: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Return the report's finished per-turn attempts in turn order, or why not."""

    entries, reason = read_entries(path)
    if entries is None:
        return None, reason
    turns = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("entry_type") == "attempt"
        and entry.get("status") == ATTEMPT_COMPLETE
        and type(_sequence(entry).get("turn_index")) is int
        and single_output(entry) is not None
    ]
    if not turns:
        return None, "no_finished_attempt"
    return sorted(turns, key=lambda turn: _sequence(turn)["turn_index"]), None


def turn_failure(turns: list[dict[str, Any]], planned: int) -> tuple[str | None, str | None]:
    """Return ``(incomplete_reason, error_type)`` for the first turn that failed."""

    for turn in turns:
        failure = _text(_sequence(turn).get("failure"))
        if failure is not None:
            position = _sequence(turn)["turn_index"] + 1
            return f"turn_failed:{position}", f"generation_failed:{failure}"
    if len(turns) < planned:
        return f"turn_failed:{len(turns) + 1}", None
    return None, None


def sequential_calls(
    turns: list[dict[str, Any]], records: Records, planned: int
) -> tuple[str, list[dict[str, Any]]]:
    """Return each call once, with the turn that made it.

    A turn's ``tool_calls`` note holds every call so far; its own calls start
    at ``call_offset``. A recorded proxy call without a turn goes to the last.
    """

    if records.calls is not None:
        calls = [dict(call) for call in records.calls]
        for call in calls:
            if call["turn_index"] is None:
                call["turn_index"] = planned - 1
        return "mcp_recording_proxy", calls
    return "tool_native", [call for turn in turns for call in turn_calls(turn)]


def turn_calls(turn: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the calls one turn made, from ``call_offset`` on in its ``tool_calls`` note."""

    sequence = _sequence(turn)
    native = output_notes(turn).get("tool_calls")
    native = native if isinstance(native, list) else []
    offset = _count(sequence.get("call_offset")) or 0
    return [
        {**_native_call(call), "turn_index": sequence["turn_index"]}
        for call in native[offset:]
        if isinstance(call, dict)
    ]


def sequential_judge(turns: list[dict[str, Any]], records: Records) -> dict[str, Any] | None:
    """Return the final turn's verdict and every judge request of the conversation."""

    final = judge_of(turns[-1], records)
    if final is None or records.judge_count is not None:
        return final
    judged = [record for turn in turns for record in _records(turn, "RubricJudge")]
    dispatched = sum(1 for record in judged if record.get("request") is not None)
    return {**final, "request_count": dispatched}


def observed_calls(notes: dict[str, Any], records: Records) -> tuple[str, list[dict[str, Any]]]:
    if records.calls is not None:
        return "mcp_recording_proxy", [{**call, "turn_index": 0} for call in records.calls]
    native = notes.get("tool_calls")
    native = native if isinstance(native, list) else []
    return "tool_native", [_native_call(call) for call in native if isinstance(call, dict)]


def detections_of(attempt: dict[str, Any]) -> list[dict[str, Any]]:
    results = attempt.get("detector_results")
    if not isinstance(results, dict):
        return []
    return [_detection(attempt, plugin, scores) for plugin, scores in results.items()]


def _detection(attempt: dict[str, Any], plugin: str, scores: Any) -> dict[str, Any]:
    name = plugin.rsplit(".", 1)[-1]
    score = scores[0] if isinstance(scores, list) and scores else None
    record = _first_record(attempt, name)
    matched = record.get("matched_calls")
    return {
        "detector": name,
        "outcome": OUTCOMES.get(score, INCONCLUSIVE),
        "reason": _text(record.get("reason")),
        "matched_calls": matched if isinstance(matched, list) else None,
    }


def result_of(detections: list[dict[str, Any]], claim_level: str) -> dict[str, Any]:
    """Return the driver detection's outcome, as orch's ``rich_result`` does."""

    driver = DRIVERS.get(claim_level)
    found = next((item for item in detections if item["detector"] == driver), None)
    if found is None:
        return {
            "outcome": None,
            "claim_level": claim_level,
            "reason": f"detection_missing:{driver}",
        }
    return {"outcome": found["outcome"], "claim_level": claim_level, "reason": found["reason"]}


def judge_of(attempt: dict[str, Any], records: Records) -> dict[str, Any] | None:
    judged = _records(attempt, "RubricJudge")
    if not judged:
        return None
    first = judged[0]
    dispatched = sum(1 for record in judged if record.get("request") is not None)
    return {
        "verdict": _text(first.get("verdict")),
        "reason": _text(first.get("reason")),
        "request_count": records.judge_count if records.judge_count is not None else dispatched,
    }


def bundle_digest(root: Path) -> str:
    files = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in (BUNDLE_FILE, CONVERSATIONS, RUN_CONFIG)
        if (root / name).is_file()
    }
    return hashlib.sha256(canonical_text(files).encode("utf-8")).hexdigest()


def native_files(report_file: Path, receipt_dir: Path) -> list[dict[str, str]]:
    if not report_file.is_file():
        return []
    try:
        relative = report_file.resolve().relative_to(receipt_dir.resolve())
    except ValueError as exc:
        raise ParseError("the report must lie inside the receipt's directory") from exc
    digest = hashlib.sha256(report_file.read_bytes()).hexdigest()
    return [{"path": relative.as_posix(), "sha256": digest}]


def _sequence(attempt: dict[str, Any]) -> dict[str, Any]:
    notes = attempt.get("notes")
    sequence = notes.get("sequence") if isinstance(notes, dict) else None
    return sequence if isinstance(sequence, dict) else {}


def _records(attempt: dict[str, Any], detector: str) -> list[dict[str, Any]]:
    notes = attempt.get("notes")
    found = notes.get(RECORD_NOTES[detector]) if isinstance(notes, dict) else None
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


def _first_record(attempt: dict[str, Any], detector: str) -> dict[str, Any]:
    if detector not in RECORD_NOTES:
        return {}
    found = _records(attempt, detector)
    return found[0] if found else {}


def _native_call(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": _text(call.get("name")),
        "arguments": _decoded(call.get("arguments")),
        "result": _decoded(call.get("output")),
        "error": _text(call.get("error")),
        "status": _text(call.get("status")),
        "turn_index": 0,
    }


def _proxy_call(line: str) -> dict[str, Any]:
    try:
        call = json.loads(line)
    except json.JSONDecodeError:
        call = {}
    call = call if isinstance(call, dict) else {}
    fields = {key: call.get(key) for key in CALL_FIELDS}
    fields["result"], decoded_error = _decoded_result(fields["result"])
    fields["error"] = _error_text(fields["error"]) or decoded_error
    for key in ("name", "status"):
        fields[key] = _text(fields[key])
    # Single-turn receipts put every call in turn 0; sequential ones keep a
    # recorded turn (see ``sequential_calls``).
    return {**fields, "turn_index": _count(call.get("turn_index"))}


def _envelope_texts(result: Any) -> list[str] | None:
    """Return the text blocks of an MCP tool result, or None for any other value."""

    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return None
    return [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]


def _decoded_result(result: Any) -> tuple[Any, str | None]:
    """Return the ``(result, error)`` the proxy's recorded tool result stands for.

    The proxy stores the MCP envelope the tool answered with; the receipt
    carries what the tool returned: the text blocks joined by a newline and
    parsed as JSON when they parse, else kept as text. A result flagged
    ``isError`` is a failed call: no result, and the text as the error. Any
    other value, or a successful envelope with no text block, stays as
    recorded; ``structuredContent`` is never read.
    """

    texts = _envelope_texts(result)
    if texts is None:
        return result, None
    if result.get("isError") is True:
        return None, "\n".join(texts) or "isError"
    if not texts:
        return result, None
    joined = "\n".join(texts)
    try:
        return json.loads(joined), None
    except json.JSONDecodeError:
        return joined, None


def _error_text(error: Any) -> str | None:
    """Return a JSON-RPC error's message, or the error object as compact JSON.

    A call that failed at the protocol level did not run, so the receipt says
    so even when the error carries no message.
    """

    if not isinstance(error, dict):
        return _text(error)
    return _text(error.get("message")) or json.dumps(
        error, separators=(",", ":"), ensure_ascii=False
    )


def _decoded(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None
