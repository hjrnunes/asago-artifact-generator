"""parse: turn MiDojo's control-plane records for one bundle into ``execution-receipt-v1``.

The run script left the control plane's run and evaluation records in the
bundle. A reply's rubric verifier left its verdict; a command attempt's verdict is computed
here from the calls MiDojo recorded. A usable run holds exactly one graded
evaluation whose verdict and ``security`` flag agree. Anything else yields a
``failed`` receipt whose ``incomplete_reason`` names what was wrong; the receipt
is still written.

The control plane's flag has two values and the contract three: ``security`` is
true exactly when the verdict is ``detected``, and the verdict tells ``not_detected`` from
``inconclusive`` (an inconclusive command grade agrees with either flag). When the
orchestrator's boundary services recorded the package, their calls replace the control
plane's, and the two counts must agree; so must the mini-agents server's own report log.

A sequential bundle's evaluation holds one turn per user turn MiDojo sent, each with
its message and reply, and every recorded call names the turn it belongs to
(``turn``). The receipt lists every planned turn with its reply and the calls made in
it; each call, from the proxy or the control plane, takes its turn from the matching
control-plane call. A turn that has no reply, or a run that ends before the last turn,
yields a ``failed`` receipt with ``incomplete_reason`` ``turn_failed:<position>``
(counted from 1) and the turns sent so far, as the Garak adapter writes it.

An unfinished call started and never finished: MiDojo holds it with ``complete: false``
and the mini-agents server logs it with ``report: unfinished``, under the same
``call_id``. It makes the attempt's result and detection ``inconclusive`` with the reason
``unfinished_call:<function>`` (the first such call), and the receipt still completes;
MiDojo's flag then agrees with either verdict, since MiDojo counts the call as an
attempt. The two records must name the same unfinished calls, or the receipt fails
with ``unfinished_call_mismatch``; so does an unfinished call in a run without a server log.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring.condition_eval import (
    evaluate_condition,
    validate_condition,
)
from asago_bundle_core.errors import BundleError
from asago_bundle_core.mcp_records import read_json, text
from asago_bundle_core.text import canonical_text

from .compiler import (
    BUNDLE_CODE,
    BUNDLE_FILE,
    COMMAND_ATTEMPT,
    EVALUATION_DUMP,
    NATIVE_OUTPUTS,
    REPLY,
    RUN_DUMP,
    SEQUENTIAL,
    SUITE_FILE,
    TOOL,
    TURNS_CODE,
    VERDICT_FILE,
)
from .environment import placeholder
from .records import Records, is_unfinished, native_call, read_records, server_unfinished

SCHEMA_VERSION = "execution-receipt-v1"
DETECTORS = {COMMAND_ATTEMPT: "ToolCallCondition", REPLY: "RubricJudge"}
OUTCOMES = ("detected", "not_detected", "inconclusive")
# The judge's three verdicts and the outcome each stands for.
JUDGE_OUTCOMES = {
    "supported": "detected",
    "contradicted": "not_detected",
    "unresolved": "inconclusive",
}
# A file a bundle lacks (``TURNS_CODE`` in all but a sequential reply bundle) is left out.
DIGESTED = (BUNDLE_FILE, SUITE_FILE, *BUNDLE_CODE, *TURNS_CODE)
MANIFEST_KEYS = frozenset({"scenario_id", "package_digest", "claim_level"})
RUNTIME_LISTS = (
    "processes",
    "network_calls_allowed",
    "network_calls_blocked",
    "files_created",
    "files_modified",
    "files_deleted",
    "security_findings",
)


class ParseError(BundleError):
    """The bundle cannot be read, so no receipt can name its package."""


@dataclass(frozen=True)
class Bundle:
    """A concrete bundle; ``turns`` holds a sequential bundle's user turns, else None."""

    root: Path
    manifest: dict[str, Any]
    prompt: str | None
    suite: dict[str, Any]
    turns: list[str] | None = None


@dataclass(frozen=True)
class Evidence:
    """The run, the evaluation and the verdict of one run.

    A command attempt's verdict is computed from the recorded calls; a reply's is the rubric
    verifier's file.
    """

    run: dict[str, Any]
    evaluation: dict[str, Any]
    verdict: dict[str, Any]


def parse_bundle(
    bundle_dir: str | Path,
    out_path: str | Path,
    *,
    records_dir: str | Path | None = None,
    tool_revision: str | None = None,
    adapter_revision: str | None = None,
) -> dict[str, Any]:
    """Write the receipt for one bundle's control-plane records to ``out_path`` and return it."""

    bundle = read_bundle(Path(bundle_dir))
    out = Path(out_path)
    records = read_records(Path(records_dir)) if records_dir else Records()
    attempt, reason = build_attempt(bundle, records)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"id": TOOL, "revision": tool_revision, "adapter_revision": adapter_revision},
        "package": {
            "id": bundle.manifest["scenario_id"],
            "digest": bundle.manifest["package_digest"],
        },
        "bundle_digest": bundle_digest(bundle.root),
        "execution_status": "failed" if reason else "completed",
        "runtime_status": None if reason else "completed",
        "incomplete_reason": reason,
        "attempts": [attempt] if attempt else [],
        "native": native_files(bundle.root, out.parent, bundle.manifest["claim_level"]),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_text(receipt), encoding="utf-8")
    return receipt


def read_bundle(root: Path) -> Bundle:
    try:
        manifest = json.loads((root / BUNDLE_FILE).read_text(encoding="utf-8"))
        suite = json.loads((root / SUITE_FILE).read_text(encoding="utf-8"))
        task = suite["user_tasks"][0]
        turns = task["turns"] if manifest.get("delivery") == SEQUENTIAL else None
        if turns is not None and not (
            isinstance(turns, list) and turns and all(isinstance(item, str) for item in turns)
        ):
            raise ParseError("the suite's turns are not a list of user texts")
        prompt = task["prompt"] if turns is None else None
        if manifest.get("claim_level") == COMMAND_ATTEMPT:
            security = suite["injection_tasks"][0]["security"]
            if "tool_call_condition" not in security:
                raise ParseError(
                    "the suite has no tool_call_condition: it was compiled before the builtin "
                    "MiDojo predicate; recompile the package"
                )
            condition = security["tool_call_condition"]
            problem = validate_condition(condition)
            if problem:
                raise ParseError(f"the suite's condition is invalid: {problem}")
        missing = MANIFEST_KEYS - manifest.keys()
    except (OSError, IndexError, KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ParseError(f"bundle is unreadable: {exc}") from exc
    if missing:
        raise ParseError(f"bundle manifest lacks {sorted(missing)}")
    return Bundle(root, manifest, prompt, suite, turns)


def build_attempt(bundle: Bundle, records: Records) -> tuple[dict[str, Any] | None, str | None]:
    """Return the run's one attempt and why the run cannot be graded, if it cannot.

    A sequential run whose turn failed is the one exception to the order of ``CHECKS``:
    MiDojo completes and grades an evaluation only after every turn got its reply, so
    the failed turn takes the place of ``_graded`` (after the evaluation's count and
    identity) and comes before the verdict is read, and the receipt keeps the attempt
    of the turns sent (``failed_turn``).
    """

    control, reason = read_control_plane(bundle)
    if control is None:
        return None, reason
    if bundle.turns is not None:
        attempt, reason = failed_turn(control, bundle, records)
        if reason:
            return attempt, reason
    evidence, reason = read_evidence(bundle, control)
    if evidence is None:
        return None, reason
    reason = evidence_failure(evidence, bundle, records)
    if reason:
        return None, reason
    return attempt_of(evidence, bundle, records), None


def read_control_plane(
    bundle: Bundle,
) -> tuple[tuple[dict[str, Any], dict[str, Any]] | None, str | None]:
    """Return the run and evaluation records, or why they cannot be read."""

    documents = []
    for name in (RUN_DUMP, EVALUATION_DUMP):
        document, reason = read_document(bundle.root / name, "control_plane")
        if document is None:
            return None, reason
        documents.append(document)
    run, evaluation = documents
    return (run, evaluation), None


def read_evidence(
    bundle: Bundle, control: tuple[dict[str, Any], dict[str, Any]]
) -> tuple[Evidence | None, str | None]:
    run, evaluation = control
    if bundle.manifest["claim_level"] == COMMAND_ATTEMPT:
        return Evidence(run, evaluation, command_verdict(bundle, evaluation)), None
    verdict, reason = read_document(bundle.root / VERDICT_FILE, "verdict")
    if verdict is None:
        # A grade that raised leaves no verdict; the ungraded evaluation is the cause.
        return None, reason if is_graded(evaluation) else "evaluation_ungraded"
    return Evidence(run, evaluation, verdict), None


def command_verdict(bundle: Bundle, evaluation: dict[str, Any]) -> dict[str, Any]:
    """Return the verdict of the suite's condition on the calls MiDojo recorded.

    Malformed calls are reported by their own check, so they count as no calls here.
    """

    condition = bundle.suite["injection_tasks"][0]["security"]["tool_call_condition"]
    recorded = evaluation.get("function_calls")
    if not (isinstance(recorded, list) and all(isinstance(call, dict) for call in recorded)):
        recorded = []
    calls = [{"name": call.get("function"), "arguments": call.get("args")} for call in recorded]
    result = evaluate_condition(condition, calls, True)
    return {
        "outcome": result.outcome,
        "reason": result.reason,
        "claim_level": COMMAND_ATTEMPT,
        "matched_calls": list(result.matched_calls),
    }


def read_document(path: Path, prefix: str) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, f"{prefix}_missing"
    document = read_json(path)
    if not isinstance(document, dict):
        return None, f"{prefix}_malformed"
    return document, None


Check = Callable[[Evidence, Bundle, Records], "str | None"]


def evidence_failure(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    """Return the first reason the evidence cannot be graded, or None."""

    for check in CHECKS:
        reason = check(evidence, bundle, records)
        if reason:
            return reason
    return None


def _evaluation_count(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    evaluations = evidence.run.get("evaluations")
    ok = isinstance(evaluations, list) and len(evaluations) == 1
    return None if ok else "evaluation_count_mismatch"


def _evaluation_identity(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    listed = evidence.run["evaluations"][0]
    same = isinstance(listed, dict) and listed.get("id") == evidence.evaluation.get("id")
    return None if same else "evaluation_mismatch"


def is_graded(evaluation: dict[str, Any]) -> bool:
    return evaluation.get("completed") is True and isinstance(evaluation.get("security"), bool)


def _graded(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    return None if is_graded(evidence.evaluation) else "evaluation_ungraded"


def _verdict_shape(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    verdict = evidence.verdict
    level = bundle.manifest["claim_level"]
    ok = (
        verdict.get("outcome") in OUTCOMES
        and verdict.get("claim_level") == level
        and text(verdict.get("reason")) is not None
        and LEVEL_SHAPES.get(level, _never)(verdict)
    )
    return None if ok else "verdict_malformed"


def _never(verdict: dict[str, Any]) -> bool:
    return False


def _command_shape(verdict: dict[str, Any]) -> bool:
    matched = verdict.get("matched_calls")
    return isinstance(matched, list) and all(_is_count(index) for index in matched)


def _reply_shape(verdict: dict[str, Any]) -> bool:
    refs = verdict.get("evidence_refs")
    return (
        JUDGE_OUTCOMES.get(verdict.get("verdict")) == verdict["outcome"]
        and isinstance(refs, list)
        and all(isinstance(ref, str) for ref in refs)
        and _is_count(verdict.get("request_count"))
    )


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


LEVEL_SHAPES: dict[str, Callable[[dict[str, Any]], bool]] = {
    COMMAND_ATTEMPT: _command_shape,
    REPLY: _reply_shape,
}


def _verdict_agrees(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    if unfinished_calls(evidence.evaluation["function_calls"]):
        # The attempt is inconclusive (``attempt_verdict``), and MiDojo's flag counts the
        # unfinished call as an attempt, so either flag agrees.
        return None
    outcome, security = evidence.verdict["outcome"], evidence.evaluation["security"]
    if bundle.manifest["claim_level"] == COMMAND_ATTEMPT:
        # The condition is MiDojo's own; an inconclusive grade agrees with either flag.
        disagrees = (security and outcome == "not_detected") or (
            not security and outcome == "detected"
        )
        return "verdict_disagrees" if disagrees else None
    return None if security is (outcome == "detected") else "verdict_disagrees"


def _prompt_matches(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    """A single run sent the bundle's prompt; a sequential one, a prefix of its turns."""

    if bundle.turns is not None:
        return _turns_match(evidence, bundle, records)
    same = evidence.evaluation.get("agent_input") == bundle.prompt
    return None if same else "agent_input_mismatch"


def _calls_shape(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    calls = evidence.evaluation.get("function_calls")
    ok = isinstance(calls, list) and all(isinstance(call, dict) for call in calls)
    return None if ok else "function_calls_malformed"


def _seed_provisioned(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    """Every probe's payload must sit where the suite put its placeholder."""

    task = bundle.suite["injection_tasks"][0]
    probes = task.get("probes") or {}
    provisioned = evidence.evaluation.get("pre_environment")
    for name, probe in probes.items():
        index = int(name.removeprefix("seed_"))
        path = _find(bundle.suite.get("environment"), placeholder(index))
        value = _at(provisioned, path) if path is not None else None
        if not (isinstance(value, str) and probe["payload"] in value):
            return "seed_not_provisioned"
    return None


def _find(node: Any, needle: str) -> list[Any] | None:
    """Return the path of the first string in *node* that holds *needle*."""

    if isinstance(node, str):
        return [] if needle in node else None
    children = (
        node.items()
        if isinstance(node, dict)
        else enumerate(node)
        if isinstance(node, list)
        else ()
    )
    for key, child in children:
        found = _find(child, needle)
        if found is not None:
            return [key, *found]
    return None


def _at(node: Any, path: list[Any]) -> Any:
    for key in path:
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return None
    return node


def _records_agree(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    """MiDojo, the server log and the proxy hold the same calls.

    MiDojo and the server log must name the same unfinished calls by ``call_id``, and
    a run without a server log can hold none; then
    equal totals mean equal finished calls. The proxy holds unfinished calls too:
    asago-orch's recording proxy records each ``tools/call`` when its request arrives,
    before forwarding it (``mcp_proxy.py:165-193``, called at ``:293-295``), with
    ``status: no_response`` and no result or error until a reply settles it
    (``:275-281``). A call the server never finished is therefore in the proxy's file,
    still ``no_response`` or with whatever reply the framework sent, and it pairs with
    MiDojo's record by content as any call does (``proxy_turns``).
    """

    if records.malformed:
        return "records_malformed"
    if records.undelivered:
        return "sink_failed"
    function_calls = evidence.evaluation["function_calls"]
    # Without a server log, no unfinished call has its second record.
    server_unfinished_lines = [line for line in records.server or [] if server_unfinished(line)]
    if _call_ids(unfinished_calls(function_calls)) != _call_ids(server_unfinished_lines):
        return "unfinished_call_mismatch"
    if records.calls is not None and len(records.calls) != len(
        evidence.evaluation["function_calls"]
    ):
        return "tool_call_count_mismatch"
    if records.server is not None and len(records.server) != len(
        evidence.evaluation["function_calls"]
    ):
        return "tool_call_count_mismatch"
    if bundle.turns is not None and records.calls is not None:
        _, reason = proxy_turns(evidence.evaluation["function_calls"], records)
        return reason
    return None


def unfinished_calls(function_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [call for call in function_calls if is_unfinished(call)]


def _call_ids(items: list[dict[str, Any]]) -> list[str]:
    """Return the items' ``call_id`` values as sorted canonical JSON text, repeats kept."""

    return sorted(json.dumps(item.get("call_id"), sort_keys=True) for item in items)


CHECKS: tuple[Check, ...] = (
    _evaluation_count,
    _evaluation_identity,
    _graded,
    _calls_shape,
    _verdict_shape,
    _verdict_agrees,
    _prompt_matches,
    _seed_provisioned,
    _records_agree,
)


# --- the turns of a sequential run ---------------------------------------------------------


def _turns_match(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    """The sequential check in the place of the prompt's: the turns and the calls' turns."""

    for check in (_turns_shape, _turn_inputs, _call_turns):
        reason = check(evidence, bundle, records)
        if reason:
            return reason
    return None


def _turns_shape(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    """The evaluation's turns are numbered from 0, each with its message text."""

    return None if well_formed_turns(evidence.evaluation) else "turns_malformed"


def well_formed_turns(evaluation: dict[str, Any]) -> bool:
    turns = evaluation.get("turns")
    return isinstance(turns, list) and all(
        isinstance(turn, dict)
        and type(turn.get("index")) is int
        and turn["index"] == position
        and isinstance(turn.get("message"), str)
        for position, turn in enumerate(turns)
    )


def _turn_inputs(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    """The turns MiDojo sent are the bundle's turns, in order, and no more."""

    sent = [turn["message"] for turn in evidence.evaluation["turns"]]
    same = len(sent) <= len(bundle.turns) and sent == bundle.turns[: len(sent)]
    return None if same else "turn_input_mismatch"


def _call_turns(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    """Every control-plane call names a turn MiDojo sent; none is placed by default."""

    sent = len(evidence.evaluation["turns"])
    for call in evidence.evaluation["function_calls"]:
        turn = call.get("turn")
        if turn is None:
            return "call_turn_missing"
        if type(turn) is not int or not 0 <= turn < sent:
            return "call_turn_out_of_range"
    return None


def call_key(name: Any, arguments: Any) -> str:
    """Return a call's identity: its tool name and its arguments, canonical JSON text."""

    return json.dumps([name, arguments], sort_keys=True, ensure_ascii=False)


def proxy_turns(
    function_calls: list[dict[str, Any]], records: Records
) -> tuple[list[int] | None, str | None]:
    """Return the turn of each proxy call, from the control-plane call of the same content.

    The proxy lists calls as their requests arrive and the sink delivers them to the
    control plane once settled, so the two lists can differ in order; calls are paired by
    ``call_key``, never by position or by name alone, and the two lists must hold the
    same calls (``tool_call_mismatch``).

    Calls of one key that MiDojo stamped with different turns are paired in order:
    the earlier-arriving proxy call takes the earlier turn. The proxy numbers each call
    as its request arrives (``sequence``, asago-orch ``mcp_proxy.py:171-182``). MiDojo
    starts turn k before it sends message k and records reply k before it starts turn
    k+1 (midojo ``orchestrator.py:235-237``), and stamps a call with the latest started
    turn when the sink delivers it (``control_plane/store.py:209``), which the sink does
    before the result reaches the target (``mcp_proxy.py:11-13``). So while the target
    waits for each result before it replies, every call of turn k arrives before every
    call of turn k+1. That holding is checked: the arrival order of all calls must not
    go back a turn. Without arrival numbers, or when it goes back, the pairing is
    ``tool_call_ambiguous``.
    """

    proxy = records.calls or []
    stamped: dict[str, list[int]] = {}
    for call in function_calls:
        stamped.setdefault(call_key(call.get("function"), call.get("args")), []).append(
            call["turn"]
        )
    positions: dict[str, list[int]] = {}
    for position, call in enumerate(proxy):
        positions.setdefault(call_key(call.get("name"), call.get("arguments")), []).append(
            position
        )
    if {key: len(turns) for key, turns in stamped.items()} != {
        key: len(found) for key, found in positions.items()
    }:
        return None, "tool_call_mismatch"
    arrivals = records.arrivals or []
    ordered = len(arrivals) == len(proxy) and all(type(a) is int for a in arrivals)
    ordered = ordered and len(set(arrivals)) == len(arrivals)
    turns: list[int] = [0] * len(proxy)
    by_arrival = False
    for key, found in positions.items():
        expected = sorted(stamped[key])
        if len(set(expected)) > 1:
            if not ordered:
                return None, "tool_call_ambiguous"
            found = sorted(found, key=lambda position: arrivals[position])
            by_arrival = True
        for position, turn in zip(found, expected, strict=True):
            turns[position] = turn
    if by_arrival:
        in_arrival = [
            turns[position] for position in sorted(range(len(proxy)), key=arrivals.__getitem__)
        ]
        if in_arrival != sorted(in_arrival):
            return None, "tool_call_ambiguous"
    return turns, None


def failed_turn(
    control: tuple[dict[str, Any], dict[str, Any]], bundle: Bundle, records: Records
) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``turn_failed:<position>`` and the attempt of the turns sent, or no reason.

    Turns that are not well formed are left to ``_turns_shape`` in its place. The
    attempt is written only when the records it shows pass their checks; otherwise
    the receipt names the failed turn and holds no attempt.
    """

    run, evaluation = control
    if not well_formed_turns(evaluation):
        return None, None
    reason = turn_failure(evaluation["turns"], len(bundle.turns))
    if reason is None:
        return None, None
    unread = Evidence(run, evaluation, {})
    # The checks before ``_graded`` in ``CHECKS`` come before the failed turn too.
    for check in RUN_CHECKS:
        failure = check(unread, bundle, records)
        if failure:
            return None, failure
    for check in SENT_TURN_CHECKS:
        if check(unread, bundle, records):
            return None, reason
    level = bundle.manifest["claim_level"]
    source, calls = sequential_calls(evaluation["function_calls"], records)
    replies = turn_replies(evaluation["turns"])
    return {
        "index": 0,
        "generation": {
            "request_count": records.request_count,
            "response_status": None,
            "error_type": None,
        },
        "turns": receipt_turns(bundle.turns, replies, calls),
        "result": {
            "outcome": None,
            "claim_level": level,
            "reason": f"detection_missing:{DETECTORS[level]}",
        },
        "observation": {
            "source": source,
            "assistant_messages": [replies[index] for index in sorted(replies)],
            "tool_calls": calls,
        },
        "runtime_observations": {"source": None, **{name: [] for name in RUNTIME_LISTS}},
        "detections": [],
        "judge": None,
    }, reason


# The checks of ``CHECKS`` before ``_graded``, whose place a failed turn takes.
RUN_CHECKS: tuple[Check, ...] = (_evaluation_count, _evaluation_identity)
# The checks of the records a failed turn's attempt shows.
SENT_TURN_CHECKS: tuple[Check, ...] = (
    _calls_shape,
    _turn_inputs,
    _call_turns,
    _records_agree,
)


def turn_failure(turns: list[dict[str, Any]], planned: int) -> str | None:
    """Return ``turn_failed:<position>`` for the first turn without a reply, else None.

    An empty reply is no reply, as Garak's ``EmptyReply``.
    """

    for turn in turns:
        reply = turn.get("reply")
        if not isinstance(reply, str) or not reply:
            return f"turn_failed:{turn['index'] + 1}"
    if len(turns) < planned:
        return f"turn_failed:{len(turns) + 1}"
    return None


def turn_replies(turns: list[dict[str, Any]]) -> dict[int, str]:
    """Map each turn index to its reply text."""

    return {turn["index"]: turn["reply"] for turn in turns if text(turn.get("reply"))}


def sequential_calls(
    function_calls: list[dict[str, Any]], records: Records
) -> tuple[str, list[dict[str, Any]]]:
    """Return the observed calls, each with the turn MiDojo stamped on its control-plane call.

    A proxy call takes the turn of the control-plane call of the same content
    (``proxy_turns``, which ``_records_agree`` has already run).
    """

    source, calls = observed_calls(function_calls, records)
    if records.calls is None:
        turns = [recorded["turn"] for recorded in function_calls]
    else:
        turns, _ = proxy_turns(function_calls, records)
        assert turns is not None, "the records were checked by _records_agree"
    return source, [{**call, "turn_index": turn} for call, turn in zip(calls, turns, strict=True)]


def receipt_turns(
    messages: list[str], replies: dict[int, str], calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return one receipt turn per planned user turn: its request, reply, and calls."""

    return [
        {
            "index": index,
            "request": {"messages": [{"role": "user", "content": message}]},
            "response": {
                "assistant_messages": [replies[index]] if index in replies else [],
                "tool_call_indices": [
                    position for position, call in enumerate(calls) if call["turn_index"] == index
                ],
            },
        }
        for index, message in enumerate(messages)
    ]


def attempt_of(evidence: Evidence, bundle: Bundle, records: Records) -> dict[str, Any]:
    evaluation = evidence.evaluation
    verdict = attempt_verdict(evidence.verdict, evaluation["function_calls"])
    if bundle.turns is None:
        source, calls = observed_calls(evaluation["function_calls"], records)
        output = text(evaluation.get("agent_output"))
        replies = [output] if output else []
        turns = receipt_turns([bundle.prompt], {0: output} if output else {}, calls)
    else:
        source, calls = sequential_calls(evaluation["function_calls"], records)
        by_turn = turn_replies(evaluation["turns"])
        replies = [by_turn[index] for index in sorted(by_turn)]
        turns = receipt_turns(bundle.turns, by_turn, calls)
    return {
        "index": 0,
        "generation": {
            "request_count": records.request_count,
            "response_status": "completed",
            "error_type": None,
        },
        "turns": turns,
        "result": {
            "outcome": verdict["outcome"],
            "claim_level": verdict["claim_level"],
            "reason": verdict["reason"],
        },
        "observation": {"source": source, "assistant_messages": replies, "tool_calls": calls},
        "runtime_observations": {"source": None, **{name: [] for name in RUNTIME_LISTS}},
        "detections": [detection_of(verdict)],
        "judge": judge_of(evidence.verdict, records),
    }


def attempt_verdict(
    verdict: dict[str, Any], function_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return the verdict the attempt reports: inconclusive while a call is unfinished.

    The detector graded calls of which one never finished, so its verdict stands for
    no outcome; the reason names the first unfinished call. The judge record keeps the
    judge's own verdict.
    """

    unfinished = unfinished_calls(function_calls)
    if not unfinished:
        return verdict
    reason = f"unfinished_call:{unfinished[0].get('function')}"
    return {**verdict, "outcome": "inconclusive", "reason": reason}


def detection_of(verdict: dict[str, Any]) -> dict[str, Any]:
    """Return the detection the verifier's verdict stands for; the judge names no calls."""

    return {
        "detector": DETECTORS[verdict["claim_level"]],
        "outcome": verdict["outcome"],
        "reason": verdict["reason"],
        "matched_calls": verdict.get("matched_calls"),
    }


def judge_of(verdict: dict[str, Any], records: Records) -> dict[str, Any] | None:
    """Return the judge record of a reply verdict; the boundary judge's count replaces its own."""

    if verdict["claim_level"] != REPLY:
        return None
    count = records.judge_count if records.judge_count is not None else verdict["request_count"]
    return {"verdict": verdict["verdict"], "reason": verdict["reason"], "request_count": count}


def observed_calls(
    function_calls: list[dict[str, Any]], records: Records
) -> tuple[str, list[dict[str, Any]]]:
    if records.calls is not None:
        return "mcp_recording_proxy", records.calls
    return "tool_native", [native_call(call) for call in function_calls]


def bundle_digest(root: Path) -> str:
    files = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in DIGESTED
        if (root / name).is_file()
    }
    return hashlib.sha256(canonical_text(files).encode("utf-8")).hexdigest()


def native_files(root: Path, receipt_dir: Path, claim_level: str) -> list[dict[str, str]]:
    """List the native outputs that exist, relative to the receipt's directory."""

    found = []
    for name in NATIVE_OUTPUTS[claim_level]:
        path = root / name
        if not path.is_file():
            continue
        try:
            relative = path.resolve().relative_to(receipt_dir.resolve())
        except ValueError as exc:
            raise ParseError("the native outputs must lie inside the receipt's directory") from exc
        found.append(
            {"path": relative.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    return found
