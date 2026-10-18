"""Offline tests for the named private-authoring profile bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from asago_artifact_generator import cli
from asago_artifact_generator.authoring.transport import PrivateModelAuthoringTransport
from asago_artifact_generator.profiles import (
    ProfileFieldError,
    ProfileFileError,
    ProfileNotFoundError,
    load_authoring_profile,
)

from .cli_helpers import PlainCliRunner
from .support import (
    HANDOFF,
    chat_completion,
    fake_openai,
    profile_file,
    prompt_packet,
    target_inputs,
)


@dataclass
class _CliCapture:
    """What the `generate` command handed to the transport and the orchestrator."""

    transport: dict[str, Any] = field(default_factory=dict)
    orchestrator: dict[str, Any] = field(default_factory=dict)
    run_inputs: tuple[Any, ...] = ()
    constructed: bool = False


def _fake_cli_authoring(monkeypatch: pytest.MonkeyPatch) -> _CliCapture:
    """Replace the CLI's transport and orchestrator with recorders that fail the run.

    The returned capture holds the keyword arguments each constructor received and the
    inputs of `run`, so a test asserts what the command wired without any model request.
    """

    capture = _CliCapture()

    class FakeTransport:
        max_retries = 0

        def __init__(self, **kwargs: Any) -> None:
            capture.transport.update(kwargs)

    class FakeOrchestrator:
        def __init__(self, **kwargs: Any) -> None:
            capture.orchestrator.update(kwargs)

        def run(self, *inputs: Any) -> SimpleNamespace:
            capture.run_inputs = inputs
            return SimpleNamespace(
                status="failed", package_path=None, review_status={}, findings=[]
            )

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", FakeTransport)
    monkeypatch.setattr(cli, "AuthoringOrchestrator", FakeOrchestrator)
    return capture


def _forbid_cli_transport(monkeypatch: pytest.MonkeyPatch) -> _CliCapture:
    """Make constructing the CLI's transport raise and record that it happened."""

    capture = _CliCapture()

    def fail_if_constructed(**_: Any) -> object:
        capture.constructed = True
        raise AssertionError("transport must not be constructed")

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", fail_if_constructed)
    return capture


def _invoke_generate(
    tmp_path: Path,
    *,
    extra_args: list[str] | None = None,
) -> object:
    target_profile, runtime_contract = target_inputs(tmp_path)
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
    return PlainCliRunner().invoke(cli.app, arguments)


def test_loader_returns_named_connection_fields_without_logging_or_redaction(
    tmp_path: Path,
) -> None:
    profiles_file, values = profile_file(tmp_path)

    profile = load_authoring_profile(profiles_file, "profile-a")

    assert profile.name == "profile-a"
    assert profile.base_url == values["base_url"]
    assert profile.api_key == values["api_key"]
    assert profile.model == values["model"]
    assert values["api_key"] not in repr(profile)
    assert values["base_url"] not in repr(profile)


def test_author_cli_passes_profile_values_directly_to_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_file, values = profile_file(tmp_path)
    captured = _fake_cli_authoring(monkeypatch)

    result = _invoke_generate(
        tmp_path,
        extra_args=[
            "--profile",
            "profile-a",
            "--profiles-file",
            str(profiles_file),
        ],
    )

    assert result.exit_code == 1
    assert captured.transport["base_url"] == values["base_url"]
    assert captured.transport["api_key"] == values["api_key"]
    assert captured.transport["model"] == values["model"]
    assert captured.transport["profile_name"] == "profile-a"
    thinking_off = {"chat_template_kwargs": {"enable_thinking": False}}
    assert captured.transport["extra_body"] == thinking_off
    assert captured.transport["review_extra_body"] == thinking_off
    assert captured.transport["context_window_tokens"] == 32_768
    assert captured.transport["max_completion_tokens"] == 8_192
    assert captured.transport["review_fill_context"] is True
    _, inventory, runtime = captured.run_inputs
    assert [operation["name"] for operation in inventory["operations"]] == ["process_refund"]
    assert {handle["ref"] for handle in inventory["source_handles"]} == {
        "target-profile",
        "tool:process_refund",
    }
    assert captured.orchestrator["discovery_provenance"]["target_id"] == "fixture-target"
    assert runtime["delivery"] == ["direct_user_message"]
    assert values["api_key"] not in result.output
    assert values["base_url"] not in result.output


def test_author_cli_passes_optional_profile_controls_to_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_file, _ = profile_file(
        tmp_path,
        reasoning_effort="high",
        service_tier="priority",
        service_tier_fallback="auto",
        sampling_controls=False,
        strict_json_schema=False,
        context_window=1_050_000,
        max_completion_tokens=32_000,
        timeout=900,
    )
    captured = _fake_cli_authoring(monkeypatch)

    result = _invoke_generate(
        tmp_path,
        extra_args=[
            "--profile",
            "profile-a",
            "--profiles-file",
            str(profiles_file),
        ],
    )

    assert result.exit_code == 1
    assert captured.transport["reasoning_effort"] == "high"
    assert captured.transport["service_tier"] == "priority"
    assert captured.transport["service_tier_fallback"] == "auto"
    assert captured.transport["sampling_controls"] is False
    assert captured.transport["strict_json_schema"] is False
    assert captured.transport["context_window_tokens"] == 1_050_000
    assert captured.transport["max_completion_tokens"] == 32_000
    assert captured.transport["timeout"] == 900
    assert captured.transport["extra_body"] is None
    assert captured.transport["review_extra_body"] is None


def test_profile_without_a_timeout_or_strict_json_schema_sends_the_contract_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_file, _ = profile_file(tmp_path)
    captured = _fake_cli_authoring(monkeypatch)

    _invoke_generate(
        tmp_path,
        extra_args=["--profile", "profile-a", "--profiles-file", str(profiles_file)],
    )

    assert captured.transport["timeout"] == 600
    assert captured.transport["strict_json_schema"] is False


def test_profile_without_the_field_loads_beside_a_profile_that_sets_it(tmp_path: Path) -> None:
    base = {"base_url": "https://profile.example.invalid/v1", "api_key": "k", "model": "m"}
    profiles_file = tmp_path / "two.yaml"
    profiles_file.write_text(
        yaml.safe_dump(
            {"gemma": dict(base), "glm": {**base, "repetition_penalty": 1.05}},
        ),
        encoding="utf-8",
    )

    profile = load_authoring_profile(profiles_file, "gemma")

    assert (profile.name, profile.model) == ("gemma", "m")


def test_author_cli_merges_the_penalty_into_both_thinking_bodies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_file, _ = profile_file(tmp_path, repetition_penalty=1.05)
    review_body = {"chat_template_kwargs": {"enable_thinking": True}}
    monkeypatch.setattr(cli, "REVIEW_THINKING_EXTRA_BODY", review_body)
    captured = _fake_cli_authoring(monkeypatch)

    result = _invoke_generate(
        tmp_path,
        extra_args=["--profile", "profile-a", "--profiles-file", str(profiles_file)],
    )

    assert result.exit_code == 1
    assert captured.transport["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
        "repetition_penalty": 1.05,
    }
    assert captured.transport["review_extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True},
        "repetition_penalty": 1.05,
    }
    assert review_body == {"chat_template_kwargs": {"enable_thinking": True}}
    assert "repetition_penalty" not in captured.transport


def test_penalty_profile_reaches_author_and_review_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_file, _ = profile_file(tmp_path, repetition_penalty=1.05)
    captured = _fake_cli_authoring(monkeypatch)
    _invoke_generate(
        tmp_path,
        extra_args=["--profile", "profile-a", "--profiles-file", str(profiles_file)],
    )
    completions = fake_openai(monkeypatch, [chat_completion(), chat_completion()])
    transport = PrivateModelAuthoringTransport(**captured.transport)

    transport.complete(prompt_packet("call1"))
    transport.complete(prompt_packet("plan_review"))

    body = {"chat_template_kwargs": {"enable_thinking": False}, "repetition_penalty": 1.05}
    assert [request["extra_body"] for request in completions.requests] == [body, body]
    assert all("repetition_penalty" not in request for request in completions.requests)


def test_author_cli_rejects_missing_named_profile_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_file, _ = profile_file(tmp_path)
    transport = _forbid_cli_transport(monkeypatch)

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
    assert transport.constructed is False


def test_author_cli_requires_a_named_profile_before_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = _forbid_cli_transport(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret-value")

    result = _invoke_generate(tmp_path)

    assert result.exit_code == 2
    assert "--profile" in result.output
    assert "environment-secret-value" not in result.output
    assert transport.constructed is False


def test_profile_secret_is_redacted_from_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles_file, values = profile_file(tmp_path)

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
            "profile-a",
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
        ("profile-a: {}\n", "  ", ProfileNotFoundError, "profile name must be a nonblank string"),
        ("profile-a: {}\n", None, ProfileNotFoundError, "profile name must be a nonblank string"),
        (None, "profile-a", ProfileFileError, "could not read model profiles file"),
        ("key: [unclosed\n", "profile-a", ProfileFileError, "could not read model profiles file"),
        ("- profile-a\n", "profile-a", ProfileFileError, "model profiles file is not a mapping"),
        ("profiles: []\n", "profile-a", ProfileFileError, "profiles entry is not a mapping"),
        ("profiles: {}\n", "profile-a", ProfileNotFoundError, "profile 'profile-a' not found"),
        ("profile-a: model\n", "profile-a", ProfileFieldError, "'profile-a' is not a mapping"),
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
