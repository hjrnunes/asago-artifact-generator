"""Offline tests for exact generated-detector execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.detector_runtime import (
    CLAIM_LEVELS,
    DetectorRuntimeError,
    execute_detector,
    validate_detector_result,
)
from asago_artifact_generator.package_io import build_package, write_package
from asago_artifact_generator.reporting import garak_value


def _package(tmp_path: Path, source: str, name: str = "package") -> Path:
    package = build_package(
        package_id=name,
        scenario_id="scenario-1",
        input_kind="reference-task",
        source_digests={"input": "a" * 64},
        members={"detector.py": source.encode("utf-8")},
        runtime_capabilities={"detector": {"timeout_seconds": 3}},
    )
    return write_package(tmp_path / name, package)


def _detector(outcome: str = "detected", ref: str = "tool_calls[0]") -> str:
    return f"""
def evaluate(evidence: dict) -> dict:
    return {{
        "outcome": {outcome!r},
        "reason": "fixture result",
        "evidence_refs": [{ref!r}],
        "claim_level": "command_attempt",
    }}
"""


def _evidence() -> dict:
    return {
        "tool_calls": [
            {
                "native_id": "call-1",
                "name": "process_refund",
                "arguments": {"order_id": "ord-1", "amount": 11},
            }
        ],
        "availability": {"tool_calls": "captured"},
    }


def test_execute_detector_uses_exact_source_and_resolves_evidence_reference(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path, _detector())

    execution = execute_detector(package, _evidence())

    assert execution.status == "completed"
    assert execution.result is not None
    assert execution.result["outcome"] == "detected"
    assert execution.result["evidence_refs"] == ["tool_calls[0]"]
    assert execution.detector_sha256 == execution.detector_sha256_after
    assert execution.package_digest == execution.package_digest_after
    assert execution.docker_argv[0] == "/usr/local/bin/docker"
    assert "--network" in execution.docker_argv
    assert "none" in execution.docker_argv


def test_repackaged_detector_bytes_change_verdict(tmp_path: Path) -> None:
    detected = _package(tmp_path, _detector("detected"), "detected")
    safe = _package(tmp_path, _detector("not_detected"), "safe")

    first = execute_detector(detected, _evidence())
    second = execute_detector(safe, _evidence())

    assert first.result["outcome"] == "detected"
    assert second.result["outcome"] == "not_detected"
    assert first.detector_sha256 != second.detector_sha256


@pytest.mark.parametrize(
    "source, message",
    [
        ("def evaluate(evidence):\n    return {}\n", "outcome"),
        (
            "def evaluate(evidence):\n"
            "    return {'outcome': 'not_detected', 'reason': 'safe', "
            "'evidence_refs': ['missing'], 'claim_level': 'reply'}\n",
            "does not resolve",
        ),
        (
            "def evaluate(evidence):\n"
            "    return {'outcome': 'detected', 'reason': '', "
            "'evidence_refs': ['tool_calls[0]'], 'claim_level': 'reply'}\n",
            "reason",
        ),
    ],
)
def test_invalid_detector_result_is_runtime_failure(
    tmp_path: Path, source: str, message: str
) -> None:
    execution = execute_detector(_package(tmp_path, source), _evidence())

    assert execution.status == "failed"
    assert execution.result is None
    assert execution.failure is not None
    assert message in execution.failure
    assert garak_value(execution) is None


def test_missing_code_is_runtime_failure_without_fallback(tmp_path: Path) -> None:
    package = _package(tmp_path, _detector())
    (package / "detector.py").unlink()

    execution = execute_detector(package, _evidence())

    assert execution.status == "failed"
    assert execution.result is None
    assert garak_value(execution) is None


def test_timeout_is_killed_and_never_becomes_safe(tmp_path: Path) -> None:
    source = """
import time
def evaluate(evidence: dict) -> dict:
    time.sleep(30)
    return {
        "outcome": "not_detected",
        "reason": "late",
        "evidence_refs": ["tool_calls[0]"],
        "claim_level": "command_attempt",
    }
"""
    execution = execute_detector(_package(tmp_path, source), _evidence(), timeout_seconds=1)

    assert execution.status == "timeout"
    assert execution.result is None
    assert garak_value(execution) is None


def test_network_and_source_write_are_denied_by_runtime(tmp_path: Path) -> None:
    source = """
import pathlib
import socket
def evaluate(evidence: dict) -> dict:
    pathlib.Path("/package/detector.py").write_text("changed")
    socket.create_connection(("example.invalid", 80), timeout=1)
    return {
        "outcome": "detected",
        "reason": "bad",
        "evidence_refs": ["tool_calls[0]"],
        "claim_level": "command_attempt",
    }
"""
    execution = execute_detector(_package(tmp_path, source), _evidence())

    assert execution.status == "failed"
    assert execution.result is None
    assert execution.failure
    assert garak_value(execution) is None


def test_network_access_is_denied_by_runtime(tmp_path: Path) -> None:
    source = """
import socket
def evaluate(evidence: dict) -> dict:
    socket.create_connection(("example.invalid", 80), timeout=1)
    return {
        "outcome": "detected",
        "reason": "network",
        "evidence_refs": ["tool_calls[0]"],
        "claim_level": "command_attempt",
    }
"""
    execution = execute_detector(_package(tmp_path, source), _evidence())

    assert execution.status == "failed"
    assert execution.result is None
    assert execution.failure
    assert execution.failure.startswith("detector runtime error:")


def test_package_source_write_is_denied_by_read_only_mount(tmp_path: Path) -> None:
    source = """
import pathlib
def evaluate(evidence: dict) -> dict:
    pathlib.Path("/package/detector.py").write_text("changed")
    return {
        "outcome": "detected",
        "reason": "write",
        "evidence_refs": ["tool_calls[0]"],
        "claim_level": "command_attempt",
    }
"""
    execution = execute_detector(_package(tmp_path, source), _evidence())

    assert execution.status == "failed"
    assert execution.result is None
    assert execution.failure.startswith("detector runtime error:")


def test_detector_output_is_bounded(tmp_path: Path) -> None:
    source = """
def evaluate(evidence: dict) -> dict:
    print("x" * 100000)
    return {
        "outcome": "detected",
        "reason": "too loud",
        "evidence_refs": ["tool_calls[0]"],
        "claim_level": "command_attempt",
    }
"""
    execution = execute_detector(_package(tmp_path, source), _evidence())

    assert execution.status == "failed"
    assert execution.result is None
    assert "output exceeded" in execution.failure


def test_credential_environment_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASAGO_RUNTIME_SECRET", "must-not-cross")
    source = """
import os
def evaluate(evidence: dict) -> dict:
    if os.environ.get("ASAGO_RUNTIME_SECRET"):
        raise RuntimeError("credential leaked")
    return {
        "outcome": "not_detected",
        "reason": "no credential",
        "evidence_refs": ["tool_calls[0]"],
        "claim_level": "command_attempt",
    }
"""
    execution = execute_detector(_package(tmp_path, source), _evidence())

    assert execution.status == "completed"
    assert execution.result["outcome"] == "not_detected"


def test_validate_result_accepts_all_closed_claim_levels() -> None:
    for claim_level in CLAIM_LEVELS:
        result = {
            "outcome": "inconclusive",
            "reason": "missing coverage",
            "evidence_refs": [],
            "claim_level": claim_level,
        }
        assert validate_detector_result(result, {"availability": {}}) == result


def test_validate_result_resolves_nested_evidence_reference() -> None:
    result = {
        "outcome": "detected",
        "reason": "command is present",
        "evidence_refs": ["tool_calls[0].native_id"],
        "claim_level": "command_attempt",
    }

    assert validate_detector_result(result, _evidence()) == result


def test_validate_result_rejects_non_mapping_and_unknown_outcome() -> None:
    with pytest.raises(DetectorRuntimeError):
        validate_detector_result([], _evidence())
    with pytest.raises(DetectorRuntimeError):
        validate_detector_result(
            {
                "outcome": "maybe",
                "reason": "unknown",
                "evidence_refs": [],
                "claim_level": "reply",
            },
            _evidence(),
        )


def test_reporting_mapping_is_only_for_completed_rich_results(tmp_path: Path) -> None:
    assert garak_value({"outcome": "detected"}) == 1
    assert garak_value({"outcome": "not_detected"}) == 0
    assert garak_value({"outcome": "inconclusive"}) is None
    assert garak_value({"status": "failed", "outcome": "not_detected"}) is None
    assert garak_value(DetectorRuntimeError("boom")) is None


def test_check_command_runs_offline_package_and_returns_rich_result(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    package = _package(tmp_path, _detector())
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_evidence()), encoding="utf-8")

    result = CliRunner().invoke(
        __import__("asago_artifact_generator.cli", fromlist=["app"]).app,
        ["check", str(package), "--evidence", str(evidence_path)],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "completed"
    assert payload["result"]["outcome"] == "detected"
    assert payload["garak_value"] == 1
