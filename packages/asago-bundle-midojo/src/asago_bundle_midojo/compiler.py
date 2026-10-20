"""compile: turn an artifact package into a MiDojo bundle template.

The template is a suite directory, ``asago_suite``, that MiDojo loads by its
dotted name ``asago_suite.suite``; the suite file has slots for the prompt (or,
for a sequential package, one ``user_turn_<n>`` slot per user turn, which
``midojo-run`` sends in order in one conversation) and the verdict path, and
``bundle.json`` (``tool-bundle-v1``) names the values the orchestrator resolves
and the two commands: ``serve`` starts the control plane on loopback, and the
entrypoint runs ``midojo-run`` against it and reads the control plane's records
into the bundle. The agent runtime is ``unmanaged``: the
orchestrator hosts the target and MiDojo reaches it through the model gateway.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring.condition_eval import validate_condition
from asago_artifact_generator.package_io import ArtifactPackage, load_package
from asago_bundle_core.errors import BundleError
from asago_bundle_core.gap import CapabilityGap, capability_gap_record
from asago_bundle_core.slots import bundle_slot, contains_marker, template_slots, value_slot
from asago_bundle_core.text import canonical_text

from .bundle_files.asago_rubric import RUBRIC_NAME
from .bundle_files.asago_rubric_turns import TURNS_RUBRIC_NAME
from .bundle_files.verifier_names import NEUTRAL_NAME
from .environment import EnvironmentFault, base_target, suite_environment

BUNDLE_SCHEMA_VERSION = "tool-bundle-v1"
TOOL = "midojo"
TOOL_REVISION_RANGE = ">=5efb3de8"
# A user task's ``turns`` and each call's ``turn`` arrived on asago/main at 29d2b6e, and a
# verifier's ``VerificationContext.turns`` (the conversation the sequential judge reads) at
# bddb79b.
TURNS_REVISION_RANGE = ">=bddb79bc"
# MiDojo's builtin predicate; its value is the condition object itself.
TOOL_CALL_CONDITION = "tool_call_condition"
COMMAND_ATTEMPT = "command_attempt"
REPLY = "reply"
CLAIM_LEVELS = ("command_attempt", "reply", "state_effect")
SINGLE = "single"
SEQUENTIAL = "sequential"
USER_TURN_PREFIX = "user_turn_"
CONDITION_MEMBER = "tool_call_condition.json"
JUDGE_MEMBER = "judge.json"
BUNDLE_FILE = "bundle.json"
SUITE_TEMPLATE = "suite.template.json"
SEED_MEMBER = "seed.json"
SUITE_PACKAGE = "asago_suite"
SUITE_MODULE = f"{SUITE_PACKAGE}.suite"
SUITE_FILE = f"{SUITE_PACKAGE}/suite.yaml"
VERDICT_FILE = "verdict.json"
RUN_SCRIPT = "run_midojo.py"
MCP_SERVER_LABEL = "qualification_target"
USER_TASK_ID = "asago_user_task"
INJECTION_TASK_ID = "asago_injection_task"
CONTROL_URL = "http://127.0.0.1:{service_port}"
# The judge's key is read from the environment; the orchestrator's boundary judge adds the
# real one.
JUDGE_KEY = "OPENAICOMPATIBLE_API_KEY"
# Values the commands name as ``{key}``; the suite template holds the others.
ENTRYPOINT_VALUES = ("gateway_url", "mcp_url", "model", "service_port")
BUNDLE_FILES = "asago_bundle_midojo.bundle_files"
# Bundle path -> (package, file of that package it copies).
BUNDLE_CODE = {
    f"{SUITE_PACKAGE}/__init__.py": (BUNDLE_FILES, "suite_package.py"),
    f"{SUITE_PACKAGE}/suite.py": (BUNDLE_FILES, "suite_module.py"),
    f"{SUITE_PACKAGE}/asago_verifiers.py": (BUNDLE_FILES, "asago_verifiers.py"),
    f"{SUITE_PACKAGE}/asago_rubric.py": (BUNDLE_FILES, "asago_rubric.py"),
    RUN_SCRIPT: (BUNDLE_FILES, "run_midojo.py"),
    f"{SUITE_PACKAGE}/verifier_names.py": (BUNDLE_FILES, "verifier_names.py"),
}
# A sequential reply bundle also gets the rubric judge of the conversation, and a suite
# module that registers it; every other bundle's code stays ``BUNDLE_CODE``.
TURNS_CODE = {
    f"{SUITE_PACKAGE}/suite.py": (BUNDLE_FILES, "suite_module_turns.py"),
    f"{SUITE_PACKAGE}/asago_rubric_turns.py": (BUNDLE_FILES, "asago_rubric_turns.py"),
}
RUN_DUMP = "control-plane/run.json"
EVALUATION_DUMP = "control-plane/evaluation.json"
RESULTS_FILE = "midojo-logs/results.json"
COMMAND_OUTPUTS = (RUN_DUMP, EVALUATION_DUMP, RESULTS_FILE)
# Only the rubric verifier of a reply package writes a verdict file; a command attempt is graded
# by ``parse`` from MiDojo's recorded calls.
NATIVE_OUTPUTS = {COMMAND_ATTEMPT: COMMAND_OUTPUTS, REPLY: (*COMMAND_OUTPUTS, VERDICT_FILE)}


class CompileError(BundleError):
    """The package cannot become a MiDojo bundle."""


def compile_package(
    package_dir: str | Path, out_dir: str | Path, *, midojo_base: str | Path, domain: str
) -> dict[str, Any]:
    """Write the bundle template for one package and return its manifest.

    ``midojo_base`` is mini-agents' ``mini-agents-midojo-base-v1`` file; ``domain`` picks the
    ledger the suite serves and the slots the package's seed may target.
    """

    out = Path(out_dir)
    package = _load(Path(package_dir))
    stimulus = _json_member(package, "stimulus.json")
    level = claim_level(_json_member(package, "plan.json"))
    reasons = gap_reasons(level, stimulus)
    delivery = SEQUENTIAL if delivery_mode(stimulus) == SEQUENTIAL else SINGLE
    if reasons:
        raise CapabilityGap(capability_gap_record(TOOL, package, delivery, "; ".join(reasons)))
    turns = user_turn_count(stimulus)
    target = _base_target(Path(midojo_base), domain)
    seed = _json_member(package, SEED_MEMBER) if SEED_MEMBER in package.members else None
    try:
        environment, probes = suite_environment(target, seed)
    except EnvironmentFault as exc:
        raise CompileError(str(exc)) from exc
    suite = suite_template(package, level, environment, probes, turns)
    requires = sorted(template_slots([suite]) | set(ENTRYPOINT_VALUES))
    manifest = bundle_manifest(package, level, requires, delivery)
    _write(out, {SUITE_TEMPLATE: suite, BUNDLE_FILE: manifest}, bundle_code(level, delivery))
    return manifest


def bundle_code(level: str, delivery: str) -> dict[str, tuple[str, str]]:
    """Return the code a bundle of ``level`` and ``delivery`` copies (see ``BUNDLE_CODE``)."""

    if level == REPLY and delivery == SEQUENTIAL:
        return {**BUNDLE_CODE, **TURNS_CODE}
    return BUNDLE_CODE


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

    reasons = [_delivery_reason(stimulus), _claim_reason(level)]
    return [reason for reason in reasons if reason]


def _delivery_reason(stimulus: Any) -> str | None:
    """MiDojo sends user turns only, each as its own request in one conversation.

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


def user_turn_count(stimulus: Any) -> int | None:
    """Return a sequential stimulus's user turn count, or None for one prompt.

    The count is the history's user turns plus the final user text, and the
    stimulus's ``turn_count`` must state it.
    """

    if delivery_mode(stimulus) != SEQUENTIAL:
        return None
    count = len(stimulus["history"]) + 1
    stated = stimulus.get("turn_count")
    if type(stated) is not int or stated != count or count < 2:
        raise CompileError(f"sequential turn_count {stated!r} does not match {count} user turns")
    return count


def _claim_reason(level: str) -> str | None:
    """The command attempt and the reply are graded here; the state effect is not."""

    if level not in (COMMAND_ATTEMPT, REPLY):
        return f"the {level} claim is not graded by this adapter"
    return None


def suite_template(
    package: ArtifactPackage,
    level: str,
    environment: dict[str, Any],
    probes: dict[str, dict[str, str]],
    turns: int | None = None,
) -> dict[str, Any]:
    """Return the suite file with its slots: one user task and one injection task.

    A single package's user task has a ``prompt``; a sequential one's has ``turns``,
    each text a ``user_turn_<n>`` slot counted from 1.
    """

    scenario_id = package.manifest.scenario_id
    name, check = _security_check(package, level, turns)
    injection_task: dict[str, Any] = {
        "id": INJECTION_TASK_ID,
        "description": f"{scenario_id}: {_DESCRIPTIONS[level]}",
        "security": {name: check},
    }
    if probes:
        injection_task["probes"] = probes
    user_task: dict[str, Any] = {"id": USER_TASK_ID}
    if turns is None:
        user_task["prompt"] = value_slot("messages")
    else:
        user_task["turns"] = [value_slot(key) for key in user_turn_keys(turns)]
    return {
        "agent_runtime": {"type": "unmanaged"},
        "environment": environment,
        "user_tasks": [{**user_task, "utility": {NEUTRAL_NAME: {}}}],
        "injection_tasks": [injection_task],
    }


def user_turn_keys(turns: int) -> list[str]:
    return [f"{USER_TURN_PREFIX}{number}" for number in range(1, turns + 1)]


_DESCRIPTIONS = {
    COMMAND_ATTEMPT: "the target attempts the command",
    REPLY: "the target's reply answers the rubric question",
}


def _security_check(
    package: ArtifactPackage, level: str, turns: int | None = None
) -> tuple[str, dict[str, Any]]:
    """Return the predicate name and spec that grade the claim."""

    if level == REPLY:
        return RUBRIC_NAME if turns is None else TURNS_RUBRIC_NAME, {
            "rubric": _rubric(package),
            **_judged_text(turns),
            "judge_url": value_slot("judge_url"),
            "judge_model": value_slot("judge_model"),
            "verdict_file": bundle_slot(VERDICT_FILE),
        }
    return TOOL_CALL_CONDITION, _condition(package)


def _judged_text(turns: int | None) -> dict[str, Any]:
    """Return the user text the judge sees and, for a sequential package, the planned turns.

    A sequential spec names its user turns: ``user_text`` the last and ``history`` the
    earlier ones. The judge reads the conversation MiDojo recorded, checks that it holds
    those turns, and judges the turns ``judge_turns`` names (``final`` or ``all``), each
    with the earlier messages and replies as its history (``asago_rubric_turns``).
    """

    if turns is None:
        return {"user_text": value_slot("messages")}
    *earlier, last = user_turn_keys(turns)
    return {
        "user_text": value_slot(last),
        "history": [{"role": "user", "content": value_slot(key)} for key in earlier],
        "judge_turns": value_slot("judge_turns"),
    }


def bundle_manifest(
    package: ArtifactPackage, level: str, requires: list[str], delivery: str = SINGLE
) -> dict[str, Any]:
    reply = level == REPLY
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "tool": TOOL,
        "tool_revision_range": (
            TURNS_REVISION_RANGE if delivery == SEQUENTIAL else TOOL_REVISION_RANGE
        ),
        "package_id": package.manifest.package_id,
        "scenario_id": package.manifest.scenario_id,
        "package_digest": package.manifest.manifest_digest,
        "claim_level": level,
        "delivery": delivery,
        "plugins": [_rubric_name(delivery), NEUTRAL_NAME] if reply else [NEUTRAL_NAME],
        "requires": requires,
        "environment": ["OPENAI_API_KEY", JUDGE_KEY] if reply else ["OPENAI_API_KEY"],
        "templates": {"suite": SUITE_TEMPLATE},
        "serve": {
            "argv": [
                "{tool_python}",
                "-m",
                "midojo.serve",
                "--host",
                "127.0.0.1",
                "--port",
                "{service_port}",
                "--load-suite",
                SUITE_MODULE,
            ],
            "ready_url": f"{CONTROL_URL}/health",
        },
        "entrypoint": [
            "{tool_python}",
            f"{{bundle}}/{RUN_SCRIPT}",
            "--bundle",
            "{bundle}",
            "--control-url",
            CONTROL_URL,
            "--",
            "--agent-uri",
            "{gateway_url}",
            "--protocol",
            "openai",
            "--model-name",
            "{model}",
            "--mcp-server-url",
            "{mcp_url}",
            "--mcp-server-label",
            MCP_SERVER_LABEL,
            "--suite",
            SUITE_MODULE,
            "--logdir",
            "{bundle}/midojo-logs",
        ],
        "native_outputs": list(NATIVE_OUTPUTS[level]),
    }


def _rubric_name(delivery: str) -> str:
    return TURNS_RUBRIC_NAME if delivery == SEQUENTIAL else RUBRIC_NAME


def _base_target(path: Path, domain: str) -> dict[str, Any]:
    try:
        base = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompileError(f"the MiDojo base cannot be read: {exc}") from exc
    try:
        return dict(base_target(base, domain))
    except EnvironmentFault as exc:
        raise CompileError(str(exc)) from exc


def _condition(package: ArtifactPackage) -> dict[str, Any]:
    condition = _json_member(package, CONDITION_MEMBER)
    if contains_marker(condition):
        raise CompileError("package content contains a slot marker")
    problem = validate_condition(condition)
    if problem:
        raise CompileError(f"{CONDITION_MEMBER}: condition is invalid: {problem}")
    return condition


def _rubric(package: ArtifactPackage) -> dict[str, Any]:
    """Return the judge's rubric; the runtime facts are a slot the orchestrator fills."""

    judge = _json_member(package, JUDGE_MEMBER)
    if not isinstance(judge, dict) or not isinstance(judge.get("question"), str):
        raise CompileError(f"{JUDGE_MEMBER} has no question text")
    rubric = {
        "question": judge["question"],
        "criteria": judge.get("criteria", []),
        "facts": judge.get("facts", []),
    }
    if contains_marker(rubric):
        raise CompileError("package content contains a slot marker")
    return {**rubric, "runtime_facts": value_slot("judge_runtime_facts")}


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


def _write(out: Path, documents: dict[str, Any], code: dict[str, tuple[str, str]]) -> None:
    if out.exists() and any(out.iterdir()):
        raise CompileError(f"output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    (out / SUITE_PACKAGE).mkdir()
    for name, document in documents.items():
        (out / name).write_text(canonical_text(document), encoding="utf-8")
    write_bundle_code(out, code)


def write_bundle_code(out: Path, code: dict[str, tuple[str, str]] = BUNDLE_CODE) -> None:
    """Copy each file of ``code`` byte for byte to its bundle path under ``out``."""

    for name, (package, original) in code.items():
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files(package).joinpath(original).read_bytes())
