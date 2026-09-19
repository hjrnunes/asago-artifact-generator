"""Offline regression test for the internal no-correction seam."""

from __future__ import annotations

from pathlib import Path

from asago_artifact_generator.authoring import (
    AuthoringOrchestrator,
    ScriptedAuthoringTransport,
)

from .test_authoring_orchestration import _contract, _inventory, _view


def test_no_correction_failed_call1_stops_without_a_correction_contact(tmp_path: Path) -> None:
    transport = ScriptedAuthoringTransport([b"{}"])

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "call1",
        task_id="call1",
        correction_allowed=False,
    ).run(_view(), _inventory(), _contract())

    assert result.status == "failed"
    assert [request["stage"] for request in transport.requests] == ["call1"]
    assert [record["stage"] for record in result.ledger] == ["call1"]
