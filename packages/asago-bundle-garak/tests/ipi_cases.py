"""The planted packages, and the reports an ``InjectionSpecProbe`` run writes, for the tests.

The packages come from the MiDojo adapter's own builders
(``packages/asago-bundle-midojo/tests/injection_spec_cases.py``): both adapters compile the
same package for the same fixture target, and the entry Garak writes must carry the
injection spec MiDojo compiled. The reports follow what ``garak/probes/midojo.py`` and
``garak/detectors/midojo.py`` write: each turn's attempt at status 1, the turn that
ends the chain again at status 2 with the detectors' results, and the sealed trace on
every attempt of a sealed chain. ``legacy_report`` turns one into the names a report
recorded before the rename carries, which the parser still reads.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

from conftest import REPO

MIDOJO_TESTS = REPO / "packages" / "asago-bundle-midojo" / "tests"
if str(MIDOJO_TESTS) not in sys.path:
    sys.path.insert(0, str(MIDOJO_TESTS))

from injection_spec_cases import (  # noqa: E402, F401  the MiDojo adapter's builders, by path
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
PROBE = "probes.midojo.InjectionSpecProbe"
VERDICT = "midojo.EvaluationVerdict"
EFFECT = "midojo.EvaluationEffect"
CONDITION_DETECTOR = "toolcall.ToolCallCondition"
# The plugin and note names a report recorded before the rename carries. The probe and
# detectors still answer to them, so a pre-rename report must parse as the same run.
LEGACY_NAMES = {
    PROBE: "probes.ipi_episode.IPIEpisode",
    VERDICT: "ipi_episode.EpisodeVerdict",
    EFFECT: "ipi_episode.EpisodeEffect",
    "injection_spec_chain": "ipi_episode",
    "evaluation_trace": "episode_trace",
    "evaluation_verdict_results": "episode_verdict_results",
    "evaluation_effect_results": "episode_effect_results",
}
SCORES = {"detected": 1.0, "not_detected": 0.0, "inconclusive": None}
# The exposure of an episode whose payload reached no recorded boundary.
UNEXPOSED = {
    "emitted": "unknown",
    "agent_tool_result": "unknown",
    "model_input": "unknown",
    "instructions": [],
}
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


def grade(
    *,
    security: Any,
    validity: str = "valid",
    delivered: Any = True,
    reason: str | None = None,
    effect: Any = None,
    blocked: bool = False,
) -> dict[str, Any]:
    """MiDojo's grade of one episode, as the trace carries it."""

    return {
        "security": security,
        "security_reason": reason,
        "grade_validity": {
            "security": {"status": validity, "reasons": []},
            "utility": {"status": "not_applicable", "reasons": []},
        },
        "injection_delivered": delivered,
        "attempted": True,
        "blocked": blocked,
        "effect_observed": effect,
    }


def trace(
    *,
    calls: tuple[dict[str, Any], ...] = (),
    security: Any = None,
    validity: str = "valid",
    delivered: Any = True,
    reason: str | None = None,
    effect: Any = None,
    blocked: bool = False,
    capture_complete: bool = True,
    sealed: str = "final",
    error: str | None = None,
    delivery: tuple[dict[str, Any], ...] = (),
    exposure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The sealed trace the probe puts on a chain's final attempt.

    ``delivery`` and ``exposure`` are what ``midojo.delivery.delivery_facts`` and
    ``exposure`` return for the sealed calls; an episode with no placement records neither,
    so the defaults are no facts and every boundary unknown.
    """

    return {
        "evaluation_id": EVALUATION_ID,
        "snapshot_id": SNAPSHOT_ID,
        "snapshot": {
            "evaluation_id": EVALUATION_ID,
            "snapshot_id": SNAPSHOT_ID,
            "evidence_status": {},
        },
        "verdict": {
            "evaluation_id": EVALUATION_ID,
            "snapshot_id": SNAPSHOT_ID,
            "grade": grade(
                security=security,
                validity=validity,
                delivered=delivered,
                reason=reason,
                effect=effect,
                blocked=blocked,
            ),
        },
        "capture_complete": capture_complete,
        "sealed": sealed,
        "failure": None,
        "error": error,
        "tool_calls": list(calls),
        "delivery": list(delivery),
        "exposure": exposure if exposure is not None else UNEXPOSED,
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
    """The ``tool_call_condition_results`` record the condition detector writes.

    The condition detector states its decision as an outcome and writes no score, unlike
    the two MiDojo detectors: the run's ``detector_results`` carries the score its outcome
    names.
    """

    return {
        "outcome": outcome,
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
    """The ``evaluation_verdict_results`` record the verdict detector writes."""

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


def effect_record(
    outcome: str,
    *,
    reason: str | None = None,
    detail: str | None = None,
    security: Any = None,
    validity: str = "valid",
    delivered: Any = True,
    capture_complete: bool = True,
    effect_observed: Any = None,
) -> dict[str, Any]:
    """The ``evaluation_effect_results`` record the effect detector writes."""

    return {
        "outcome": outcome,
        "score": SCORES.get(outcome),
        "reason": reason,
        "detail": detail,
        "security": security,
        "security_validity": validity,
        "injection_delivered": delivered,
        "capture_complete": capture_complete,
        "effect_observed": effect_observed,
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
            "injection_spec_chain": {
                "chain": chain,
                "turn": position,
                "turns": turns,
                "final": final,
            },
            "source_metadata": metadata,
        },
    }
    if sealed is not None:
        entry["notes"]["evaluation_trace"] = sealed
    if results is not None:
        entry["detector_results"] = results
    return entry


def chain(
    *,
    texts: tuple[str, ...] = ("Read me my notes.",),
    replies: tuple[str, ...] | None = None,
    sealed: dict[str, Any] | None = None,
    records: tuple[dict[str, Any], ...] = (),
    effect: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    chain_index: int = 0,
    seq: int = 0,
) -> list[dict[str, Any]]:
    """Return one chain's report lines: every turn at status 1, the last again at status 2.

    ``sealed`` is the trace the probe writes when it seals the chain; without one the
    chain ends at its last status-1 attempt, as a run killed before the seal leaves it.
    ``records`` are the verdict and condition records the detectors write, and ``effect``
    the effect detector's; they go on the status-2 attempt, which is the only one Garak
    evaluates. A detector with no record is left out of the report, as a run that never
    loaded it leaves it out.
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
    results: dict[str, list[Any]] = {}
    if len(records) > 1:
        # the probe's own detector runs first, then the extended ones in name order
        results[CONDITION_DETECTOR] = [SCORES.get(records[1].get("outcome"))]
        notes["tool_call_condition_results"] = list(records[1:])
    if effect is not None:
        results[EFFECT] = [effect.get("score")]
        notes["evaluation_effect_results"] = [effect]
    if records:
        results[VERDICT] = [records[0].get("score")]
        notes["evaluation_verdict_results"] = [records[0]]
    if results:
        finished["detector_results"] = results
    finished["notes"] = notes
    return [*lines, finished]


def legacy_report(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the report lines with the plugin and note names a pre-rename run wrote.

    Each old name belongs to the same plugin or note the probe and detectors write now,
    so a legacy report describes the same run in the recorded vocabulary. A test that
    compares one with a current report proves the parser reads both forms.
    """

    legacy: list[dict[str, Any]] = []
    for original in entries:
        entry = copy.deepcopy(original)
        if entry.get("probe_classname") in LEGACY_NAMES:
            entry["probe_classname"] = LEGACY_NAMES[entry["probe_classname"]]
        for member in ("notes", "detector_results"):
            found = entry.get(member)
            if isinstance(found, dict):
                entry[member] = {LEGACY_NAMES.get(key, key): value for key, value in found.items()}
        legacy.append(entry)
    return legacy


def write_report(path: Path, entries: list[dict[str, Any]]) -> Path:
    """Write report lines as ``report.jsonl`` and return the file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries),
        encoding="utf-8",
    )
    return path
