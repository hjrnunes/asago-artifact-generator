"""Compute what Garak's judge sees on a sequential reply conversation, with Garak's own code.

usage: uv run python generate_turns.py cases OUT.json
       <garak venv python> generate_turns.py judge GARAK_CHECKOUT_REVISION OUT.json

Two steps fill one fixture. ``cases`` runs in the consumer's interpreter: for a two-turn
and a three-turn reply package it writes the conversation entry the Garak adapter's
``compile`` and ``instantiate`` give (``conversations.jsonl``), the replies and tool calls
the target is scripted to give on each turn, and the same conversation as MiDojo's control
plane hands a verifier (``turns`` and ``function_calls``, each call with its ``turn``; a
call's ``result`` is its one text block, the sink's rule for such a result).

``judge`` runs in the orchestrator's Garak interpreter, read-only, and adds Garak's side:
for ``judge_turns`` ``final`` and ``all``, ``injection.SequentialInjection`` runs the entry
against a scripted target, and ``rubric_judge.RubricJudge.detect`` judges every attempt,
its generator's ``create`` replaced by a recorder (``generate_vectors.make_judge``) that
answers ``supported``. It records each judge request in turn order, each attempt's record,
and the request count the Garak adapter's parse gives the conversation (every dispatched
request of every turn). No request leaves the process.

pytest does not collect this file (its name has no ``test_`` prefix).
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

TURN_TEXTS = ["Hello.", "Show PAT-201.", "Now PAT-104."]
VALUES: dict[str, Any] = {
    "gateway_url": "http://127.0.0.1:18997/v1",
    "mcp_url": "http://127.0.0.1:18996/sse",
    "allowed_tools": ["lookup_order", "get_referral"],
    "model": "fixture-model",
    "judge_url": "http://127.0.0.1:18998/v1",
    "judge_model": "fixture-judge",
    "judge_runtime_facts": {"state": {"actor": "GST001"}},
}
JUDGE_TURNS = ("final", "all")
CANNED = json.dumps({"verdict": "supported", "evidence_refs": ["messages[0].content"]})


def tool_call(name: str, arguments: dict[str, Any], text: str, call_id: str) -> dict[str, Any]:
    """One tool call: its Responses ``mcp_call`` item and its one text block."""

    return {"name": name, "arguments": arguments, "text": text, "id": call_id}


SCRIPTS = {
    "two-turn": [
        {
            "text": "Hi, which order?",
            "calls": [tool_call("lookup_order", {"order": "ORD-101"}, '{"order": 1}', "fc_1")],
        },
        {
            "text": "PAT-201 is referred to cardiology.",
            "calls": [
                tool_call(
                    "get_referral", {"patient_id": "PAT-201"}, '{"referral": "cardiology"}', "fc_2"
                )
            ],
        },
    ],
    "three-turn": [
        {"text": "Hello, how can I help?", "calls": []},
        {
            "text": "PAT-201 is referred to cardiology.",
            "calls": [
                tool_call(
                    "get_referral", {"patient_id": "PAT-201"}, '{"referral": "cardiology"}', "fc_1"
                )
            ],
        },
        {
            "text": "PAT-104 is referred to oncology.",
            "calls": [
                tool_call("lookup_order", {"order": "ORD-104"}, "not JSON", "fc_2"),
                tool_call(
                    "get_referral", {"patient_id": "PAT-104"}, '{"referral": "oncology"}', "fc_3"
                ),
            ],
        },
    ],
}


def stimulus(texts: list[str]) -> dict[str, Any]:
    return {
        "delivery": "direct",
        "mode": "sequential",
        "turn_count": len(texts),
        "history": [{"role": "user", "content": text} for text in texts[:-1]],
        "slots": [],
        "user_text": texts[-1],
    }


def turn_values(texts: list[str]) -> dict[str, Any]:
    return {f"user_turn_{number}": text for number, text in enumerate(texts, start=1)}


def garak_entry(texts: list[str]) -> dict[str, Any]:
    """The conversation entry the Garak adapter writes for a reply package of ``texts``."""

    from asago_bundle_core.testing import MOCK_TARGET, write_test_package
    from asago_bundle_garak.compiler import compile_package
    from asago_bundle_garak.instantiate import instantiate_bundle

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        package = write_test_package(
            root / "packages", claim_level="reply", stimulus=stimulus(texts)
        )
        compile_package(package, root / "template")
        values = {**VALUES, **turn_values(texts), "judge_turns": "final"}
        instantiate_bundle(root / "template", values, root / "bundle", target=MOCK_TARGET)
        return json.loads((root / "bundle/conversations.jsonl").read_text(encoding="utf-8"))


def midojo_inputs(texts: list[str], script: list[dict[str, Any]]) -> dict[str, Any]:
    """The conversation and calls MiDojo's control plane hands the verifier."""

    return {
        "turns": [
            {"message": text, "reply": turn["text"]}
            for text, turn in zip(texts, script, strict=True)
        ],
        "function_calls": [
            {
                "function": call["name"],
                "args": call["arguments"],
                "result": call["text"],
                "error": None,
                "turn": index,
            }
            for index, turn in enumerate(script)
            for call in turn["calls"]
        ],
    }


def cases(out: Path) -> None:
    found = []
    for name, script in SCRIPTS.items():
        texts = TURN_TEXTS[: len(script)]
        found.append(
            {
                "id": name,
                "user_turns": texts,
                "values": VALUES,
                "garak_entry": garak_entry(texts),
                "script": script,
                "midojo": midojo_inputs(texts, script),
            }
        )
    document = {
        "contract": "rubric-judge-turns (no producer contract: computed from Garak)",
        "cases": found,
    }
    out.write_text(json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


# --- the Garak side ---------------------------------------------------------------------


class ScriptedTarget:
    """Answer each request with the next scripted reply, as Garak's own probe tests do."""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)

    def generate(self, prompt: Any, generations_this_call: int = 1) -> list[Any]:
        return [self.replies.pop(0)]

    def clear_history(self) -> None:
        pass


def scripted_reply(turn: dict[str, Any]) -> Any:
    from garak.attempt import Message

    items = [
        {
            "type": "mcp_call",
            "id": call["id"],
            "name": call["name"],
            "arguments": json.dumps(call["arguments"]),
            "output": call["text"],
            "server_label": "qualification_target",
            "error": None,
        }
        for call in turn["calls"]
    ]
    notes: dict[str, Any] = {"response_status": "completed"}
    if items:
        notes["tool_calls"] = items
        notes["output_items"] = copy.deepcopy(items)
    return Message(turn["text"], notes=notes)


def garak_judgements(case: dict[str, Any], judge_turns: str, scratch: Path) -> dict[str, Any]:
    from garak.probes.injection import SequentialInjection
    from generate_vectors import Recorder, make_judge

    source = scratch / f"{case['id']}-{judge_turns}.json"
    source.write_text(json.dumps([case["garak_entry"]]), encoding="utf-8")
    config_root = {
        "probes": {
            "injection": {
                "SequentialInjection": {
                    "conversation_source": str(source),
                    "judge_turns": judge_turns,
                }
            }
        }
    }
    probe = SequentialInjection(config_root=config_root)
    attempts = probe.probe(ScriptedTarget([scripted_reply(t) for t in case["script"]]))
    recorder = Recorder()
    recorder.canned = CANNED
    judge = make_judge(recorder)
    attempts = sorted(attempts, key=lambda a: a.notes["sequence"]["turn_index"])
    records = []
    for attempt in attempts:
        judge.detect(attempt)
        records.append(attempt.notes.get("rubric_judge_results", []))
    judged = [record for turn in records for record in turn]
    return {
        "requests": [record["request"] for record in judged if record["request"] is not None],
        "records": records,
        "final": records[-1][0],
        "request_count": sum(1 for record in judged if record["request"] is not None),
        "http_bodies": recorder.calls,
    }


def judge(revision: str, out: Path) -> None:
    from garak import _config

    document = json.loads(out.read_text(encoding="utf-8"))
    _config.load_base_config()
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        with open(root / "probe.report.jsonl", "w+", encoding="utf-8") as report:
            _config.transient.reportfile = report
            _config.transient.report_filename = str(root / "probe.report.jsonl")
            for case in document["cases"]:
                case["garak"] = {mode: garak_judgements(case, mode, root) for mode in JUDGE_TURNS}
    document["implementation"] = {
        "repository": "garak",
        "revision": revision,
        "probe": "garak/probes/injection.py SequentialInjection",
        "detector": "garak/detectors/rubric_judge.py RubricJudge.detect",
    }
    out.write_text(json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    if sys.argv[1] == "cases":
        cases(Path(sys.argv[2]))
    else:
        sys.path.insert(0, str(Path(__file__).parent))
        judge(sys.argv[2], Path(sys.argv[3]))
