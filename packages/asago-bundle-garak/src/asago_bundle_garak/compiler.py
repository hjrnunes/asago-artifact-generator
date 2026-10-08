"""compile: turn an artifact package into a Garak bundle template.

The template is one ``IndirectInjection`` conversation entry and one Garak run
configuration, both with slots for the values orch resolves at execute time,
plus ``bundle.json`` (``tool-bundle-v1``), which names those values and the
command that runs Garak on the concrete bundle.
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
PROBE = "injection.IndirectInjection"
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
    scenario_id = package.manifest.scenario_id
    conversation = conversation_template(package, level)
    run = run_template(level, scenario_id)
    requires = sorted(template_slots([conversation, run]) | set(ENTRYPOINT_VALUES))
    manifest = bundle_manifest(package, level, requires)
    _write(out, {CONVERSATION_TEMPLATE: conversation, RUN_TEMPLATE: run, BUNDLE_FILE: manifest})
    return manifest


def delivery_gap(package: ArtifactPackage, stimulus: Any) -> dict[str, Any] | None:
    """Return the capability gap for a delivery Garak cannot send, else None.

    ``IndirectInjection`` sends fixed messages in one request, so it cannot
    carry a sequential exchange in which every later turn follows the
    target's real reply.
    """

    mode = stimulus.get("mode", SINGLE) if isinstance(stimulus, dict) else SINGLE
    if mode != SEQUENTIAL:
        return None
    return {
        "kind": "capability_gap",
        "tool": TOOL,
        "package_id": package.manifest.package_id,
        "scenario_id": package.manifest.scenario_id,
        "package_digest": package.manifest.manifest_digest,
        "delivery": SEQUENTIAL,
        "reason": (
            "injection.IndirectInjection sends fixed messages; a sequential "
            "package needs each later turn sent after the target's real reply"
        ),
    }


def claim_level(plan: Any) -> str:
    claim = plan.get("observation_claim") if isinstance(plan, dict) else None
    level = claim.get("claim_level") if isinstance(claim, dict) else None
    if level not in DETECTORS:
        raise CompileError(f"unsupported claim level: {level!r}")
    return level


def conversation_template(package: ArtifactPackage, level: str) -> dict[str, Any]:
    """Return the conversation entry with its value slots."""

    entry: dict[str, Any] = {
        "messages": value_slot("messages"),
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


def run_template(level: str, scenario_id: str) -> dict[str, Any]:
    """Return the Garak run configuration with its slots."""

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
        "probes": {
            "injection": {"IndirectInjection": {"conversation_source": bundle_slot(CONVERSATIONS)}}
        },
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


def bundle_manifest(package: ArtifactPackage, level: str, requires: list[str]) -> dict[str, Any]:
    detector = DETECTORS[level]
    scenario_id = package.manifest.scenario_id
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "tool": TOOL,
        "tool_revision_range": TOOL_REVISION_RANGE,
        "package_id": package.manifest.package_id,
        "scenario_id": scenario_id,
        "package_digest": package.manifest.manifest_digest,
        "claim_level": level,
        "delivery": SINGLE,
        "target_mode": TARGET_MODE,
        "plugins": [PROBE, detector],
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
            PROBE,
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
