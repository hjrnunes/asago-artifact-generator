"""Offline tests for the named private-authoring profile bridge."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from asago_artifact_generator import cli
from asago_artifact_generator.profiles import (
    ProfileFieldError,
    ProfileFileError,
    ProfileNotFoundError,
    load_authoring_profile,
)

from .support import HANDOFF


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    profile = tmp_path / "execution-target-profile.json"
    profile_payload = {
        "schema_version": "execution-target-profile-v1",
        "target_id": "synthetic-target",
        "authorization_scope_id": "synthetic-scope",
        "basis": "target",
        "inventory_authority": "observed",
        "semantic_authority": "inferred",
        "inventory_completeness": "observed_complete",
        "source_protocol": "mcp",
        "source_inventory_digest": "a" * 64,
        "discovery_provenance": {
            "scanner_id": "scanner",
            "interpreter_id": "interpreter",
            "verifier_id": "verifier",
        },
        "inventory": {
            "schema_version": "mcp-inventory-v1",
            "target_id": "synthetic-target",
            "authorization_scope_id": "synthetic-scope",
            "source_protocol": "mcp",
            "semantic_digest": "a" * 64,
            "tools": [],
        },
        "resources": [],
        "interpretations": [],
        "diagnostics": [],
    }
    digest_payload = dict(profile_payload)
    profile_payload["semantic_digest"] = hashlib.sha256(
        b"execution-target-profile-v1\0"
        + json.dumps(
            digest_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    profile.write_text(
        json.dumps(profile_payload),
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
    return profile, runtime_contract


def _profile_file(tmp_path: Path, **changes: object) -> tuple[Path, dict[str, object]]:
    values = {
        "base_url": "https://profile.example.invalid/v1",
        "api_key": "profile-secret-value",
        "model": "profile-model",
    }
    values.update(changes)
    path = tmp_path / "profiles.yaml"
    path.write_text(yaml.safe_dump({"gemma4-oc": values}), encoding="utf-8")
    return path, values


def _invoke_generate(
    tmp_path: Path,
    *,
    extra_args: list[str] | None = None,
) -> object:
    target_profile, runtime_contract = _inputs(tmp_path)
    arguments = [
        "generate",
        str(HANDOFF),
        "--target-profile",
        str(target_profile),
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


def test_loader_preserves_optional_openai_request_controls(tmp_path: Path) -> None:
    profiles_file, _ = _profile_file(
        tmp_path,
        reasoning_effort="high",
        service_tier="priority",
        service_tier_fallback="auto",
        sampling_controls=False,
        strict_json_schema=True,
        context_window=1_050_000,
        max_completion_tokens=32_000,
        timeout=900,
    )

    profile = load_authoring_profile(profiles_file, "gemma4-oc")

    assert profile.reasoning_effort == "high"
    assert profile.service_tier == "priority"
    assert profile.service_tier_fallback == "auto"
    assert profile.sampling_controls is False
    assert profile.strict_json_schema is True
    assert profile.context_window == 1_050_000
    assert profile.max_completion_tokens == 32_000
    assert profile.timeout == 900


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
        def __init__(self, *, transport: object, **kwargs: object) -> None:
            captured["transport"] = transport
            captured["orchestrator_options"] = kwargs

        def run(self, view: object, inventory: object, runtime: object) -> SimpleNamespace:
            captured["run_inputs"] = (view, inventory, runtime)
            return SimpleNamespace(
                status="failed",
                package_path=None,
                review_status={},
                findings=[],
            )

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", FakeTransport)
    monkeypatch.setattr(cli, "AuthoringOrchestrator", FakeOrchestrator)

    result = _invoke_generate(
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
    assert captured["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert captured["review_extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert captured["context_window_tokens"] == 32_768
    assert captured["max_completion_tokens"] == 8_192
    assert captured["review_fill_context"] is True
    _, inventory, runtime = captured["run_inputs"]
    assert inventory["operations"] == []
    assert {handle["ref"] for handle in inventory["source_handles"]} == {"target-profile"}
    assert captured["orchestrator_options"]["discovery_provenance"]["target_id"] == (
        "synthetic-target"
    )
    assert runtime["delivery"] == ["direct_user_message"]
    assert values["api_key"] not in result.output
    assert values["base_url"] not in result.output


def test_author_cli_passes_optional_profile_controls_to_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_file, _ = _profile_file(
        tmp_path,
        reasoning_effort="high",
        service_tier="priority",
        service_tier_fallback="auto",
        sampling_controls=False,
        strict_json_schema=True,
        context_window=1_050_000,
        max_completion_tokens=32_000,
        timeout=900,
    )
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

    result = _invoke_generate(
        tmp_path,
        extra_args=[
            "--profile",
            "gemma4-oc",
            "--profiles-file",
            str(profiles_file),
        ],
    )

    assert result.exit_code == 1
    assert captured["reasoning_effort"] == "high"
    assert captured["service_tier"] == "priority"
    assert captured["service_tier_fallback"] == "auto"
    assert captured["sampling_controls"] is False
    assert captured["strict_json_schema"] is True
    assert captured["context_window_tokens"] == 1_050_000
    assert captured["max_completion_tokens"] == 32_000
    assert captured["timeout"] == 900
    assert captured["extra_body"] is None
    assert captured["review_extra_body"] is None


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

    result = _invoke_generate(
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


def test_author_cli_requires_a_named_profile_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fail_if_constructed(**_: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("transport must not be constructed")

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", fail_if_constructed)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret-value")

    result = _invoke_generate(tmp_path)

    assert result.exit_code == 2
    assert "--profile" in result.output
    assert "environment-secret-value" not in result.output
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

    result = _invoke_generate(
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


@pytest.mark.parametrize(
    ("content", "name", "error", "message"),
    [
        ("gemma4-oc: {}\n", "  ", ProfileNotFoundError, "profile name must be a nonblank string"),
        ("gemma4-oc: {}\n", None, ProfileNotFoundError, "profile name must be a nonblank string"),
        (None, "gemma4-oc", ProfileFileError, "could not read model profiles file"),
        ("key: [unclosed\n", "gemma4-oc", ProfileFileError, "could not read model profiles file"),
        ("- gemma4-oc\n", "gemma4-oc", ProfileFileError, "model profiles file is not a mapping"),
        ("profiles: []\n", "gemma4-oc", ProfileFileError, "profiles entry is not a mapping"),
        ("profiles: {}\n", "gemma4-oc", ProfileNotFoundError, "profile 'gemma4-oc' not found"),
        ("gemma4-oc: model\n", "gemma4-oc", ProfileFieldError, "'gemma4-oc' is not a mapping"),
    ],
)
def test_loader_rejects_unreadable_files_and_malformed_profile_entries(
    tmp_path: Path, content: str | None, name: object, error: type, message: str
) -> None:
    profiles_file = tmp_path / "profiles.yaml"
    if content is not None:
        profiles_file.write_text(content, encoding="utf-8")

    with pytest.raises(error, match=message):
        load_authoring_profile(profiles_file, name)


def test_loader_reads_profiles_nested_under_a_profiles_key(tmp_path: Path) -> None:
    _, values = _profile_file(tmp_path)
    profiles_file = tmp_path / "nested.yaml"
    profiles_file.write_text(yaml.safe_dump({"profiles": {"gemma4-oc": values}}), encoding="utf-8")

    assert load_authoring_profile(profiles_file, "gemma4-oc").model == values["model"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reasoning_effort", " "),
        ("service_tier", 3),
        ("sampling_controls", "no"),
        ("strict_json_schema", 1),
        ("context_window", 0),
        ("max_completion_tokens", True),
        ("timeout", -1),
        ("timeout", "fast"),
    ],
)
def test_loader_rejects_an_invalid_optional_request_control(
    tmp_path: Path, field: str, value: object
) -> None:
    profiles_file, _ = _profile_file(tmp_path, **{field: value})

    with pytest.raises(ProfileFieldError, match=f"has invalid field '{field}'") as exc_info:
        load_authoring_profile(profiles_file, "gemma4-oc")

    assert exc_info.value.field == field
