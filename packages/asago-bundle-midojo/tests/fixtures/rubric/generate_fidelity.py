"""Compare the string MiDojo's sink stores for a tool result with Garak's evidence value.

usage: <garak venv python> generate_fidelity.py RUNS_DIR SINK_MODULE OUT.json

``RUNS_DIR`` is orch's ``runs/`` directory and ``SINK_MODULE`` the path of
``midojo_sink.py`` in an orch checkout. Run it with the Garak interpreter (orch's
``.runtime/garak/venv``), read-only. It writes two groups of cases, each holding the
MCP result the recording proxy saw, the body the sink's ``call_body`` makes of it
(executed from that source), and the value Garak's judge evidence holds for the same call:

``real``       pairs of a saved proxy record with the call Garak captured for the same
               exchange in the saved dry-run executes (``runs/run-dryrun-*``); the
               captured call is Garak's own record, so the pairing needs no assumption;
``synthetic``  results of shapes the saved runs do not hold. Garak's capture of a call
               is modelled as the Responses ``mcp_call`` item whose ``output`` is the
               result's text, or whose ``error`` is set and ``output`` absent for a failed
               call. A result with several blocks, a non-text block, or no text block has
               no modelled capture: ``garak`` is null for those.

The saved runs are read, not changed; no score or gold file is opened and no request is
made. pytest does not collect this file.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

from garak.detectors.toolcall import normalise_tool_call


def sink_call_body(sink: Path) -> Any:
    tree = ast.parse(sink.read_text(encoding="utf-8"))
    wanted = {"_text", "_error", "call_body"}
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    scope: dict[str, Any] = {"json": json, "Any": object}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(sink), "exec"), scope)
    return scope["call_body"]


CALL_BODY: Any = None


def sink_of(name: str, arguments: Any, result: Any, error: Any = None) -> dict[str, Any]:
    body = CALL_BODY({"name": name, "arguments": arguments, "result": result, "error": error})
    return {key: body[key] for key in ("function", "args", "result", "error")}


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def captured_calls(report: Path) -> list[dict[str, Any]]:
    found = []
    for line in report.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("entry_type") != "attempt" or entry.get("status") != 2:
            continue
        for output in entry.get("outputs", []):
            calls = (output.get("notes") or {}).get("tool_calls")
            if isinstance(calls, list):
                found.extend(calls)
    return found


def real_cases(runs: Path) -> tuple[list[dict[str, Any]], int]:
    cases: dict[str, dict[str, Any]] = {}
    skipped = 0
    for calls_file in sorted(
        runs.glob("run-dryrun-*/stages/execute/output/*/mcp_capture/calls.jsonl")
    ):
        scenario = calls_file.parents[1]
        reports = sorted(scenario.glob("generation_capture/bundle/reports/*.report.jsonl"))
        records = [json.loads(x) for x in calls_file.read_text().splitlines() if x.strip()]
        captured = [call for report in reports for call in captured_calls(report)]
        if not records or len(records) != len(captured):
            skipped += 1
            continue
        for record, call in zip(records, captured, strict=True):
            case = {
                "mcp_result": record.get("result"),
                "mcp_error": record.get("error"),
                "name": record.get("name"),
                "arguments": record.get("arguments"),
                "garak_call": call,
                "sink": sink_of(
                    record.get("name"),
                    record.get("arguments"),
                    record.get("result"),
                    record.get("error"),
                ),
                "garak": normalise_tool_call(call),
            }
            cases[json.dumps(case, sort_keys=True)] = case
    ordered = sorted(cases.values(), key=lambda case: json.dumps(case, sort_keys=True))
    return [{"id": f"real-{index:02d}", **case} for index, case in enumerate(ordered)], skipped


def modelled_capture(name: str, arguments: Any, result: Any) -> dict[str, Any] | None:
    """Garak's capture of a call, for a result of one text block or an error flag."""

    content = result.get("content") if isinstance(result, dict) else None
    blocks = content if isinstance(content, list) else []
    texts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    if len(blocks) != 1 or len(texts) != 1:
        return None
    item = {"type": "mcp_call", "name": name, "arguments": json.dumps(arguments)}
    if result.get("isError") is True:
        return {**item, "error": "Error (code 1): None"}
    return {**item, "output": texts[0]}


SYNTHETIC: list[tuple[str, Any]] = [
    ("json-object", {"content": [text_block('{"referral": "cardiology"}')]}),
    ("json-list", {"content": [text_block('[{"id": 1}, {"id": 2}]')]}),
    ("json-scalar", {"content": [text_block("42")]}),
    ("json-string", {"content": [text_block('"quoted"')]}),
    ("json-null", {"content": [text_block("null")]}),
    ("json-bool", {"content": [text_block("true")]}),
    ("json-padded", {"content": [text_block('  {"a": 1}\n')]}),
    ("json-unicode", {"content": [text_block('{"fee": "5 \\u20ac", "note": "caf\u00e9"}')]}),
    ("json-large-number", {"content": [text_block('{"n": 12345678901234567890, "f": 1.50}')]}),
    ("plain-text", {"content": [text_block("plain words, not JSON")]}),
    ("empty-text", {"content": [text_block("")]}),
    ("not-quite-json", {"content": [text_block("{'a': 1}")]}),
    ("error-plain", {"content": [text_block("boom")], "isError": True}),
    (
        "error-json-object",
        {"content": [text_block('{"error": "no such patient"}')], "isError": True},
    ),
    ("error-json-scalar", {"content": [text_block("404")], "isError": True}),
    (
        "with-structured-content",
        {"content": [text_block('{"a": 1}')], "structuredContent": {"a": 2}},
    ),
    ("two-text-blocks-json", {"content": [text_block('{"a": 1}'), text_block('{"b": 2}')]}),
    ("two-text-blocks-split-json", {"content": [text_block('{"a":'), text_block("1}")]}),
    ("image-block", {"content": [{"type": "image", "data": "AAAA", "mimeType": "image/png"}]}),
    ("text-and-image", {"content": [text_block('{"a": 1}'), {"type": "image", "data": "AA"}]}),
    ("no-content", {"content": []}),
    ("structured-only", {"structuredContent": {"a": 1}}),
    ("not-a-mapping", "raw text result"),
    ("null-result", None),
]


def synthetic_cases() -> list[dict[str, Any]]:
    cases = []
    for name, result in SYNTHETIC:
        arguments = {"patient_id": "PAT-201"}
        call = modelled_capture("tool", arguments, result)
        cases.append(
            {
                "id": f"synthetic-{name}",
                "mcp_result": result,
                "mcp_error": None,
                "name": "tool",
                "arguments": arguments,
                "garak_call": call,
                "sink": sink_of("tool", arguments, result),
                "garak": normalise_tool_call(call) if call else None,
            }
        )
    return cases


def main(runs: Path, sink: Path, out: Path) -> None:
    global CALL_BODY
    CALL_BODY = sink_call_body(sink)
    real, skipped = real_cases(runs)
    document = {
        "version": 1,
        "garak_function": "garak.detectors.toolcall.normalise_tool_call",
        "sink": {
            "repository": "asago-orch",
            "branch": "w5/d5-o-sink",
            "module": "asago_orch/qualification/boundary/midojo_sink.py",
            "function": "call_body",
        },
        "real_source": (
            "saved dry-run executes (runs/run-dryrun-*), proxy records paired with Garak captures"
        ),
        "real_skipped_outputs": skipped,
        "real": real,
        "synthetic": synthetic_cases(),
    }
    out.write_text(json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
