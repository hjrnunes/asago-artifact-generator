"""compile: turn an artifact package into a Garak bundle template.

The template is one conversation entry and one Garak run configuration, both
with slots for the values orch resolves at execute time, plus ``bundle.json``
(``tool-bundle-v1``), which names those values and the command that runs Garak
on the concrete bundle. ``injection.IndirectInjection`` sends a single-turn
package's messages in one request; ``injection.SequentialInjection`` sends a
sequential package's user turns one per request, each after the target's real
reply to the one before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from asago_artifact_generator.package_io import ArtifactPackage, load_package

from .slots import bundle_slot, contains_marker, template_slots, value_slot

BUNDLE_SCHEMA_VERSION = "tool-bundle-v1"
TOOL = "garak"
TOOL_REVISION_RANGE = ">=1c2918ae"
TARGET_MODE = "orch_hosted"
PROBES = {
    "single": "injection.IndirectInjection",
    "sequential": "injection.SequentialInjection",
}
DETECTORS = {
    "command_attempt": "toolcall.ToolCallCondition",
    "reply": "rubric_judge.RubricJudge",
}
CONDITION_MEMBER = "tool_call_condition.json"
JUDGE_MEMBER = "judge.json"
MCP_SERVER_LABEL = "qualification_target"
MAX_OUTPUT_TOKENS = 4096
TIMEOUT_SECONDS = 180.0
SINGLE = "single"
SEQUENTIAL = "sequential"
BUNDLE_FILE = "bundle.json"
CONVERSATION_TEMPLATE = "conversation.template.json"
RUN_TEMPLATE = "run.template.json"
CONVERSATIONS = "conversations.jsonl"
RUN_CONFIG = "run.yaml"
REPORTS = "reports"
USER_TURN_PREFIX = "user_turn_"
# Values the entrypoint names as ``{key}``; the templates hold the others.
ENTRYPOINT_VALUES = ("model",)


class CompileError(ValueError):
    """The package cannot become a Garak bundle."""


class CapabilityGap(Exception):
    """Garak cannot run the package yet; ``record`` says why."""

    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(record["reason"])
        self.record = record


def compile_package(package_dir: str | Path, out_dir: str | Path) -> dict[str, Any]:
    """Write the bundle template for one package and return its manifest.

    Raises ``CapabilityGap`` before writing anything when Garak cannot deliver
    the package, and ``CompileError`` for a package it cannot read.
    """

    out = Path(out_dir)
    package = _load(Path(package_dir))
    stimulus = _json_member(package, "stimulus.json")
    gap = delivery_gap(package, stimulus)
    if gap is not None:
        raise CapabilityGap(gap)
    level = claim_level(_json_member(package, "plan.json"))
    turns = user_turn_count(stimulus)
    delivery = SINGLE if turns is None else SEQUENTIAL
    scenario_id = package.manifest.scenario_id
    conversation = conversation_template(package, level, turns)
    run = run_template(level, scenario_id, delivery)
    requires = sorted(template_slots([conversation, run]) | set(ENTRYPOINT_VALUES))
    manifest = bundle_manifest(package, level, requires, delivery)
    _write(out, {CONVERSATION_TEMPLATE: conversation, RUN_TEMPLATE: run, BUNDLE_FILE: manifest})
    return manifest


def delivery_mode(stimulus: Any) -> Any:
    return stimulus.get("mode", SINGLE) if isinstance(stimulus, dict) else SINGLE


def delivery_gap(package: ArtifactPackage, stimulus: Any) -> dict[str, Any] | None:
    """Return the capability gap for a delivery Garak cannot send, else None.

    ``SequentialInjection`` sends only user turns, each after the target's
    real reply; a sequential history holding any other message has no turn
    it could send.
    """

    if delivery_mode(stimulus) != SEQUENTIAL:
        return None
    history = stimulus.get("history")
    if isinstance(history, list) and all(
        isinstance(item, dict) and item.get("role") == "user" for item in history
    ):
        return None
    return {
        "kind": "capability_gap",
        "tool": TOOL,
        "package_id": package.manifest.package_id,
        "scenario_id": package.manifest.scenario_id,
        "package_digest": package.manifest.manifest_digest,
        "delivery": SEQUENTIAL,
        "reason": (
            "injection.SequentialInjection sends user turns only; this "
            "sequential history holds another message"
        ),
    }


def user_turn_count(stimulus: Any) -> int | None:
    """Return a sequential stimulus's user turn count, or None for one request.

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


def claim_level(plan: Any) -> str:
    claim = plan.get("observation_claim") if isinstance(plan, dict) else None
    level = claim.get("claim_level") if isinstance(claim, dict) else None
    if level not in DETECTORS:
        raise CompileError(f"unsupported claim level: {level!r}")
    return level


def conversation_template(
    package: ArtifactPackage, level: str, turns: int | None = None
) -> dict[str, Any]:
    """Return the conversation entry with its value slots.

    A sequential entry holds one user message per turn, each text a
    ``user_turn_<n>`` slot counted from 1.
    """

    entry: dict[str, Any] = {
        "messages": value_slot("messages") if turns is None else user_turn_messages(turns),
        "tools": [
            {
                "type": "mcp",
                "server_label": MCP_SERVER_LABEL,
                "server_url": value_slot("mcp_url"),
                "require_approval": "never",
                "allowed_tools": value_slot("allowed_tools"),
            }
        ],
    }
    if level == "command_attempt":
        entry["tool_call_condition"] = _slot_free(_json_member(package, CONDITION_MEMBER))
    else:
        entry["judge_rubric"] = judge_rubric(_json_member(package, JUDGE_MEMBER))
    return entry


def user_turn_messages(turns: int) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": value_slot(f"{USER_TURN_PREFIX}{number}")}
        for number in range(1, turns + 1)
    ]


def judge_rubric(judge: Any) -> dict[str, Any]:
    """Return the rubric orch builds today, with the runtime facts as a slot."""

    if not isinstance(judge, dict):
        raise CompileError(f"{JUDGE_MEMBER} is not an object")
    rubric = {
        "question": judge.get("question"),
        "criteria": judge.get("criteria", []),
        "facts": judge.get("facts", []),
    }
    return {**_slot_free(rubric), "runtime_facts": value_slot("judge_runtime_facts")}


def run_template(level: str, scenario_id: str, delivery: str = SINGLE) -> dict[str, Any]:
    """Return the Garak run configuration with its slots.

    A sequential reply bundle takes ``judge_turns`` (``final`` or ``all``);
    the tool-call condition runs after every turn and needs none.
    """

    probe = PROBES[delivery].split(".")[1]
    options: dict[str, Any] = {"conversation_source": bundle_slot(CONVERSATIONS)}
    if delivery == SEQUENTIAL and level == "reply":
        options["judge_turns"] = value_slot("judge_turns")

    plugins: dict[str, Any] = {
        "generators": {
            "openai": {
                "OpenAIResponsesGenerator": {
                    "uri": value_slot("gateway_url"),
                    "max_tokens": MAX_OUTPUT_TOKENS,
                    "extra_params": {"tool_choice": "auto"},
                    "max_retries": 0,
                    "client_options": {"timeout": TIMEOUT_SECONDS},
                }
            }
        },
        "probes": {"injection": {probe: options}},
    }
    if level == "reply":
        plugins["detectors"] = {"rubric_judge": {"RubricJudge": _judge_model()}}
    return {
        "system": {"lite": False, "parallel_attempts": False, "parallel_requests": False},
        "run": {"generations": 1},
        "reporting": {"report_dir": bundle_slot(REPORTS), "report_prefix": scenario_id},
        "plugins": plugins,
    }


def _judge_model() -> dict[str, Any]:
    return {
        "detector_model_type": "openai.OpenAICompatible",
        "detector_model_name": value_slot("judge_model"),
        "detector_model_config": {
            "uri": value_slot("judge_url"),
            "max_retries": 0,
            "client_options": {"timeout": TIMEOUT_SECONDS},
        },
    }


def bundle_manifest(
    package: ArtifactPackage, level: str, requires: list[str], delivery: str = SINGLE
) -> dict[str, Any]:
    detector = DETECTORS[level]
    probe = PROBES[delivery]
    scenario_id = package.manifest.scenario_id
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "tool": TOOL,
        "tool_revision_range": TOOL_REVISION_RANGE,
        "package_id": package.manifest.package_id,
        "scenario_id": scenario_id,
        "package_digest": package.manifest.manifest_digest,
        "claim_level": level,
        "delivery": delivery,
        "target_mode": TARGET_MODE,
        "plugins": [probe, detector],
        "requires": requires,
        "environment": _environment(level),
        "templates": {"conversation": CONVERSATION_TEMPLATE, "run": RUN_TEMPLATE},
        "entrypoint": [
            "{tool_python}",
            "-m",
            "garak",
            "--config",
            f"{{bundle}}/{RUN_CONFIG}",
            "--target_type",
            "openai.OpenAIResponsesGenerator",
            "--target_name",
            "{model}",
            "--probes",
            probe,
            "--detectors",
            detector,
        ],
        "native_outputs": [f"{REPORTS}/{scenario_id}.report.jsonl"],
    }


def _environment(level: str) -> list[str]:
    """Name the variables the entrypoint reads; orch sets placeholder keys."""

    names = ["OPENAI_API_KEY"]
    if level == "reply":
        names.append("OPENAICOMPATIBLE_API_KEY")
    return names


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


def _slot_free(content: Any) -> Any:
    if contains_marker(content):
        raise CompileError("package content contains a slot marker")
    return content


def _write(out: Path, documents: dict[str, Any]) -> None:
    if out.exists() and any(out.iterdir()):
        raise CompileError(f"output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    for name, document in documents.items():
        (out / name).write_text(canonical_text(document), encoding="utf-8")


def canonical_text(document: Any) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
