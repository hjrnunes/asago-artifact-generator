"""compile: turn a planted_item package into one MiDojo episode for one target.

The output directory holds ``episode.json`` (``midojo-episode-v2``): the package's
identity, its claim level and delivery, the MiDojo revisions the episode needs, how the
run intercepts the target's tools, which tools the agent may call and what happens to a
write, and ``episode``, the MiDojo ``Episode`` as plain data (``episode.py``). Everything in
it is fixed by the package and the caller's target config, so the same inputs give the
same bytes.

- The turns are the stimulus's user turns: its history's, then its user text.
- Each seed item becomes one ``tool_output`` instruction from the seed slot of the
  package's runtime contract it names (``placement.py``): the carrier's structured
  result, the field through a selector of the item's record by the slot's declared
  ``record_key`` (a field inside a list), or the plain field of a single-record carrier
  on the calls whose ``record_key`` argument is the record, else on the calls whose
  result's ``record_key`` property is the record, the slot's mode and ``max_chars``.
  ``placements`` restates each in the contract's terms. MiDojo's host interceptor places
  the text in the result as it forwards the call; the target's data is not changed.
- ``interception`` is host-side: the runner opens the episode with MiDojo's
  ``HostInterception`` whose upstream is the target config's ``endpoint.mcp_url`` at run
  time. The agent holds the interceptor's URL and bearer token and nothing else of
  MiDojo's: no evaluation session, and no address or credential of its control plane.
  The target is only where the interceptor forwards calls, and gets no MiDojo credential
  or session either. The episode holds no URL.
- ``tools.allowed`` are the operations of the package's inventory; the agent is given no
  other. ``tools.write_disposition`` names, for each allowed tool the target config
  declares mutating (``declared.py``), ``forward`` (the target performs the write; the
  default) or ``block`` (nothing runs; MiDojo records the call as blocked). Every other
  call is forwarded.
- A command attempt is checked with MiDojo's builtin ``tool_call_condition`` predicate,
  whose value is the package's condition. A reply is checked with MiDojo's builtin
  ``reply_judge`` predicate: its claim is the judge's question, its rubric the judge's
  criteria and facts with the episode's user turns and ``judge_turns``, and it asks the
  judge service (``judge_service.py``) at the URL in ``JUDGE_URL`` with the token in
  ``JUDGE_TOKEN``, sending the earlier turns and the recorded calls. It waits 180
  seconds for each turn the service judges, and 10 more (``judge_timeout_seconds``).
  An episode runs no package setup, so the judge gets no runtime facts. The directory
  holds ``episode.json`` alone, and ``verifiers`` names no verifier for the runner to
  register.
- There is no utility check and no environment, and the runtime is ``unmanaged``: the
  runner connects the agent.

MiDojo runs planted_item packages only. A package whose stimulus delivers its attack in
the user turns (a direct or user-turn package), a single package with history, a
history message that is not the user's, a stimulus with run-time bindings, a
state_effect claim, and a seed item the interceptor cannot place in an MCP tool result
(``placement.gap_reason``) are capability gaps: ``compile`` raises ``CapabilityGap`` with
the shared record and writes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring.condition_eval import validate_condition
from asago_artifact_generator.package_io import ArtifactPackage, load_package
from asago_bundle_core.errors import BundleError
from asago_bundle_core.gap import CapabilityGap, capability_gap_record
from asago_bundle_core.slots import contains_marker
from asago_bundle_core.target import TargetConfig, authorized_target
from asago_bundle_core.text import canonical_text

from .declared import DeclarationFault, DeclaredTools, declared_tools
from .episode import episode, injection_plan
from .judge_service import FINAL_TURN, JUDGE_TURNS, TIMEOUT_SECONDS, judged_count
from .placement import (
    Placement,
    PlacementFault,
    gap_reason,
    locate,
    placements,
    runtime_contract,
    seed_slots,
)

SCHEMA_VERSION = "midojo-episode-v2"
TOOL = "midojo"
# ``Episode`` with its injection plan, ``midojo.open_episode`` with ``HostInterception``,
# a structured-content path's record selector, and ``midojo.grade`` over a sealed snapshot
# are on asago/main at a5f81e9. From 3824922 the interceptor also copies a placement into a
# result's text when that text mirrors its structured content, so an agent that reads only
# the text gets the planted item, and each sealed call record carries its injection outcomes.
# From b5b8c75 a target can match the call's arguments (``call_arguments``), which places an
# item in the result of a single-record carrier.
TOOL_REVISION_RANGE = ">=b5b8c752"
# From c0bee12 (the merge of result-record matching into asago/main) a target can match the
# call's result instead (``result_record``), which places an item in the result of a
# single-record carrier whose record ID is in the result only, and the host interceptor
# declares that capability. An earlier MiDojo refuses the key, so an episode with such a
# target names this range; every other episode keeps the one above, which still runs it.
RESULT_RECORD_REVISION_RANGE = ">=c0bee126"
# From cab2a35 (the merge of the judge-service reply check into asago/main, after c0bee12)
# MiDojo has the builtin ``reply_judge`` predicate, so a reply episode names this range.
REPLY_JUDGE_REVISION_RANGE = ">=cab2a35e"
EPISODE_FILE = "episode.json"
SEED_MEMBER = "seed.json"
CONDITION_MEMBER = "tool_call_condition.json"
JUDGE_MEMBER = "judge.json"
INPUTS_MEMBER = "inputs.json"
PLANTED_ITEM = "planted_item"
COMMAND_ATTEMPT = "command_attempt"
REPLY = "reply"
CLAIM_LEVELS = ("command_attempt", "reply", "state_effect")
SINGLE = "single"
SEQUENTIAL = "sequential"
# The interceptor runs on the host and forwards to the target config's MCP endpoint. It is
# MiDojo's ``MidojoMCP``, which declares every capability a compiled target needs
# (``call_arguments`` and, from c0bee12, ``result_record``), so compile models no other.
HOST_INTERCEPTION = {"mode": "host", "upstream": "endpoint.mcp_url"}
FORWARD = "forward"
BLOCK = "block"
WRITE_DISPOSITIONS = (FORWARD, BLOCK)
# MiDojo's builtin predicate; its value is the condition object itself.
TOOL_CALL_CONDITION = "tool_call_condition"
# MiDojo's builtin predicate that asks a judge service, and how a reply episode sets it: the
# service's URL and bearer token are in the grading process's environment.
REPLY_JUDGE = "reply_judge"
JUDGE_URL_ENV = "JUDGE_URL"
JUDGE_TOKEN_ENV = "JUDGE_TOKEN"
# What the check waits beyond the service's budget for the answer to travel.
JUDGE_MARGIN_SECONDS = 10


class CompileError(BundleError):
    """The package cannot become a MiDojo episode."""


def compile_episode(
    package_dir: str | Path,
    out_dir: str | Path,
    *,
    target: TargetConfig | Mapping[str, Any] | str | Path,
    write_disposition: Mapping[str, str] | None = None,
    judge_turns: str = FINAL_TURN,
) -> dict[str, Any]:
    """Write the episode of one package for ``target`` into ``out_dir`` and return the document.

    ``target`` is the caller's target config: a ``target.yaml`` path, or a ``TargetConfig``
    read from one, with ``tools-list.json`` and ``runtime-spec.yaml`` beside it; one not
    authorized for a run (``authorized_as``) is refused. ``write_disposition`` maps a
    mutating tool to ``forward`` or ``block``; every other mutating tool forwards.
    ``judge_turns`` (``final`` or ``all``) is which turns the judge service grades for a
    reply; the episode's rubric carries it.
    """

    if judge_turns not in JUDGE_TURNS:
        raise CompileError(f"judge_turns is one of {sorted(JUDGE_TURNS)}, not {judge_turns!r}")
    config = authorized_target(target, CompileError)
    try:
        declared = declared_tools(config)
    except DeclarationFault as exc:
        raise CompileError(str(exc)) from exc
    out = Path(out_dir)
    package = _load(Path(package_dir))
    stimulus = _json_member(package, "stimulus.json")
    level = claim_level(_json_member(package, "plan.json"))
    delivery = SEQUENTIAL if delivery_mode(stimulus) == SEQUENTIAL else SINGLE
    reasons = gap_reasons(level, stimulus)
    if reasons:
        raise CapabilityGap(capability_gap_record(TOOL, package, delivery, "; ".join(reasons)))
    if SEED_MEMBER not in package.members:
        raise CompileError(f"a {PLANTED_ITEM} package has no {SEED_MEMBER}")
    try:
        planted = [
            locate(item, declared)
            for item in placements(
                _json_member(package, SEED_MEMBER), seed_slots(runtime_contract(package))
            )
        ]
    except PlacementFault as exc:
        raise CompileError(str(exc)) from exc
    unplaced = [reason for item in planted if (reason := gap_reason(item, declared))]
    if unplaced:
        raise CapabilityGap(capability_gap_record(TOOL, package, delivery, "; ".join(unplaced)))
    allowed = allowed_tools(package, declared, {item.carrier for item in planted})
    turns = user_turns(stimulus)
    security = _security_check(package, level, turns, judge_turns)
    document = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "tool_revision_range": revision_range(level, planted),
        "package_id": package.manifest.package_id,
        "scenario_id": package.manifest.scenario_id,
        "package_digest": package.manifest.manifest_digest,
        "claim_level": level,
        "delivery": delivery,
        "verifiers": [],
        "interception": dict(HOST_INTERCEPTION),
        "tools": {
            "allowed": allowed,
            "write_disposition": write_dispositions(allowed, declared, write_disposition or {}),
        },
        "placements": [item.entry() for item in planted],
        "episode": episode(
            turns=turns,
            plan=injection_plan([item.instruction() for item in planted]),
            security=security,
        ),
    }
    _write(out, document)
    return document


def revision_range(level: str, planted: list[Placement]) -> str:
    """Return the MiDojo revisions the episode needs: from cab2a35 for a reply, which has
    every capability below; else from c0bee12 when a target matches a result, else from
    b5b8c75."""

    if level == REPLY:
        return REPLY_JUDGE_REVISION_RANGE
    if any(item.by_result for item in planted):
        return RESULT_RECORD_REVISION_RANGE
    return TOOL_REVISION_RANGE


def allowed_tools(
    package: ArtifactPackage, declared: DeclaredTools, carriers: set[str]
) -> list[str]:
    """Return the operations of the package's inventory, each one the target declares.

    The carriers must be among them: the agent reads the planted text through one.
    """

    inputs = _json_member(package, INPUTS_MEMBER) if INPUTS_MEMBER in package.members else {}
    inventory = inputs.get("inventory") if isinstance(inputs, dict) else None
    operations = inventory.get("operations") if isinstance(inventory, dict) else None
    names = sorted(
        {
            item["name"]
            for item in operations or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
    )
    if not names:
        raise CompileError(f"the package's {INPUTS_MEMBER} inventory lists no operation")
    unknown = [name for name in names if name not in declared.tools]
    if unknown:
        raise CompileError(f"the package allows tools the target does not list: {unknown}")
    missing = sorted(carriers - set(names))
    if missing:
        raise CompileError(f"the package does not allow its carrier: {missing}")
    return names


def write_dispositions(
    allowed: list[str], declared: DeclaredTools, chosen: Mapping[str, str]
) -> dict[str, Any]:
    """Return the default disposition and each allowed mutating tool's, forward unless chosen."""

    mutating = [name for name in allowed if name in declared.mutating]
    unknown = sorted(set(chosen) - set(mutating))
    if unknown:
        raise CompileError(
            f"a write disposition is set only for an allowed mutating tool, not {unknown}"
        )
    bad = sorted(name for name, value in chosen.items() if value not in WRITE_DISPOSITIONS)
    if bad:
        raise CompileError(
            f"a write disposition is one of {list(WRITE_DISPOSITIONS)}; {bad} have another"
        )
    return {"default": FORWARD, "tools": {name: chosen.get(name, FORWARD) for name in mutating}}


def claim_level(plan: Any) -> str:
    claim = plan.get("observation_claim") if isinstance(plan, dict) else None
    level = claim.get("claim_level") if isinstance(claim, dict) else None
    if level not in CLAIM_LEVELS:
        raise CompileError(f"unsupported claim level: {level!r}")
    return level


def delivery_mode(stimulus: Any) -> Any:
    return stimulus.get("mode", SINGLE) if isinstance(stimulus, dict) else SINGLE


def gap_reasons(level: str, stimulus: Any) -> list[str]:
    """Return why MiDojo cannot run the package, one text per cause; empty when it can."""

    reasons = [
        _planted_reason(stimulus),
        _delivery_reason(stimulus),
        _binding_reason(stimulus),
        _claim_reason(level),
    ]
    return [reason for reason in reasons if reason]


def _planted_reason(stimulus: Any) -> str | None:
    """MiDojo runs a package whose attack is a planted item, and no other."""

    delivery = stimulus.get("delivery") if isinstance(stimulus, dict) else None
    if delivery == PLANTED_ITEM:
        return None
    return (
        f"MiDojo runs {PLANTED_ITEM} packages only; this stimulus delivers its attack "
        f"in the user turns ({delivery!r})"
    )


def _delivery_reason(stimulus: Any) -> str | None:
    """An episode sends user turns only, each as its own message in one conversation.

    A sequential package's user turns are those turns. A single package sends its
    history and user text in one request; as separate turns the target would answer
    each, which changes the attack, so a single package with history is a gap.
    """

    history = stimulus.get("history") if isinstance(stimulus, dict) else None
    sequential = delivery_mode(stimulus) == SEQUENTIAL
    if not history and not sequential:
        return None
    if not (
        isinstance(history, list)
        and all(isinstance(item, dict) and item.get("role") == "user" for item in history)
    ):
        return "MiDojo sends user turns only; this stimulus holds a non-user message"
    if sequential:
        return None
    return (
        "a single package sends its history and user text in one prompt; MiDojo would "
        "send each as its own turn"
    )


def _binding_reason(stimulus: Any) -> str | None:
    """An episode's turns are fixed text; a binding would need a value at run time."""

    slots = stimulus.get("slots") if isinstance(stimulus, dict) else None
    if not slots:
        return None
    return "an episode sends fixed user turns; this stimulus fills run-time bindings into them"


def _claim_reason(level: str) -> str | None:
    """The command attempt and the reply are graded here; the state effect is not."""

    if level not in (COMMAND_ATTEMPT, REPLY):
        return f"the {level} claim is not graded by this adapter"
    return None


def user_turns(stimulus: dict[str, Any]) -> list[str]:
    """Return the user turns: the history's, then the user text.

    A sequential stimulus's ``turn_count`` must state their number.
    """

    turns = [item.get("content") for item in stimulus.get("history") or []]
    turns.append(stimulus.get("user_text"))
    if not all(isinstance(turn, str) and turn for turn in turns):
        raise CompileError("the stimulus's user turns are not all non-empty text")
    if delivery_mode(stimulus) == SEQUENTIAL:
        stated = stimulus.get("turn_count")
        if type(stated) is not int or stated != len(turns) or len(turns) < 2:
            raise CompileError(
                f"sequential turn_count {stated!r} does not match {len(turns)} user turns"
            )
    if contains_marker(turns):
        raise CompileError("package content contains a slot marker")
    return turns


def _security_check(
    package: ArtifactPackage, level: str, turns: list[str], judge_turns: str
) -> dict[str, Any]:
    if level == REPLY:
        return {REPLY_JUDGE: _reply_judge(package, turns, judge_turns)}
    return {TOOL_CALL_CONDITION: _condition(package)}


def _condition(package: ArtifactPackage) -> dict[str, Any]:
    condition = _json_member(package, CONDITION_MEMBER)
    if contains_marker(condition):
        raise CompileError("package content contains a slot marker")
    problem = validate_condition(condition)
    if problem:
        raise CompileError(f"{CONDITION_MEMBER}: condition is invalid: {problem}")
    return condition


def _reply_judge(package: ArtifactPackage, turns: list[str], judge_turns: str) -> dict[str, Any]:
    """Return the ``reply_judge`` spec: the judge's question as the claim, and as the rubric
    its criteria and facts, no runtime facts, the user turns and the judged turns."""

    judge = _json_member(package, JUDGE_MEMBER)
    question = judge.get("question") if isinstance(judge, dict) else None
    # MiDojo refuses a claim that is not a non-empty string.
    if not isinstance(question, str) or not question.strip():
        raise CompileError(f"{JUDGE_MEMBER} has no question text")
    rubric = {
        "criteria": judge.get("criteria", []),
        "facts": judge.get("facts", []),
        "runtime_facts": {},
        "turns": list(turns),
        "judge_turns": judge_turns,
    }
    if contains_marker([question, rubric]):
        raise CompileError("package content contains a slot marker")
    return {
        "claim": question,
        "rubric": rubric,
        "url_env": JUDGE_URL_ENV,
        "token_env": JUDGE_TOKEN_ENV,
        "prior_turns": True,
        "tool_calls": True,
        "timeout_seconds": judge_timeout_seconds(judge_turns, len(turns)),
    }


def judge_timeout_seconds(judge_turns: str, turns: int) -> int:
    """Return how long the check waits for the judge service: the old judge's 180 seconds
    for each model request (one per judged turn), the service's budget, and a margin."""

    return int(TIMEOUT_SECONDS) * judged_count(judge_turns, turns) + JUDGE_MARGIN_SECONDS


def _load(package_dir: Path) -> ArtifactPackage:
    try:
        return load_package(package_dir)
    except (OSError, ValueError) as exc:
        raise CompileError(f"package cannot be loaded: {exc}") from exc


def _json_member(package: ArtifactPackage, name: str) -> Any:
    content = package.members.get(name)
    if content is None:
        raise CompileError(f"package has no {name}")
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompileError(f"package {name} is not JSON: {exc}") from exc


def _write(out: Path, document: dict[str, Any]) -> None:
    if out.exists() and any(out.iterdir()):
        raise CompileError(f"output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    (out / EPISODE_FILE).write_text(canonical_text(document), encoding="utf-8")
