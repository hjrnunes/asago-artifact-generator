"""The planted packages, and the reports an ``InjectionSpecProbe`` run writes, for the tests.

The packages come from the injection-spec library's own builders
(``packages/asago-injection-spec/tests/injection_spec_cases.py``): both adapters compile the
same package for the same fixture target, and the entry Garak writes must carry the
injection spec MiDojo compiled. The reports follow what ``garak/probes/midojo.py`` and
``garak/detectors/midojo.py`` write: each turn's attempt at status 1, the turn that
ends the chain again at status 2 with the detectors' results, and the sealed trace on
every attempt of a sealed chain.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

from midojo_spec.snapshot import snapshot_digest

from conftest import REPO

INJECTION_SPEC_TESTS = REPO / "packages" / "asago-injection-spec" / "tests"
if str(INJECTION_SPEC_TESTS) not in sys.path:
    sys.path.insert(0, str(INJECTION_SPEC_TESTS))

from injection_spec_cases import (  # noqa: E402, F401  the library's builders, by path
    CONDITION,
    JUDGE,
    PIGGYBANK,
    PLANTED,
    READ_SLOT,
    SEQUENTIAL_TURNS,
    SINGLE_TURNS,
    SLOTS,
    TARGET,
    history_binding_options,
    planted_stimulus,
    seed_document,
    seed_item,
    write_planted_package,
)

# The probe's class name, as the report's ``probe_classname`` writes it.
PROBE = "midojo.InjectionSpecProbe"
VERDICT = "midojo.EvaluationVerdict"
EFFECT = "midojo.EvaluationEffect"
CONDITION_DETECTOR = "toolcall.ToolCallCondition"
SCORES = {"detected": 1.0, "not_detected": 0.0, "inconclusive": None}
# The exposure of an episode whose payload reached no recorded boundary.
UNEXPOSED = {
    "emitted": "unknown",
    "agent_tool_result": "unknown",
    "model_input": "unknown",
    "instructions": [],
}
EVALUATION_ID = "evaluation-6f1c"
# The reason MiDojo's grade states for the fixture target's condition: a decided grade
# always states one, so a test that does not name one gets this.
DEFAULT_REASON = "the condition holds on the captured calls"
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
    evidence_status: dict[str, str] | None = None,
    utility: Any = False,
    utility_status: str = "not_applicable",
) -> dict[str, Any]:
    """MiDojo's grade of one episode, as the trace carries it.

    Every field of ``GradeResponse`` is here, as MiDojo writes them: a decided grade states
    its reason (``DEFAULT_REASON`` when a test does not name one), an undecided one does not.
    A check the injection spec does not author grades ``utility`` ``False`` with the status
    ``not_applicable``, which is what a test that names neither gets; a test that sets
    ``utility_status`` to ``valid`` or ``invalid`` writes the grade of a check that ran.
    """

    return {
        "security": security,
        "security_reason": DEFAULT_REASON if reason is None and security is not None else reason,
        "utility": utility,
        "utility_reason": None,
        "grade_validity": {
            "security": {"status": validity, "reasons": []},
            "utility": {"status": utility_status, "reasons": []},
        },
        "evidence_status": dict(evidence_status or {}),
        "injection_delivered": delivered,
        "attempted": True,
        "blocked": blocked,
        "effect_observed": effect,
    }


def sealed_snapshot(
    *,
    calls: tuple[dict[str, Any], ...] = (),
    outcomes: dict[str, list[dict[str, Any]]] | None = None,
    evidence_status: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A snapshot in MiDojo's ``SnapshotResponse`` shape, holding ``calls``, digested.

    ``outcomes`` gives a call's ``injection_outcomes`` by ``call_id``, as MiDojo's
    interceptor seals them; a call it does not name states none. The snapshot ID is the
    content's own digest, so ``midojo_spec.snapshot.verified`` accepts it.
    """

    content = {
        "evaluation_id": EVALUATION_ID,
        "run_id": None,
        "user_task_id": None,
        "injection_task_id": None,
        "agent_output": "Done.",
        "pre_environment": {},
        "environment": {},
        "function_calls": [
            {
                "call_id": call["call_id"],
                "function": call["name"],
                "args": copy.deepcopy(call["arguments"]),
                "result": call["result"],
                "error": None,
                "blocked": call["blocked"],
                "disposition": call["disposition"],
                "complete": call["complete"],
                "provenance": "interceptor",
                "timestamp": "2026-01-01T00:00:00Z",
                "turn": call["turn"],
                "pre_environment": {},
                "post_environment": {},
                "structured_content": None,
                "injection_outcomes": (outcomes or {}).get(call["call_id"]),
            }
            for call in calls
        ],
        "turns": [],
        "observations": {},
        "evidence_status": dict(evidence_status or {}),
        "sealed_at": "2026-01-01T00:00:01Z",
        "capture_failures": [],
    }
    return {**content, "snapshot_id": snapshot_digest(content)}


def trace(
    *,
    calls: tuple[dict[str, Any], ...] = (),
    outcomes: dict[str, list[dict[str, Any]]] | None = None,
    security: Any = None,
    validity: str = "valid",
    delivered: Any = True,
    reason: str | None = None,
    effect: Any = None,
    blocked: bool = False,
    capture_complete: bool = True,
    sealed: str = "final",
    error: str | None = None,
    release_error: str | None = None,
    delivery: tuple[dict[str, Any], ...] = (),
    exposure: dict[str, Any] | None = None,
    evidence_status: dict[str, str] | None = None,
    utility: Any = False,
    utility_status: str = "not_applicable",
) -> dict[str, Any]:
    """The sealed trace the probe puts on a chain's final attempt.

    The snapshot is MiDojo's document shape and its ID is its own digest, so the route can
    verify it. ``delivery`` and ``exposure`` are what ``midojo.delivery.delivery_facts`` and
    ``exposure`` return for the sealed calls; only a sealed chain states them, so a chain
    closed short (``sealed`` not ``final``) records neither, as the probe writes it. An
    episode with no placement records no facts and every boundary unknown. ``release_error``
    is the reason MiDojo's service gave for a release it never acknowledged after the seal,
    and None when the probe closed the handle cleanly, as the probe writes both.
    """

    evidence = dict(evidence_status or {})
    snapshot = sealed_snapshot(calls=calls, outcomes=outcomes, evidence_status=evidence)
    final = sealed == "final"
    return {
        "evaluation_id": EVALUATION_ID,
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot": snapshot,
        "verdict": {
            "schema_version": "midojo-verdict-v1",
            "evaluation_id": EVALUATION_ID,
            "snapshot_id": snapshot["snapshot_id"],
            "grade": grade(
                security=security,
                validity=validity,
                delivered=delivered,
                reason=reason,
                effect=effect,
                blocked=blocked,
                evidence_status=evidence,
                utility=utility,
                utility_status=utility_status,
            ),
        },
        "capture_complete": capture_complete,
        "sealed": sealed,
        "failure": None,
        "error": error,
        "release_error": release_error,
        "tool_calls": list(calls),
        "delivery": list(delivery) if final else None,
        "exposure": (exposure if exposure is not None else UNEXPOSED) if final else None,
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
    disposition: str = "forwarded",
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


def injection_outcome(
    *,
    index: int = 0,
    slot_key: str = "planted_item:seed_0",
    occurrence: int = 1,
    status: str = "applied",
    detail: str = "exact selector matched; mirrored text re-serialised",
    selected: bool = True,
    emitted: tuple[str, ...] = ("structured_content",),
    payload_sha256: str | None = None,
    slot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One call's ``CallInjectionOutcome``, as MiDojo seals it: what the instruction placed.

    ``payload_sha256`` and ``slot`` are the digest of the payload text and the seed slot it
    was placed in, each written only when the outcome states it.
    """

    found: dict[str, Any] = {
        "instruction_index": index,
        "slot_key": slot_key,
        "occurrence": occurrence,
        "status": status,
        "detail": detail,
        "selected": selected,
        "transformation": "identity",
        "emitted": list(emitted),
    }
    if payload_sha256 is not None:
        found["payload_digest"] = f"sha256:{payload_sha256}"
        found["placed_digest"] = f"sha256:{payload_sha256}"
    if slot is not None:
        found["slot"] = slot
    return found


def delivery_fact(
    *,
    call_id: str = "call-1",
    function: str = "list_notes",
    disposition: str = "forwarded",
    turn: int = 0,
    index: int = 0,
    slot_key: str = "planted_item:seed_0",
    occurrence: int = 1,
    status: str = "applied",
    selected: bool = True,
    emitted: tuple[str, ...] = ("structured_content",),
    slot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One ``DeliveryFact``: the instruction placed on a call, as the probe records it.

    ``slot`` is the seed slot the payload was placed in, written only when the fact states
    it; the probe computes it from the plan, and the receipt leaves it out.
    """

    found: dict[str, Any] = {
        "call_id": call_id,
        "function": function,
        "disposition": disposition,
        "turn": turn,
        "interceptor_instruction_index": index,
        "plan_instruction_index": index,
        "slot_key": slot_key,
        "occurrence": occurrence,
        "status": status,
        "selected": selected,
        "transformation": "identity",
        "emitted": list(emitted),
    }
    if slot is not None:
        found["slot"] = slot
    return found


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


def write_report(path: Path, entries: list[dict[str, Any]]) -> Path:
    """Write report lines as ``report.jsonl`` and return the file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries),
        encoding="utf-8",
    )
    return path
