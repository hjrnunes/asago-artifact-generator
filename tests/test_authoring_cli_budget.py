"""Offline CLI tests for caller-supplied authoring budget controls."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from asago_artifact_generator import cli
from asago_artifact_generator.authoring import AuthoringResult

from .test_authoring_orchestration import HANDOFF, _contract, _inventory


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    inventory = tmp_path / "inventory.json"
    runtime = tmp_path / "runtime.json"
    inventory.write_text(json.dumps(_inventory()), encoding="utf-8")
    runtime.write_text(json.dumps(_contract()), encoding="utf-8")
    return HANDOFF, inventory, runtime


def test_invalid_cli_budget_is_rejected_before_transport_construction(
    tmp_path: Path, monkeypatch
) -> None:
    source, inventory, runtime = _inputs(tmp_path)
    constructed = False

    def unexpected_transport(**kwargs):
        nonlocal constructed
        constructed = True
        raise AssertionError("transport must not be constructed")

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", unexpected_transport)
    result = CliRunner().invoke(
        cli.app,
        [
            "author",
            str(source),
            "--inventory",
            str(inventory),
            "--runtime-contract",
            str(runtime),
            "--task-dispatch-limit",
            "2",
            "--aggregate-dispatch-limit",
            "26",
            "--aggregate-spent",
            "27",
        ],
    )

    assert result.exit_code != 0
    assert constructed is False
    assert "maximum of 26" in result.output.lower()


def test_cli_passes_generic_baseline_controls_to_orchestrator(tmp_path: Path, monkeypatch) -> None:
    source, inventory, runtime = _inputs(tmp_path)
    captured: dict[str, object] = {}

    class FakeTransport:
        max_retries = 0

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, view, inventory_data, runtime_data):
            return AuthoringResult(status="failed", task_id="baseline")

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", lambda **kwargs: FakeTransport())
    monkeypatch.setattr(cli, "AuthoringOrchestrator", FakeOrchestrator)
    result = CliRunner().invoke(
        cli.app,
        [
            "author",
            str(source),
            "--inventory",
            str(inventory),
            "--runtime-contract",
            str(runtime),
            "--task-id",
            "baseline-task",
            "--task-dispatch-limit",
            "2",
            "--aggregate-dispatch-limit",
            "26",
            "--aggregate-spent",
            "20",
            "--no-correction",
        ],
    )

    assert result.exit_code == 1
    budget = captured["budget"]
    assert budget.aggregate_limit == 26
    assert budget.task_limit == 2
    assert budget.total_dispatched == 20
    assert captured["correction_allowed"] is False
