"""Offline regression test for zero correction allowances."""

from __future__ import annotations

from pathlib import Path

from .support import ScriptedAuthoringTransport, stage_local_orchestrator, unreviewed_policy
from .test_versioned_authoring_wire import _inventory, _runtime_contract, _view


def test_zero_corrections_failed_call1_stops_without_a_correction_contact(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([b"{}"])

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "call1",
        task_id="call1",
        policy=unreviewed_policy(plan_max_corrections=0, artifact_max_corrections=0),
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    assert [request["stage"] for request in transport.requests] == ["call1"]
    assert [record["stage"] for record in result.ledger] == ["call1"]
