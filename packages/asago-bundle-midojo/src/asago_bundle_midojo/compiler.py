"""compile: turn an artifact package into a MiDojo bundle template.

The template is a suite directory, ``asago_suite``, that MiDojo loads by its
dotted name ``asago_suite.suite``; the suite file has slots for the prompt and
the verdict path, and ``bundle.json`` (``tool-bundle-v1``) names the values orch
resolves and the two commands: ``serve`` starts the control plane on loopback,
and the entrypoint runs ``midojo-run`` against it and reads the control plane's
records into the bundle. The agent runtime is ``unmanaged``: orch hosts the
target and MiDojo reaches it through the model gateway.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from asago_artifact_generator.package_io import ArtifactPackage, load_package
from asago_bundle_core.errors import BundleError
from asago_bundle_core.gap import CapabilityGap, capability_gap_record
from asago_bundle_core.slots import bundle_slot, contains_marker, template_slots, value_slot
from asago_bundle_core.text import canonical_text

from .bundle_files.asago_rubric import RUBRIC_NAME
from .bundle_files.asago_verifiers import (
    CONDITION_NAME,
    NEUTRAL_NAME,
    validate_condition,
)

BUNDLE_SCHEMA_VERSION = "tool-bundle-v1"
TOOL = "midojo"
TOOL_REVISION_RANGE = ">=9ceb22e3"
TARGET_MODE = "orch_hosted"
COMMAND_ATTEMPT = "command_attempt"
REPLY = "reply"
CLAIM_LEVELS = ("command_attempt", "reply", "state_effect")
SINGLE = "single"
SEQUENTIAL = "sequential"
CONDITION_MEMBER = "tool_call_condition.json"
JUDGE_MEMBER = "judge.json"
BUNDLE_FILE = "bundle.json"
SUITE_TEMPLATE = "suite.template.json"
SUITE_PACKAGE = "asago_suite"
SUITE_MODULE = f"{SUITE_PACKAGE}.suite"
SUITE_FILE = f"{SUITE_PACKAGE}/suite.yaml"
VERDICT_FILE = "verdict.json"
RUN_SCRIPT = "run_midojo.py"
MCP_SERVER_LABEL = "qualification_target"
USER_TASK_ID = "asago_user_task"
INJECTION_TASK_ID = "asago_injection_task"
CONTROL_URL = "http://127.0.0.1:{service_port}"
# The judge's key is read from the environment; orch's boundary judge adds the real one.
JUDGE_KEY = "OPENAICOMPATIBLE_API_KEY"
# Values the commands name as ``{key}``; the suite template holds the others.
ENTRYPOINT_VALUES = ("gateway_url", "mcp_url", "model", "service_port")
# Bundle path -> file of ``bundle_files`` it copies.
BUNDLE_CODE = {
    f"{SUITE_PACKAGE}/__init__.py": "suite_package.py",
    f"{SUITE_PACKAGE}/suite.py": "suite_module.py",
    f"{SUITE_PACKAGE}/asago_verifiers.py": "asago_verifiers.py",
    f"{SUITE_PACKAGE}/asago_rubric.py": "asago_rubric.py",
    RUN_SCRIPT: "run_midojo.py",
}
RUN_DUMP = "control-plane/run.json"
EVALUATION_DUMP = "control-plane/evaluation.json"
RESULTS_FILE = "midojo-logs/results.json"
NATIVE_OUTPUTS = (RUN_DUMP, EVALUATION_DUMP, RESULTS_FILE, VERDICT_FILE)


class CompileError(BundleError):
    """The package cannot become a MiDojo bundle."""


def compile_package(package_dir: str | Path, out_dir: str | Path) -> dict[str, Any]:
    """Write the bundle template for one package and return its manifest."""

    out = Path(out_dir)
    package = _load(Path(package_dir))
    stimulus = _json_member(package, "stimulus.json")
    level = claim_level(_json_member(package, "plan.json"))
    reasons = gap_reasons(level, stimulus)
    if reasons:
        delivery = SEQUENTIAL if delivery_mode(stimulus) == SEQUENTIAL else SINGLE
        raise CapabilityGap(capability_gap_record(TOOL, package, delivery, "; ".join(reasons)))
    suite = suite_template(package, level)
    requires = sorted(template_slots([suite]) | set(ENTRYPOINT_VALUES))
    manifest = bundle_manifest(package, level, requires)
    _write(out, {SUITE_TEMPLATE: suite, BUNDLE_FILE: manifest})
    return manifest


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
    """MiDojo sends one user prompt per evaluation, so any other history is a gap."""

    if delivery_mode(stimulus) == SEQUENTIAL:
        return "MiDojo sends one prompt per evaluation; this package holds several user turns"
    history = stimulus.get("history") if isinstance(stimulus, dict) else None
    if not history:
        return None
    if all(isinstance(item, dict) and item.get("role") == "user" for item in history):
        return "MiDojo sends one prompt per evaluation; this stimulus holds earlier user turns"
    return "MiDojo sends one user prompt; this stimulus holds a non-user message"


def _claim_reason(level: str) -> str | None:
    """The command attempt and the reply are graded here; the state effect is not."""

    if level not in (COMMAND_ATTEMPT, REPLY):
        return f"the {level} claim is not graded by this adapter"
    return None


def suite_template(package: ArtifactPackage, level: str) -> dict[str, Any]:
    """Return the suite file with its slots: one user task and one injection task."""

    scenario_id = package.manifest.scenario_id
    name, check = _security_check(package, level)
    return {
        "agent_runtime": {"type": "unmanaged"},
        "user_tasks": [
            {
                "id": USER_TASK_ID,
                "prompt": value_slot("messages"),
                "utility": {NEUTRAL_NAME: {}},
            }
        ],
        "injection_tasks": [
            {
                "id": INJECTION_TASK_ID,
                "description": f"{scenario_id}: {_DESCRIPTIONS[level]}",
                "security": {name: check},
            }
        ],
    }


_DESCRIPTIONS = {
    COMMAND_ATTEMPT: "the target attempts the command",
    REPLY: "the target's reply answers the rubric question",
}


def _security_check(package: ArtifactPackage, level: str) -> tuple[str, dict[str, Any]]:
    """Return the verifier name and spec that grade the claim; the verdict path is a slot."""

    verdict_file = bundle_slot(VERDICT_FILE)
    if level == REPLY:
        return RUBRIC_NAME, {
            "rubric": _rubric(package),
            "user_text": value_slot("messages"),
            "judge_url": value_slot("judge_url"),
            "judge_model": value_slot("judge_model"),
            "verdict_file": verdict_file,
        }
    return CONDITION_NAME, {"condition": _condition(package), "verdict_file": verdict_file}


def bundle_manifest(package: ArtifactPackage, level: str, requires: list[str]) -> dict[str, Any]:
    reply = level == REPLY
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "tool": TOOL,
        "tool_revision_range": TOOL_REVISION_RANGE,
        "package_id": package.manifest.package_id,
        "scenario_id": package.manifest.scenario_id,
        "package_digest": package.manifest.manifest_digest,
        "claim_level": level,
        "delivery": SINGLE,
        "target_mode": TARGET_MODE,
        "plugins": [RUBRIC_NAME if reply else CONDITION_NAME, NEUTRAL_NAME],
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
        "native_outputs": list(NATIVE_OUTPUTS),
    }


def _condition(package: ArtifactPackage) -> dict[str, Any]:
    condition = _json_member(package, CONDITION_MEMBER)
    if contains_marker(condition):
        raise CompileError("package content contains a slot marker")
    problem = validate_condition(condition)
    if problem:
        raise CompileError(f"{CONDITION_MEMBER}: condition is invalid: {problem}")
    return condition


def _rubric(package: ArtifactPackage) -> dict[str, Any]:
    """Return the judge's rubric; the runtime facts are a slot orch fills."""

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


def _write(out: Path, documents: dict[str, Any]) -> None:
    if out.exists() and any(out.iterdir()):
        raise CompileError(f"output directory is not empty: {out}")
    source = files("asago_bundle_midojo").joinpath("bundle_files")
    out.mkdir(parents=True, exist_ok=True)
    (out / SUITE_PACKAGE).mkdir()
    for name, document in documents.items():
        (out / name).write_text(canonical_text(document), encoding="utf-8")
    for name, original in BUNDLE_CODE.items():
        (out / name).write_bytes(source.joinpath(original).read_bytes())
