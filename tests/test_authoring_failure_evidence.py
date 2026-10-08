"""Offline tests for the failure evidence the two-call authoring seam persists."""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

import pytest

from asago_artifact_generator.authoring.core import (
    TransportResponse,
)

from .support import (
    HANDOFF,
    ScriptedAuthoringTransport,
    load_failure_evidence,
    stage_local_orchestrator,
    unreviewed_policy,
    world_builders,
)

_inventory_v2, _runtime_contract_v2 = world_builders("refund", "inventory", "runtime_contract")

(_view,) = world_builders("refund-minimal", "view")


@pytest.mark.parametrize(
    ("response", "expected_code", "expected_status"),
    [
        (
            TransportResponse(raw=b'{"broken":', usage={"prompt_tokens": 7}),
            "invalid_json",
            "unresolved",
        ),
        (
            TransportResponse(raw=b"{}", usage={"prompt_tokens": 8}),
            "plan_validation",
            "unresolved",
        ),
        (RuntimeError("provider unavailable"), "transport_failure", "transport_failure"),
    ],
    ids=["malformed-json", "schema-invalid", "provider-failure"],
)
def test_failed_authoring_persists_reloadable_evidence_before_discarding_response(
    tmp_path: Path,
    response: object,
    expected_code: str,
    expected_status: str,
) -> None:
    package_dir = tmp_path / "package"
    transport = ScriptedAuthoringTransport([response])

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=package_dir,
        task_id="durable-failure",
        policy=unreviewed_policy(plan_max_corrections=0),
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == expected_status
    assert len(transport.requests) == 1
    evidence_path = package_dir.with_name("package.failure-evidence.json")
    assert result.failure_evidence_path == evidence_path
    saved = load_failure_evidence(evidence_path)
    assert saved["status"] == expected_status
    assert saved["task_id"] == "durable-failure"
    assert saved["attempts"]
    first = saved["attempts"][0]
    assert first["prompt"]["system"] == transport.requests[0]["system"]
    assert first["prompt"]["user"] == transport.requests[0]["user"]
    assert first["controls"]["availability"] == "available"
    assert first["controls"]["value"]["max_retries"] == 0
    assert first["findings"][0]["code"] == expected_code
    assert saved["findings"] == first["findings"]
    assert saved["terminal"]["stage"] == "plan"
    assert saved["terminal"]["attempt_index"] == 0
    assert saved["terminal"]["reason"] == expected_code

    if isinstance(response, TransportResponse):
        assert first["raw_response"]["availability"] == "available"
        assert base64.b64decode(first["raw_response"]["base64"]) == response.raw
        assert first["usage"]["availability"] == "available"
        assert first["usage"]["value"] == response.usage
    else:
        assert first["raw_response"]["availability"] == "unavailable"
        assert first["raw_response"]["reason"] == "provider_failure"
        assert first["usage"]["availability"] == "unavailable"

    assert not package_dir.exists()
    assert not list(tmp_path.glob("*.failure-evidence.json.tmp"))


def test_failure_evidence_redacts_endpoint_and_secret_metadata_without_losing_controls(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    response = TransportResponse(
        raw=b"{}",
        usage={"prompt_tokens": 3},
        controls={
            "temperature": 0.0,
            "max_retries": 0,
            "base_url": "https://private.invalid/v1",
            "api_key": "do-not-persist",
        },
    )
    result = stage_local_orchestrator(
        transport=ScriptedAuthoringTransport([response]),
        package_dir=package_dir,
        task_id="safe-failure",
        policy=unreviewed_policy(plan_max_corrections=0),
    ).run(_view(), _inventory_v2(), _runtime_contract_v2())

    assert result.status == "unresolved"
    evidence_path = package_dir.with_name("package.failure-evidence.json")
    persisted = evidence_path.read_text(encoding="utf-8")
    assert "private.invalid" not in persisted
    assert "do-not-persist" not in persisted
    saved = load_failure_evidence(evidence_path)
    controls = saved["attempts"][0]["controls"]["value"]
    assert controls["temperature"] == 0.0
    assert controls["max_retries"] == 0
    assert controls["base_url"] == "<redacted>"
    assert controls["api_key"] == "<redacted>"


def test_failure_evidence_reloads_after_authoring_process_exits(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    script = """
import sys
from pathlib import Path
from tests.support import (
    ScriptedAuthoringTransport,
    stage_local_orchestrator,
    unreviewed_policy,
)
from asago_artifact_generator.input_adapter import InputKind, load_input

source, destination = map(Path, sys.argv[1:3])
view = load_input(source)
inventory = {
    "operations": [],
    "facts": [{"ref": "fact:one", "value": True, "schema": {"type": "boolean"}}],
    "source_handles": [{"ref": "scenario:constraint", "meaning": "constraint"}],
}
contract = {
    "delivery": ["direct_user_message"],
    "observation": {},
    "setup_permissions": [],
    "limits": {"max_turns": 1},
}
result = stage_local_orchestrator(
    transport=ScriptedAuthoringTransport([b'{"broken":']),
    package_dir=destination,
    task_id="process-exit",
    policy=unreviewed_policy(plan_max_corrections=0),
).run(view, inventory, contract)
raise SystemExit(0 if result.status == "unresolved" else 1)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(HANDOFF), str(package_dir)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    saved = load_failure_evidence(package_dir.with_name("package.failure-evidence.json"))
    assert saved["status"] == "unresolved"
    assert saved["attempts"][0]["raw_response"]["availability"] == "available"
    assert saved["attempts"][0]["findings"][0]["code"] == "invalid_json"
