"""What the orchestrator's boundary services recorded for the package, read for ``parse``.

``mcp_capture/calls.jsonl`` holds the tool calls the MCP recording proxy saw and
``boundary/gateway-accounting.json`` the generation request count;
``boundary/judge-exchanges.jsonl`` holds one line per request the boundary judge
served (a reply package's rubric judge). With the
MiDojo call sink on, each recorded call also says whether the sink delivered it
to the control plane (``sink: ok|failed|no_token``). A recorded call's
``result`` is the MCP envelope; the receipt carries the tool's decoded answer.

``midojo_capture/calls.jsonl`` is the mini-agents server's own log (``--midojo-log``,
mini-agents ``kernel/midojo_mode.py``): one line per call with ``call_id``, ``function``
and the outcome of ``load``, ``start``, ``push`` and ``report``. ``report`` is
``unfinished`` for a call that started in MiDojo and never reached its report. A log
written before MiDojo started calls has no ``call_id`` or ``start``.

An ``openshell`` run has no proxy, gateway or server outside the sandbox. MiDojo's
``observe()`` carries every file created in ``/sandbox/workdir`` out in
``observations["openshell"]["workdir_new_file_contents"]``, keyed by absolute path, and
the mini-agents agent command writes its evidence under ``.asago/`` there (mini-agents
``sandbox_agent/summary.py``): ``mcp-calls.jsonl``, the server's log as above, which the
server creates at its first call; ``ogx.log``, OGX's output, whose ``POST
/v1/responses`` access lines are counted as orch's gateway accounting counts them; and
``run-summary.json`` (``mini-agents-run-summary-v1``). ``workdir_records`` reads them in
place of the boundary's calls and gateway count; the summary is self-reported, so its
request count must equal the log's, and a run it lists as ``rejected`` fails the receipt.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from asago_bundle_core.mcp_records import (
    count_value,
    decoded,
    decoded_result,
    error_text,
    lines,
    read_json,
    text,
)
from asago_bundle_core.text import jsonl_lines

PROXY_CALLS = Path("mcp_capture/calls.jsonl")
SERVER_CALLS = Path("midojo_capture/calls.jsonl")
GATEWAY_ACCOUNTING = Path("boundary/gateway-accounting.json")
JUDGE_EXCHANGES = Path("boundary/judge-exchanges.jsonl")
CALL_FIELDS = ("name", "arguments", "result", "error", "status")
SINK_DELIVERED = "ok"
LOAD_FAILURES = ("failed", "no_token")
UNFINISHED = "unfinished"
OPENSHELL_SOURCE = "openshell"
EVIDENCE_DIR = "/sandbox/workdir/.asago"
SERVER_LOG = "mcp-calls.jsonl"
OGX_LOG = "ogx.log"
RUN_SUMMARY = "run-summary.json"
SUMMARY_SCHEMA = "mini-agents-run-summary-v1"
# The arrays of MiDojo's openshell observations that ``execution-receipt-v1`` carries.
RUNTIME_LISTS = (
    "processes",
    "network_calls_allowed",
    "network_calls_blocked",
    "files_created",
    "files_modified",
    "files_deleted",
    "security_findings",
)
# orch ``boundary/gateway_accounting.py``: an access line and a Responses path.
ACCESS_LINE = re.compile(r'"(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[0-9.]+" (?P<status>\d{3})\b')
RESPONSES_PATH = re.compile(r"^/v1/responses/?(?:\?.*)?$")


@dataclass(frozen=True)
class Records:
    """The boundary records of one package; ``calls`` is None when none were recorded.

    ``arrivals`` holds each proxy call's ``sequence``: the proxy numbers the calls in the
    order their requests arrive. Only a sequential receipt reads it.
    """

    calls: list[dict[str, Any]] | None = None
    request_count: int | None = None
    judge_count: int | None = None
    malformed: bool = False
    undelivered: bool = False
    server: list[dict[str, Any]] | None = None
    arrivals: list[Any] | None = None
    # Why an openshell run's workdir evidence cannot be used; None when it can, or when
    # the records are the boundary's.
    failure: str | None = None


def read_records(directory: Path) -> Records:
    accounting = read_json(directory / GATEWAY_ACCOUNTING)
    count = accounting.get("responses_request_count") if isinstance(accounting, dict) else None
    exchanges = directory / JUDGE_EXCHANGES
    counts: dict[str, Any] = {
        "request_count": count_value(count),
        "judge_count": len(lines(exchanges)) if exchanges.is_file() else None,
    }
    server_file = directory / SERVER_CALLS
    if server_file.is_file():
        server = [_raw_call(line) for line in lines(server_file)]
        if not all(isinstance(line, dict) for line in server):
            return Records(malformed=True, **counts)
        counts["server"] = server
        counts["undelivered"] = any(_server_failure(line) for line in server)
    calls_file = directory / PROXY_CALLS
    if not calls_file.is_file():
        return Records(**counts)
    raw = [_raw_call(line) for line in lines(calls_file)]
    if not all(isinstance(call, dict) for call in raw):
        return Records(**{**counts, "calls": [], "malformed": True})
    return Records(
        **{
            **counts,
            "calls": [proxy_call(call) for call in raw],
            "arrivals": [call.get("sequence") for call in raw],
            "undelivered": counts.get("undelivered", False) or _undelivered(raw),
        }
    )


def workdir_records(evaluation: dict[str, Any], boundary: Records) -> Records:
    """Return an openshell run's records: the server log and request count from the workdir.

    Only the judge's count comes from the boundary. ``failure`` names the first reason
    the evidence cannot be used: ``openshell_observations_missing`` or ``_malformed``,
    ``workdir_file_missing:<file>`` (``mcp-calls.jsonl`` only when MiDojo recorded a
    call), ``run_summary_malformed``, ``server_log_malformed``,
    ``request_count_mismatch`` (the summary's count is not the log's) or
    ``agent_run_rejected``.
    """

    records = Records(judge_count=boundary.judge_count)
    observed = openshell_observations(evaluation)
    if observed is None:
        return replace(records, failure="openshell_observations_missing")
    if not well_formed_observations(observed):
        return replace(records, failure="openshell_observations_malformed")
    files = observed["workdir_new_file_contents"]
    contents = {name: files.get(f"{EVIDENCE_DIR}/{name}") for name in (RUN_SUMMARY, OGX_LOG)}
    for name, content in contents.items():
        if content is None:
            return replace(records, failure=f"workdir_file_missing:{name}")
    log = files.get(f"{EVIDENCE_DIR}/{SERVER_LOG}")
    calls = evaluation.get("function_calls")
    if log is None and isinstance(calls, list) and calls:
        return replace(records, failure=f"workdir_file_missing:{SERVER_LOG}")
    summary = _raw_call(contents[RUN_SUMMARY])
    if not _well_formed_summary(summary):
        return replace(records, failure="run_summary_malformed")
    server = [_raw_call(line) for line in jsonl_lines(log or "")]
    if not all(isinstance(line, dict) for line in server):
        return replace(records, failure="server_log_malformed")
    count = ogx_request_count(contents[OGX_LOG])
    found = replace(
        records,
        server=server,
        undelivered=any(_server_failure(line) for line in server),
        request_count=count,
    )
    if summary["gateway_request_count"] != count:
        return replace(found, failure="request_count_mismatch")
    if summary["rejected"]:
        return replace(found, failure="agent_run_rejected")
    return found


def openshell_observations(evaluation: Any) -> dict[str, Any] | None:
    """Return the evaluation's ``observations["openshell"]``, or None."""

    observations = evaluation.get("observations") if isinstance(evaluation, dict) else None
    observed = observations.get(OPENSHELL_SOURCE) if isinstance(observations, dict) else None
    return observed if isinstance(observed, dict) else None


def well_formed_observations(observed: dict[str, Any]) -> bool:
    files = observed.get("workdir_new_file_contents")
    return (
        all(isinstance(observed.get(name), list) for name in RUNTIME_LISTS)
        and isinstance(files, dict)
        and all(isinstance(content, str) for content in files.values())
    )


def ogx_request_count(log: str) -> int:
    """Count the ``POST /v1/responses`` access lines of OGX's log."""

    return sum(
        1
        for match in ACCESS_LINE.finditer(log)
        if match["method"] == "POST" and RESPONSES_PATH.match(match["path"])
    )


def _well_formed_summary(summary: Any) -> bool:
    return (
        isinstance(summary, dict)
        and summary.get("schema") == SUMMARY_SCHEMA
        and count_value(summary.get("gateway_request_count")) is not None
        and isinstance(summary.get("rejected"), list)
    )


def _server_failure(line: dict[str, Any]) -> bool:
    """Say whether the mini-agents server could not load, start, push or report for one call.

    An unfinished call (``report: unfinished``) is no failure: ``parse`` makes its
    attempt inconclusive.
    """

    return line.get("load") in LOAD_FAILURES or "failed" in (
        line.get("start"),
        line.get("push"),
        line.get("report"),
    )


def server_unfinished(line: dict[str, Any]) -> bool:
    """Say whether the server started one call in MiDojo and never reported it."""

    return line.get("report") == UNFINISHED


def _undelivered(raw: list[dict[str, Any]]) -> bool:
    """Say whether the sink was on and left a call out of the control plane."""

    if not any("sink" in call for call in raw):
        return False
    return any(call.get("sink") != SINK_DELIVERED for call in raw)


def proxy_call(call: dict[str, Any]) -> dict[str, Any]:
    """Return a recorded call in the receipt's shape, in turn 0.

    A sequential receipt replaces the turn with the one MiDojo stamped on the matching
    control-plane call (``parse.sequential_calls``).
    """

    fields = {key: call.get(key) for key in CALL_FIELDS}
    fields["result"], decoded_error = decoded_result(fields["result"])
    fields["error"] = error_text(fields["error"]) or decoded_error
    for key in ("name", "status"):
        fields[key] = text(fields[key])
    return {**fields, "turn_index": 0}


def native_call(call: dict[str, Any]) -> dict[str, Any]:
    """Return a control-plane function call in the receipt's shape, in turn 0.

    A sequential receipt replaces the turn as for ``proxy_call``. An unfinished call
    (``complete: false``) has no result and the status ``unfinished``.
    """

    return {
        "name": text(call.get("function")),
        "arguments": call.get("args"),
        "result": decoded(call.get("result")),
        "error": text(call.get("error")),
        "status": UNFINISHED if is_unfinished(call) else None,
        "turn_index": 0,
    }


def is_unfinished(call: dict[str, Any]) -> bool:
    """Say whether MiDojo holds a control-plane call as started and not finished.

    A record from a MiDojo that predates started calls has no ``complete`` and is finished.
    """

    return call.get("complete") is False


def _raw_call(line: str) -> Any:
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None
