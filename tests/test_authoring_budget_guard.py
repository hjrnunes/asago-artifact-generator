"""Offline regression tests for explicit baseline-only authoring budgets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import (
    AuthoringBudget,
    AuthoringOrchestrator,
    ScriptedAuthoringTransport,
    load_failure_evidence,
)

from .test_authoring_orchestration import _artifact, _contract, _inventory, _plan, _view


def test_default_failed_call1_keeps_the_shared_correction_behavior(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([b"{}", json.dumps(_plan()), json.dumps(_artifact())])

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "default",
        task_id="default",
    ).run(_view(), _inventory(), _contract())

    assert result.status == "packaged"
    assert [request["stage"] for request in transport.requests] == [
        "call1",
        "correction",
        "call2",
    ]


def test_no_correction_failed_call1_stops_without_a_correction_contact(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([b"{}", json.dumps(_plan())])

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "call1",
        task_id="call1",
        correction_allowed=False,
    ).run(_view(), _inventory(), _contract())

    assert result.status == "failed"
    assert [request["stage"] for request in transport.requests] == ["call1"]
    assert [record["stage"] for record in result.ledger] == ["call1"]
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["aggregate"] == {"spent_before": 0, "limit": 16, "spent_after": 1}
    assert evidence["correction_allowed"] is False


def test_no_correction_failed_call2_stops_without_a_correction_contact(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan()), b"{}"])

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "call2",
        task_id="call2",
        correction_allowed=False,
    ).run(_view(), _inventory(), _contract())

    assert result.status == "failed"
    assert [request["stage"] for request in transport.requests] == ["call1", "call2"]
    assert [record["stage"] for record in result.ledger] == ["call1", "call2"]


def test_two_request_task_limit_blocks_a_third_transport_contact(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan()), b"{}", json.dumps(_artifact())])
    budget = AuthoringBudget(aggregate_limit=26, task_limit=2)

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "limited",
        task_id="limited",
        budget=budget,
    ).run(_view(), _inventory(), _contract())

    assert result.status == "failed"
    assert [request["stage"] for request in transport.requests] == ["call1", "call2"]
    assert budget.total_dispatched == 2
    assert any(finding.code == "correction_dispatch_failed" for finding in result.findings)
    evidence = load_failure_evidence(result.failure_evidence_path)
    assert evidence["aggregate"]["spent_after"] == 2


def test_explicit_aggregate_spent_is_preserved_and_incremented(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan()), json.dumps(_artifact())])
    budget = AuthoringBudget(
        aggregate_limit=26,
        task_limit=2,
        total_dispatched=20,
    )

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "seeded",
        task_id="seeded",
        budget=budget,
        correction_allowed=False,
    ).run(_view(), _inventory(), _contract())

    assert result.status == "packaged"
    assert budget.total_dispatched == 22
    assert result.package is not None
    assert result.package.manifest.authoring["aggregate"] == {
        "spent_before": 20,
        "limit": 26,
        "spent_after": 22,
    }
    assert result.package.manifest.authoring["task"] == {
        "spent_before": 0,
        "limit": 2,
        "spent_after": 2,
    }
    assert result.package.manifest.authoring["correction_allowed"] is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"task_limit": -1}, "task_limit"),
        ({"aggregate_limit": -1}, "aggregate_limit"),
        ({"total_dispatched": -1}, "total_dispatched"),
        ({"task_limit": 4}, "task_limit"),
        ({"aggregate_limit": 27}, "aggregate_limit"),
        ({"aggregate_limit": 26, "total_dispatched": 27}, "total_dispatched"),
    ],
)
def test_invalid_budget_values_fail_before_authoring(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        AuthoringBudget(**kwargs)
