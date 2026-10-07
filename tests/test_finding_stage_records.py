from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import AuthoringPolicy

from .policy_support import NO_REVIEW, ONE_PLAN_CORRECTION, PLAN_REVIEW, run_refund
from .support import ScriptedAuthoringTransport, load_failure_evidence, world_builders

_framed, _inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract", "view"
)

_ONE_ARTIFACT_CORRECTION = AuthoringPolicy(
    plan_max_corrections=0,
    artifact_max_corrections=1,
    review_plan=False,
    review_artifact=False,
)
_HUGE = {"junk": "x" * 1_100_000}

# One scenario per route that records a finding: the policy and the scripted responses.
_SCENARIOS: dict[str, tuple[AuthoringPolicy, Callable[[], list[Any]]]] = {
    "plan_validation": (NO_REVIEW, lambda: [b"{}"]),
    "artifact_validation": (NO_REVIEW, lambda: [json.dumps(_plan()), _framed({"junk": 1})]),
    "plan_transport_failure": (NO_REVIEW, lambda: [RuntimeError("outage")]),
    "correction_transport_failure": (
        ONE_PLAN_CORRECTION,
        lambda: [b"{}", RuntimeError("outage")],
    ),
    "plan_correction_overflow": (ONE_PLAN_CORRECTION, lambda: [json.dumps(_HUGE)]),
    "artifact_correction_overflow": (
        _ONE_ARTIFACT_CORRECTION,
        lambda: [json.dumps(_plan()), _framed(_HUGE)],
    ),
    "plan_review_unavailable": (PLAN_REVIEW, lambda: [json.dumps(_plan()), b"not json"]),
    "plan_review_transport_failure": (
        PLAN_REVIEW,
        lambda: [json.dumps(_plan()), RuntimeError("outage")],
    ),
}


def _stages(document: dict[str, Any]) -> list[str | None]:
    records = [
        *document["findings"],
        *(finding for attempt in document["attempts"] for finding in attempt["findings"]),
    ]
    return [record.get("stage") for record in records]


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_every_finding_record_in_the_evidence_names_the_stage_that_raised_it(
    tmp_path: Path, name: str
) -> None:
    policy, responses = _SCENARIOS[name]

    result = run_refund(tmp_path, ScriptedAuthoringTransport(responses()), policy)

    stages = _stages(load_failure_evidence(result.failure_evidence_path))
    assert stages
    assert set(stages) <= {"plan", "artifact"}


@pytest.mark.parametrize(
    ("name", "stage"),
    [
        ("plan_validation", "plan"),
        ("artifact_validation", "artifact"),
        ("plan_correction_overflow", "plan"),
        ("artifact_correction_overflow", "artifact"),
        ("plan_review_unavailable", "plan"),
    ],
)
def test_a_finding_record_names_the_stage_of_the_response_it_concerns(
    tmp_path: Path, name: str, stage: str
) -> None:
    policy, responses = _SCENARIOS[name]

    result = run_refund(tmp_path, ScriptedAuthoringTransport(responses()), policy)

    document = load_failure_evidence(result.failure_evidence_path)
    assert set(_stages(document)) == {stage}
    assert document["terminal"]["stage"] == stage


def test_a_package_write_failure_is_recorded_at_the_artifact_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(destination: Path, package: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr("asago_artifact_generator.authoring.orchestrator.write_package", disk_full)
    transport = ScriptedAuthoringTransport([json.dumps(_plan()), _framed()])

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="write-failure",
        policy=NO_REVIEW,
    ).run(_view(), _inventory(), _runtime_contract())

    assert [(f.code, f.stage) for f in result.findings] == [("package_write_failed", "artifact")]
    document = load_failure_evidence(result.failure_evidence_path)
    assert document["findings"][0]["stage"] == "artifact"
