"""Offline tests for the named private-authoring profile bridge."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from asago_artifact_generator import cli
from asago_artifact_generator.profiles import (
    ProfileFieldError,
    ProfileNotFoundError,
    load_authoring_profile,
)

HANDOFF = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "scenario-handoff"
    / "handoff-v1"
    / "valid"
    / "adversarial-refund.json"
)


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "operations": [],
                "facts": [],
                "source_handles": [],
            }
        ),
        encoding="utf-8",
    )
    runtime_contract = tmp_path / "runtime-contract.json"
    runtime_contract.write_text(
        json.dumps(
            {
                "delivery": ["direct_user_message"],
                "observation": {},
                "setup_permissions": [],
                "limits": {"max_turns": 1},
            }
        ),
        encoding="utf-8",
    )
    return inventory, runtime_contract


def _profile_file(tmp_path: Path, **changes: object) -> tuple[Path, dict[str, str]]:
    values = {
        "base_url": "https://profile.example.invalid/v1",
        "api_key": "profile-secret-value",
        "model": "profile-model",
    }
    values.update(changes)
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump({"gemma4-oc": values}), encoding="utf-8")
    return path, values


def _invoke_author(
    tmp_path: Path,
    *,
    extra_args: list[str] | None = None,
) -> object:
    inventory, runtime_contract = _inputs(tmp_path)
    arguments = [
        "author",
        str(HANDOFF),
        "--inventory",
        str(inventory),
        "--runtime-contract",
        str(runtime_contract),
        "--output-dir",
        str(tmp_path / "output"),
        *(extra_args or []),
    ]
    return CliRunner().invoke(cli.app, arguments)


def test_loader_returns_named_connection_fields_without_logging_or_redaction(
    tmp_path: Path,
) -> None:
    profiles_file, values = _profile_file(tmp_path)

    profile = load_authoring_profile(profiles_file, "gemma4-oc")

    assert profile.name == "gemma4-oc"
    assert profile.base_url == values["base_url"]
    assert profile.api_key == values["api_key"]
    assert profile.model == values["model"]
    assert values["api_key"] not in repr(profile)
    assert values["base_url"] not in repr(profile)


@pytest.mark.parametrize("field", ["base_url", "api_key", "model"])
def test_loader_fails_closed_for_missing_required_connection_field(
    tmp_path: Path, field: str
) -> None:
    profiles_file, values = _profile_file(tmp_path)
    values.pop(field)
    profiles_file.write_text(yaml.safe_dump({"gemma4-oc": values}), encoding="utf-8")

    with pytest.raises(ProfileFieldError) as exc_info:
        load_authoring_profile(profiles_file, "gemma4-oc")

    assert exc_info.value.field == field
    assert field in str(exc_info.value)
    assert "profile-secret-value" not in str(exc_info.value)


def test_loader_fails_closed_for_unknown_named_profile(tmp_path: Path) -> None:
    profiles_file, _ = _profile_file(tmp_path)

    with pytest.raises(ProfileNotFoundError):
        load_authoring_profile(profiles_file, "missing")


def test_author_cli_passes_profile_values_directly_to_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_file, values = _profile_file(tmp_path)
    captured: dict[str, object] = {}

    class FakeTransport:
        max_retries = 0

        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class FakeOrchestrator:
        def __init__(self, *, transport: object, **_: object) -> None:
            captured["transport"] = transport

        def run(self, *_: object) -> SimpleNamespace:
            return SimpleNamespace(
                status="failed",
                package_path=None,
                review_status={},
                findings=[],
            )

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", FakeTransport)
    monkeypatch.setattr(cli, "AuthoringOrchestrator", FakeOrchestrator)

    result = _invoke_author(
        tmp_path,
        extra_args=[
            "--profile",
            "gemma4-oc",
            "--profiles-file",
            str(profiles_file),
        ],
    )

    assert result.exit_code == 1
    assert captured["base_url"] == values["base_url"]
    assert captured["api_key"] == values["api_key"]
    assert captured["model"] == values["model"]
    assert captured["profile_name"] == "gemma4-oc"
    assert values["api_key"] not in result.output
    assert values["base_url"] not in result.output


def test_author_cli_rejects_missing_named_profile_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_file, _ = _profile_file(tmp_path)
    called = False

    def fail_if_constructed(**_: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("transport must not be constructed")

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", fail_if_constructed)

    result = _invoke_author(
        tmp_path,
        extra_args=[
            "--profile",
            "missing",
            "--profiles-file",
            str(profiles_file),
        ],
    )

    assert result.exit_code == 2
    assert "not found" in result.output
    assert called is False


def test_author_cli_keeps_real_environment_only_configuration_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class FakeTransport:
        max_retries = 0

        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    class FakeOrchestrator:
        def __init__(self, **_: object) -> None:
            pass

        def run(self, *_: object) -> SimpleNamespace:
            return SimpleNamespace(
                status="failed",
                package_path=None,
                review_status={},
                findings=[],
            )

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", FakeTransport)
    monkeypatch.setattr(cli, "AuthoringOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(cli, "BASE_URL", "https://environment.example.invalid/v1")
    monkeypatch.setattr(cli, "MODEL", "environment-model")
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret-value")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    result = _invoke_author(tmp_path)

    assert result.exit_code == 1
    assert captured == {
        "base_url": "https://environment.example.invalid/v1",
        "api_key": "environment-secret-value",
        "model": "environment-model",
    }
    assert "environment-secret-value" not in result.output


def test_author_cli_rejects_missing_environment_credential_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fail_if_constructed(**_: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("transport must not be constructed")

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", fail_if_constructed)
    for name in (
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "HF_TOKEN",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    result = _invoke_author(tmp_path)

    assert result.exit_code == 2
    assert "API key" in result.output
    assert "private" not in result.output
    assert called is False


def test_profile_secret_is_redacted_from_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_file, values = _profile_file(tmp_path)

    class FailingTransport:
        max_retries = 0

        def __init__(self, **_: object) -> None:
            pass

        def complete(self, _packet: object) -> object:
            raise RuntimeError("offline")

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", FailingTransport)

    result = _invoke_author(
        tmp_path,
        extra_args=[
            "--profile",
            "gemma4-oc",
            "--profiles-file",
            str(profiles_file),
        ],
    )

    assert result.exit_code == 1
    evidence = tmp_path / "output" / "SCN-007.failure-evidence.json"
    persisted = evidence.read_text(encoding="utf-8")
    assert values["api_key"] not in persisted
    assert values["base_url"] not in persisted
    assert values["model"] not in persisted
