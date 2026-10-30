"""The planted packages, and the reports an ``IPIEpisode`` run writes, for the Garak tests.

The packages come from the MiDojo adapter's own builders
(``packages/asago-bundle-midojo/tests/episode_cases.py``): both adapters compile the
same package for the same fixture target, and the entry Garak writes must carry the
episode MiDojo compiled. The reports follow what ``garak/probes/ipi_episode.py`` and
``garak/detectors/ipi_episode.py`` write: each turn's attempt at status 1, the turn
that ends the chain again at status 2 with the detectors' results, and the sealed
trace on every attempt of a sealed chain.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from conftest import REPO

MIDOJO_TESTS = REPO / "packages" / "asago-bundle-midojo" / "tests"
if str(MIDOJO_TESTS) not in sys.path:
    sys.path.insert(0, str(MIDOJO_TESTS))

from episode_cases import (  # noqa: E402, F401  the MiDojo adapter's builders, by path
    CONDITION,
    JUDGE,
    PIGGYBANK,
    PLANTED,
    SEQUENTIAL_TURNS,
    SINGLE_TURNS,
    SLOTS,
    TARGET,
    planted_stimulus,
    seed_document,
    seed_item,
    write_planted_package,
)

# The probe's class name, as the report's ``probe_classname`` writes it.
PROBE = "probes.ipi_episode.IPIEpisode"
VERDICT = "ipi_episode.EpisodeVerdict"
CONDITION_DETECTOR = "toolcall.ToolCallCondition"
SCORES = {"detected": 1.0, "not_detected": 0.0, "inconclusive": None}
EVALUATION_ID = "evaluation-6f1c"
SNAPSHOT_ID = "a" * 64
ACCOUNT = "piggybank"
METADATA = {
    "package_id": "SCN-051-SCN-051",
    "scenario_id": "SCN-051",
    "package_digest": "b" * 64,
    "claim_level": "command_attempt",
    "delivery": "single",
}


def grade(*, security: Any, validity: str = "valid", delivered: Any = True) -> dict[str, Any]:
    """MiDojo's grade of one episode, as the trace carries it."""

    return {
        "security": security,
        "grade_validity": {"security": validity},
        "injection_delivered": delivered,
        "attempted": True,
        "blocked": False,
        "effect_observed": None,
    }


def trace(
    *,
    calls: tuple[dict[str, Any], ...] = (),
    security: Any = None,
    validity: str = "valid",
    delivered: Any = True,
    capture_complete: bool = True,
    sealed: str = "final",
    error: str | None = None,
) -> dict[str, Any]:
    """The sealed trace the probe puts on a chain's final attempt."""

    return {
        "evaluation_id": EVALUATION_ID,
        "snapshot_id": SNAPSHOT_ID,
        "snapshot": {"evaluation_id": EVALUATION_ID, "snapshot_id": SNAPSHOT_ID},
        "verdict": {
            "evaluation_id": EVALUATION_ID,
            "snapshot_id": SNAPSHOT_ID,
            "grade": grade(security=security, validity=validity, delivered=delivered),
        },
        "capture_complete": capture_complete,
        "sealed": sealed,
        "failure": None,
        "error": error,
        "tool_calls": list(calls),
        # A condition can decide only a delivered payload with a complete capture.
        "response_status": (
            "completed" if capture_complete and delivered is True else "incomplete"
        ),
    }


def call(
    name: str,
    arguments: dict[str, Any],
    result: Any = None,
    *,
    turn: int = 0,
    complete: bool = True,
    call_id: str = "call-1",
    blocked: bool = False,
    disposition: str = "forward",
) -> dict[str, Any]:
    """One call of a sealed trace; ``result`` is the JSON text MiDojo recorded."""

    return {
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "result": None if result is None else json.dumps(result),
        "blocked": blocked,
        "disposition": disposition,
        "complete": complete,
        "turn": turn,
    }


def condition_record(
    outcome: str, *, matched_calls: list[int] | None = None, reason: str | None = None
) -> dict[str, Any]:
    """The ``tool_call_condition_results`` record the condition detector writes."""

    return {
        "outcome": outcome,
        "score": SCORES[outcome],
        "reason": reason,
        "matched_calls": matched_calls,
    }


def verdict_record(
    outcome: str,
    *,
    reason: str | None = None,
    detail: str | None = None,
    security: Any = None,
    validity: str = "valid",
    delivered: Any = True,
    capture_complete: bool = True,
    condition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``episode_verdict_results`` record the verdict detector writes."""

    return {
        "outcome": outcome,
        "score": SCORES.get(outcome),
        "reason": reason,
        "detail": detail,
        "security": security,
        "security_validity": validity,
        "injection_delivered": delivered,
        "capture_complete": capture_complete,
        "condition": condition,
    }


def prompt_turns(texts: tuple[str, ...], replies: tuple[str, ...]) -> list[list[dict[str, Any]]]:
    """Return each attempt's prompt turns: the earlier turns and replies, then its own."""

    messages: list[list[dict[str, Any]]] = []
    for position in range(len(texts)):
        turns = [
            {"role": "user", "content": {"text": text, "lang": "en"}}
            for text in texts[: position + 1]
        ]
        for index in range(position):
            if index < len(replies):
                turns.insert(
                    2 * index + 1,
                    {"role": "assistant", "content": {"text": replies[index], "lang": "en"}},
                )
        messages.append(turns)
    return messages


def _attempt(
    position: int,
    *,
    chain: int,
    turns: int,
    prompt: list[dict[str, Any]],
    status: int,
    reply: str | None,
    final: bool,
    metadata: dict[str, Any] | None,
    sealed: dict[str, Any] | None,
    results: dict[str, list[Any]] | None,
    seq: int,
) -> dict[str, Any]:
    output: dict[str, Any] = {"text": reply or "", "notes": {}}
    if sealed is not None:
        output["notes"] = {
            "tool_calls": sealed["tool_calls"],
            "response_status": sealed["response_status"],
        }
    entry: dict[str, Any] = {
        "entry_type": "attempt",
        "uuid": f"chain-{chain}-turn-{position}",
        "seq": seq,
        "status": status,
        "probe_classname": PROBE,
        "generator": {"name": "openai", "class_name": "OpenAIResponsesGenerator"},
        "prompt": {
            "turns": prompt,
            "notes": {"tools": [{"type": "mcp", "server_label": ACCOUNT}]},
        },
        "outputs": [output],
        "notes": {
            "ipi_episode": {"chain": chain, "turn": position, "turns": turns, "final": final},
            "source_metadata": metadata,
        },
    }
    if sealed is not None:
        entry["notes"]["episode_trace"] = sealed
    if results is not None:
        entry["detector_results"] = results
    return entry


def chain(
    *,
    texts: tuple[str, ...] = ("Read me my notes.",),
    replies: tuple[str, ...] | None = None,
    sealed: dict[str, Any] | None = None,
    records: tuple[dict[str, Any], ...] = (),
    metadata: dict[str, Any] | None = METADATA,
    chain_index: int = 0,
    seq: int = 0,
) -> list[dict[str, Any]]:
    """Return one chain's report lines: every turn at status 1, the last again at status 2.

    ``sealed`` is the trace the probe writes when it seals the chain; without one the
    chain ends at its last status-1 attempt, as a run killed before the seal leaves it.
    ``records`` are the verdict records the detectors write; they go on the status-2
    attempt, which is the only one Garak evaluates.
    """

    said = replies if replies is not None else ("Done.",) * len(texts)
    messages = prompt_turns(texts, said)
    lines = [
        _attempt(
            position,
            chain=chain_index,
            turns=len(texts),
            prompt=messages[position],
            status=1,
            reply=said[position] if position < len(said) else None,
            final=sealed is not None and position == len(texts) - 1,
            metadata=metadata,
            sealed=sealed if position == len(texts) - 1 else None,
            results=None,
            seq=seq + position,
        )
        for position in range(len(texts))
    ]
    if sealed is None:
        return lines
    last = lines[-1]
    finished = dict(last)
    finished["status"] = 2
    finished["seq"] = seq + len(texts)
    notes = dict(finished["notes"])
    if records:
        finished["detector_results"] = {
            VERDICT: [records[0].get("score")],
            CONDITION_DETECTOR: [records[1].get("score")] if len(records) > 1 else [],
        }
        notes["episode_verdict_results"] = [records[0]]
        notes["tool_call_condition_results"] = list(records[1:])
    finished["notes"] = notes
    return [*lines, finished]


def write_report(path: Path, entries: list[dict[str, Any]]) -> Path:
    """Write report lines as ``report.jsonl`` and return the file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries),
        encoding="utf-8",
    )
    return path
