"""Immutable package assembly from accepted responses, plus blocked-plan persistence."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from ..contract_kit import ClaimLevel
from ..input_adapter import InputView
from ..package_io import (
    TOOL_CALL_CONDITION_MEMBER,
    ArtifactPackage,
    build_package,
    tool_call_condition_bytes,
)
from .checks import _plan_claim_level
from .core import AUTHORING_INTERFACE_VERSION_V2, _json_bytes
from .inventory import _expected_authoring_input_pins, _resolved_judge_spec
from .planted_item import seed_members
from .prompt_safety import assert_no_secrets
from .reply_examples import package_examples
from .sequential_turns import stamped_stimulus


def _package_from_responses(
    *,
    view: InputView,
    plan: dict[str, Any],
    artifact: dict[str, Any],
    task_id: str,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    discovery_provenance: dict[str, Any] | None = None,
    review_status: dict[str, str] | None = None,
    terminal_status: str | None = None,
) -> ArtifactPackage:
    """Assemble the package; its requests, ledger, and reviews stay in the failure evidence."""

    claim_level = _plan_claim_level(plan)
    members = {
        "plan.json": _json_bytes(plan),
        "stimulus.json": _json_bytes(stamped_stimulus(view, artifact["stimulus"])),
        "setup.json": _json_bytes(artifact["setup_recipe"]),
        "bindings.json": _json_bytes(artifact["runtime_bindings"]),
        "prerequisites.json": _json_bytes(artifact["prerequisites"]),
        "checks.json": _json_bytes(
            {"interface": AUTHORING_INTERFACE_VERSION_V2, "status": "structurally_valid"}
        ),
        "inputs.json": _json_bytes(
            {
                "inventory": inventory,
                "discovery_provenance": deepcopy(discovery_provenance or {}),
                "runtime_contract": runtime_contract,
                "authoring_input_pins": _expected_authoring_input_pins(
                    input_view=view,
                    inventory=inventory,
                    runtime_contract=runtime_contract,
                ),
            }
        ),
        "observations.json": _json_bytes(artifact["required_observations"]),
        "explanation.json": _json_bytes({"text": artifact["explanation"]}),
        "examples.json": _json_bytes(
            package_examples(artifact["examples"], reply_claim=claim_level == ClaimLevel.REPLY)
        ),
    }
    members.update(seed_members(view, artifact, runtime_contract, plan))
    resolved_judge = _resolved_judge_spec(artifact["semantic_judge_spec"], inventory)
    if resolved_judge is not None and claim_level == ClaimLevel.REPLY:
        members["judge.json"] = _json_bytes(resolved_judge)
    condition = view.tool_call_condition
    if condition is not None and claim_level == ClaimLevel.COMMAND_ATTEMPT:
        members[TOOL_CALL_CONDITION_MEMBER] = tool_call_condition_bytes(condition)
    authoring_summary = _authoring_summary(review_status, terminal_status)
    assert_no_secrets({"authoring": authoring_summary})
    package_id = f"{task_id}-{view.scenario_id}"
    return build_package(
        package_id=package_id,
        scenario_id=view.scenario_id,
        input_kind=view.kind.value,
        source_digests=view.source_digests or {"input": view.source_sha256},
        members=members,
        authoring=authoring_summary,
        runtime_capabilities=runtime_contract,
    )


def _authoring_summary(
    review_status: dict[str, str] | None, terminal_status: str | None
) -> dict[str, Any]:
    """Return the manifest's authoring outcome, the fields downstream admission reads."""

    summary: dict[str, Any] = {}
    if review_status is not None and "artifact" in review_status:
        summary["review_status"] = {"artifact": review_status["artifact"]}
    if terminal_status is not None:
        summary["status"] = terminal_status
        summary["terminal_status"] = terminal_status
    return summary


def _persist_blocked_plan(destination: Path, plan: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = destination.with_suffix(destination.suffix + ".blocked.json")
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_bytes(
        _json_bytes({"status": "blocked", "plan": plan, "package_path": str(destination)})
    )
    temporary.replace(target)
