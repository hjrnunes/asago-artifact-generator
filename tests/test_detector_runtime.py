"""Offline tests for exact generated-detector execution."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from asago_artifact_generator import detector_runtime
from asago_artifact_generator.detector_runtime import (
    CLAIM_LEVELS,
    DOCKER,
    DetectorRuntimeError,
    execute_detector,
    normalize_evidence_packet,
    resolve_docker_path,
    validate_detector_result,
)
from asago_artifact_generator.package_io import build_package, write_package
from asago_artifact_generator.reporting import garak_value


def _package(
    tmp_path: Path,
    source: str,
    name: str = "package",
    *,
    judge_enabled: bool = False,
) -> Path:
    members = {"detector.py": source.encode("utf-8")}
    if judge_enabled:
        members["judge.json"] = b'{"question":"fixture"}'
    package = build_package(
        package_id=name,
        scenario_id="scenario-1",
        input_kind="scenario-handoff-v1",
        source_digests={"input": "a" * 64},
        members=members,
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


def test_normalize_evidence_packet_projects_a_valid_judge_without_audit_fields() -> None:
    evidence = {
        "messages": [
            {"id": "reply-1", "role": "assistant", "content": "confirmed"},
        ],
        "availability": {"messages": "captured"},
        "completeness": {"messages": "complete"},
        "judge": {
            "verdict": "supported",
            "evidence_refs": ["messages[0].content"],
            "reason": "judge_completed",
            "request": {"question": "private"},
            "output": {"verdict": "supported"},
            "dispatched": True,
            "reused": False,
        },
    }

    normalized = normalize_evidence_packet(evidence, judge_enabled=True)

    assert normalized["judge"] == {
        "verdict": "supported",
        "evidence_refs": ["messages[0].content"],
        "reason": "judge_completed",
    }
    assert "request" not in normalized["judge"]
    assert "output" not in normalized["judge"]
    assert "dispatched" not in normalized["judge"]
    assert normalized["messages"] == evidence["messages"]


@pytest.mark.parametrize(
    ("judge", "expected"),
    [
        (None, {"verdict": "unresolved", "evidence_refs": [], "reason": "judge_missing"}),
        (
            {"verdict": "supported", "evidence_refs": "messages[0]"},
            {
                "verdict": "unresolved",
                "evidence_refs": [],
                "reason": "judge_evidence_invalid",
            },
        ),
        (
            {"verdict": "supported", "evidence_refs": ["messages[99]"]},
            {
                "verdict": "unresolved",
                "evidence_refs": [],
                "reason": "judge_support_unresolved",
            },
        ),
        (
            {"verdict": "supported", "evidence_refs": ["messages[0]"]},
            {
                "verdict": "unresolved",
                "evidence_refs": [],
                "reason": "judge_support_unresolved",
            },
        ),
    ],
)
def test_normalize_evidence_packet_makes_invalid_judge_inconclusive(
    judge: dict | None, expected: dict
) -> None:
    evidence = {
        "messages": [{"id": "reply-1", "role": "assistant", "content": None}],
        "availability": {"messages": "captured"},
        "completeness": {"messages": "complete"},
    }
    if judge is not None:
        evidence["judge"] = judge

    normalized = normalize_evidence_packet(evidence, judge_enabled=True)

    assert normalized["judge"] == expected


def test_normalize_evidence_packet_removes_judge_when_package_does_not_enable_it() -> None:
    evidence = {
        "judge": {
            "verdict": "supported",
            "evidence_refs": ["messages[0]"],
            "request": {"private": True},
        },
        "messages": [],
    }

    normalized = normalize_evidence_packet(evidence, judge_enabled=False)

    assert "judge" not in normalized
    assert evidence["judge"]["request"] == {"private": True}


def test_normalize_evidence_packet_matches_shared_conformance_fixture() -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "contracts"
            / "artifact-package"
            / "judge-normalization-v1"
            / "cases.json"
        ).read_text(encoding="utf-8")
    )

    for case in fixture["cases"]:
        normalized = normalize_evidence_packet(
            case["evidence"],
            judge_enabled=case["judge_enabled"],
        )
        if case["expected_judge"] is None:
            assert "judge" not in normalized, case["name"]
        else:
            assert normalized["judge"] == case["expected_judge"], case["name"]


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
    assert execution.docker_argv[0] == resolve_docker_path()
    assert "--network" in execution.docker_argv
    assert "none" in execution.docker_argv


def _patch_docker_lookup(
    monkeypatch: pytest.MonkeyPatch, *, executable: bool, on_path: str | None
) -> None:
    monkeypatch.setattr(
        detector_runtime,
        "os",
        SimpleNamespace(access=lambda path, mode: executable, X_OK=os.X_OK),
    )
    monkeypatch.setattr(detector_runtime, "shutil", SimpleNamespace(which=lambda name: on_path))


def test_docker_path_prefers_the_documented_location_when_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_docker_lookup(monkeypatch, executable=True, on_path="/usr/bin/docker")

    assert resolve_docker_path() == DOCKER


def test_docker_path_falls_back_to_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_docker_lookup(monkeypatch, executable=False, on_path="/usr/bin/docker")

    assert resolve_docker_path() == "/usr/bin/docker"


def test_docker_path_names_the_documented_location_when_docker_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_docker_lookup(monkeypatch, executable=False, on_path=None)

    assert resolve_docker_path() == DOCKER


def test_execute_detector_injects_only_normalized_judge_projection(
    tmp_path: Path,
) -> None:
    source = """
def evaluate(evidence: dict) -> dict:
    assert set(evidence["judge"]) == {"verdict", "evidence_refs", "reason"}
    return {
        "outcome": "detected",
        "reason": evidence["judge"]["reason"],
        "evidence_refs": ["messages[0]"],
        "claim_level": "reply",
    }
"""
    evidence = {
        "messages": [{"id": "reply-1", "role": "assistant", "content": "confirmed"}],
        "availability": {"messages": "captured"},
        "completeness": {"messages": "complete"},
        "judge": {
            "verdict": "supported",
            "evidence_refs": ["messages[0]"],
            "reason": "judge_completed",
            "request": {"private": True},
            "output": {"private": True},
            "dispatched": True,
        },
    }

    execution = execute_detector(
        _package(tmp_path, source, judge_enabled=True),
        evidence,
    )

    assert execution.status == "completed"
    assert execution.result["reason"] == "judge_completed"


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


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (b"\xff\xfe", "invalid detector.py:"),
        (b"def evaluate(:\n", "invalid detector.py:"),
        (b"def other(evidence):\n    return {}\n", "detector.py must define evaluate"),
        (
            b"async def evaluate(evidence):\n    return {}\n",
            "detector.py evaluate must be synchronous",
        ),
        (b"def evaluate():\n    return {}\n", "detector.py evaluate must accept exactly evidence"),
        (
            b"def evaluate(evidence, extra):\n    return {}\n",
            "detector.py evaluate must accept exactly evidence",
        ),
        (
            b"def evaluate(packet):\n    return {}\n",
            "detector.py evaluate must accept exactly evidence",
        ),
        (
            b"def evaluate(evidence, *rest):\n    return {}\n",
            "detector.py evaluate must accept exactly evidence",
        ),
    ],
)
def test_source_interface_requires_one_synchronous_evaluate_of_evidence(
    source: bytes, message: str
) -> None:
    with pytest.raises(DetectorRuntimeError) as raised:
        detector_runtime._validate_source_interface(source)

    assert str(raised.value).startswith(message)


def test_source_interface_accepts_positional_only_evidence() -> None:
    detector_runtime._validate_source_interface(b"def evaluate(evidence, /):\n    return {}\n")


def _result(**changes: object) -> dict:
    result = {
        "outcome": "detected",
        "reason": "command is present",
        "evidence_refs": ["tool_calls[0]"],
        "claim_level": "command_attempt",
    }
    result.update(changes)
    return result


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            {"outcome": "detected"},
            "detector result is missing fields: ['claim_level', 'evidence_refs', 'reason']",
        ),
        ({**_result(), "extra": 1}, "detector result has unknown fields: ['extra']"),
        (_result(outcome=1), "detector result has invalid outcome: 1"),
        (_result(reason="  "), "detector result reason must be nonblank"),
        (_result(reason=None), "detector result reason must be nonblank"),
        (_result(evidence_refs="tool_calls"), "detector result evidence_refs must be nonblank"),
        (_result(evidence_refs=[" "]), "detector result evidence_refs must be nonblank"),
        (
            _result(outcome="not_detected", evidence_refs=[]),
            "detected and not_detected results require evidence_refs",
        ),
        (
            _result(evidence_refs=["tool_calls[4]"]),
            "evidence reference 'tool_calls[4]' does not resolve: missing path segment '4'",
        ),
        (_result(claim_level="belief"), "detector result has invalid claim level: 'belief'"),
        (_result(claim_level=None), "detector result has invalid claim level: None"),
    ],
)
def test_validate_result_names_the_first_contract_violation(result: dict, message: str) -> None:
    with pytest.raises(DetectorRuntimeError) as raised:
        validate_detector_result(result, _evidence())

    assert str(raised.value) == message or str(raised.value).startswith(message)


def test_validate_result_requires_an_evidence_object() -> None:
    with pytest.raises(DetectorRuntimeError, match="detector result must be an object"):
        validate_detector_result(_result(), [])  # type: ignore[arg-type]


def _fake_docker(tmp_path: Path, body: str) -> str:
    """Write a stand-in Docker CLI whose ``run`` executes ``body``; ``rm`` succeeds."""

    import sys
    import textwrap

    script = tmp_path / "fake-docker"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys, time\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['rm']:\n"
        "    sys.exit(0)\n"
        "package = next(\n"
        "    pathlib.Path(arg.split(',')[1].removeprefix('src='))\n"
        "    for arg in args\n"
        "    if arg.startswith('type=bind,') and ',dst=/package,' in arg\n"
        ")\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


_OK_RESULT = json.dumps(
    {
        "status": "ok",
        "result": {
            "outcome": "detected",
            "reason": "fixture result",
            "evidence_refs": ["tool_calls[0]"],
            "claim_level": "command_attempt",
        },
    }
)


def test_execute_detector_returns_a_validated_result_from_the_runner(tmp_path: Path) -> None:
    docker = _fake_docker(tmp_path, f"sys.stdout.write({_OK_RESULT!r})\n")
    package = _package(tmp_path, _detector())

    execution = execute_detector(package, _evidence(), docker_path=docker)

    assert execution.status == "completed"
    assert execution.failure is None
    assert execution.result["outcome"] == "detected"
    assert execution.docker_argv[0] == docker
    assert execution.package_digest == execution.package_digest_after
    assert execution.detector_sha256 == execution.detector_sha256_after
    assert execution.stdout == _OK_RESULT.encode("utf-8")


@pytest.mark.parametrize(
    ("body", "status", "failure"),
    [
        ("sys.stdout.write('not json')\n", "failed", "detector runtime protocol error: "),
        (
            'sys.stdout.write(\'{"status": "error", "error": "boom"}\')\n',
            "failed",
            "detector runtime error: boom",
        ),
        ("sys.stdout.write('[1]')\n", "failed", "detector container failed with exit code 0"),
        (
            'sys.stderr.write(\'{"error": "crashed"}\')\nsys.exit(3)\n',
            "failed",
            "detector runtime error: crashed",
        ),
        ("sys.exit(5)\n", "failed", "detector container failed with exit code 5"),
        (
            'sys.stdout.write(\'{"status": "ok", "result": {}}\')\n',
            "failed",
            "detector result is missing fields:",
        ),
        (
            "sys.stdout.write('x' * 70000)\n",
            "failed",
            "detector output exceeded the runtime bound",
        ),
        ("time.sleep(5)\n", "timeout", "detector exceeded 0.5s wall-clock timeout"),
    ],
)
def test_execute_detector_reports_runner_failures_without_a_result(
    tmp_path: Path, body: str, status: str, failure: str
) -> None:
    docker = _fake_docker(tmp_path, body)
    package = _package(tmp_path, _detector())

    execution = execute_detector(package, _evidence(), docker_path=docker, timeout_seconds=0.5)

    assert execution.status == status
    assert execution.result is None
    assert execution.failure.startswith(failure)
    assert execution.docker_argv[0] == docker
    assert execution.package_digest == execution.package_digest_after
    assert execution.detector_sha256 == execution.detector_sha256_after


def test_execute_detector_reports_a_package_corrupted_during_the_run(tmp_path: Path) -> None:
    docker = _fake_docker(
        tmp_path,
        "(package / 'detector.py').write_text('# changed\\n')\n"
        f"sys.stdout.write({_OK_RESULT!r})\n",
    )
    package = _package(tmp_path, _detector())

    execution = execute_detector(package, _evidence(), docker_path=docker)

    assert execution.status == "failed"
    assert execution.failure.startswith("package changed during detector execution: ")
    assert execution.package_digest_after is None
    assert execution.detector_sha256_after is None


def test_execute_detector_reports_a_package_replaced_during_the_run(tmp_path: Path) -> None:
    docker = _fake_docker(
        tmp_path,
        "from asago_artifact_generator.package_io import build_package, write_package\n"
        "write_package(package, build_package(\n"
        "    package_id='package', scenario_id='scenario-1',\n"
        "    input_kind='scenario-handoff-v1', source_digests={'input': 'a' * 64},\n"
        "    members={'detector.py': b'def evaluate(evidence):\\n    return {}\\n'},\n"
        "    runtime_capabilities={'detector': {'timeout_seconds': 3}},\n"
        "))\n"
        f"sys.stdout.write({_OK_RESULT!r})\n",
    )
    package = _package(tmp_path, _detector())

    execution = execute_detector(package, _evidence(), docker_path=docker)

    assert execution.status == "failed"
    assert execution.failure == "package or detector digest changed during execution"
    assert execution.package_digest_after != execution.package_digest
    assert execution.detector_sha256_after != execution.detector_sha256


def test_execute_detector_reports_an_unavailable_runtime(tmp_path: Path) -> None:
    package = _package(tmp_path, _detector())

    execution = execute_detector(
        package, _evidence(), docker_path=str(tmp_path / "no-such-docker")
    )

    assert execution.status == "failed"
    assert execution.failure.startswith("detector runtime unavailable: ")
    assert execution.stdout == b""
    assert execution.package_digest == execution.package_digest_after


def test_execute_detector_rejects_inputs_before_starting_a_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _fake_docker(tmp_path, "raise SystemExit('the container must not start')\n")
    package = _package(tmp_path, _detector())
    reads_secret = _package(
        tmp_path,
        "def evaluate(evidence):\n    return evidence['secret']\n",
        name="reads-secret",
    )

    not_object = execute_detector(package, [], docker_path=docker)  # type: ignore[arg-type]
    undeclared = execute_detector(reads_secret, _evidence(), docker_path=docker)
    monkeypatch.setattr(detector_runtime, "MAX_EVIDENCE_BYTES", 10)
    too_large = execute_detector(package, _evidence(), docker_path=docker)

    assert not_object.failure == "evidence packet must be an object"
    assert undeclared.failure.startswith("detector reads undeclared evidence root 'secret'")
    assert too_large.failure == "evidence exceeds the runtime input bound"
    for execution in (not_object, undeclared, too_large):
        assert execution.status == "failed"
        assert execution.docker_argv == ()
        assert execution.package_digest is not None
        assert execution.package_digest == execution.package_digest_after
        assert execution.detector_sha256 == execution.detector_sha256_after


def test_execute_detector_needs_a_persisted_package_and_a_positive_timeout(
    tmp_path: Path,
) -> None:
    built = build_package(
        package_id="package",
        scenario_id="scenario-1",
        input_kind="scenario-handoff-v1",
        source_digests={"input": "a" * 64},
        members={"detector.py": _detector().encode("utf-8")},
        runtime_capabilities={"detector": {"timeout_seconds": 3}},
    )

    in_memory = execute_detector(built, _evidence())
    absent = execute_detector(tmp_path / "absent", _evidence())

    assert in_memory.failure == "execution requires a persisted package directory"
    assert in_memory.package_digest is None
    assert absent.status == "failed"
    assert absent.package_digest is None
    assert absent.package_digest_after is None
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        execute_detector(tmp_path / "absent", _evidence(), timeout_seconds=0)
