"""Authoring policies that isolate one stage, shared by the orchestrator stage tests."""

from __future__ import annotations

from pathlib import Path

from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import AuthoringPolicy, AuthoringResult

from .support import ScriptedAuthoringTransport, world_builders

_inventory, _runtime_contract, _view = world_builders(
    "refund", "inventory", "runtime_contract", "view"
)

NO_REVIEW = AuthoringPolicy(
    plan_max_corrections=0,
    artifact_max_corrections=0,
    review_plan=False,
    review_artifact=False,
)
PLAN_REVIEW = AuthoringPolicy(
    plan_max_corrections=0,
    artifact_max_corrections=0,
    review_plan=True,
    review_artifact=False,
)


ONE_PLAN_CORRECTION = AuthoringPolicy(
    plan_max_corrections=1,
    artifact_max_corrections=0,
    review_plan=False,
    review_artifact=False,
)


def run_refund(
    tmp_path: Path,
    transport: ScriptedAuthoringTransport,
    policy: AuthoringPolicy = NO_REVIEW,
) -> AuthoringResult:
    return AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="stage-edges",
        policy=policy,
    ).run(_view(), _inventory(), _runtime_contract())
