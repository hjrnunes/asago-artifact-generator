"""Offline tests for OpenAI-compatible reasoning-model request profiles."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx2
import openai
import pytest
from openai.types.completion_usage import CompletionTokensDetails, CompletionUsage

from asago_artifact_generator.authoring import (
    AUTHORING_THINKING_EXTRA_BODY,
    AuthoringBudget,
    AuthoringOrchestrator,
    PrivateModelAuthoringTransport,
    PromptPacket,
)
from asago_artifact_generator.input_adapter import load_input

HANDOFF = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "scenario-handoff"
    / "handoff-v1"
    / "valid"
    / "adversarial-refund.json"
)


def _packet(stage: str = "call1") -> PromptPacket:
    return PromptPacket(stage=stage, version="test", system="system", user="user", payload={})


class _FakeCompletions:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _FakeOpenAI:
    def __init__(self, completions: _FakeCompletions, **kwargs: object) -> None:
        self.init_kwargs = kwargs
        self.chat = SimpleNamespace(completions=completions)


def _response(*, content: str = "{}", finish_reason: str = "stop", usage: object = None) -> object:
    return SimpleNamespace(
        model="returned-model",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ],
        usage=usage,
    )


def _rate_limit_error() -> openai.RateLimitError:
    return openai.RateLimitError(
        "rate limited",
        response=httpx2.Response(
            429,
            request=httpx2.Request("POST", "https://private.invalid/v1"),
        ),
        body=None,
    )


def test_gemma_like_profile_preserves_current_request_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = _FakeCompletions([_response()])
    client: _FakeOpenAI | None = None

    def fake_openai(**kwargs: object) -> _FakeOpenAI:
        nonlocal client
        client = _FakeOpenAI(completions, **kwargs)
        return client

    monkeypatch.setattr("openai.OpenAI", fake_openai)
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="gemma-4-26b-a4b-it",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        context_window_tokens=32_768,
        max_completion_tokens=8_192,
    )

    transport.complete(_packet())

    assert client is not None
    assert client.init_kwargs == {
        "base_url": "https://private.invalid/v1",
        "api_key": "secret-value",
        "max_retries": 0,
    }
    assert completions.requests == [
        {
            "model": "gemma-4-26b-a4b-it",
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "user"},
            ],
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            "max_completion_tokens": 8_192,
        }
    ]


def test_reasoning_effort_service_tier_and_timeout_are_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = _FakeCompletions([_response()])
    client: _FakeOpenAI | None = None

    def fake_openai(**kwargs: object) -> _FakeOpenAI:
        nonlocal client
        client = _FakeOpenAI(completions, **kwargs)
        return client

    monkeypatch.setattr("openai.OpenAI", fake_openai)
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
        reasoning_effort="high",
        service_tier="priority",
        timeout=900,
        context_window_tokens=1_050_000,
        max_completion_tokens=32_000,
    )

    transport.complete(_packet())

    assert client is not None
    assert client.init_kwargs["timeout"] == 900
    request = completions.requests[0]
    assert request["reasoning_effort"] == "high"
    assert request["service_tier"] == "priority"
    assert request["max_completion_tokens"] == 32_000
    assert transport.context_window_tokens == 1_050_000


def test_sampling_controls_false_omits_all_sampling_and_thinking_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = _FakeCompletions([_response()])
    monkeypatch.setattr(
        "openai.OpenAI",
        lambda **kwargs: _FakeOpenAI(completions, **kwargs),
    )
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
        sampling_controls=False,
        extra_body=AUTHORING_THINKING_EXTRA_BODY,
        temperature=0.0,
    )

    response = transport.complete(_packet())

    request = completions.requests[0]
    assert all(
        key not in request for key in ("temperature", "top_p", "top_k", "seed", "extra_body")
    )
    assert response.controls == {
        "max_retries": 0,
        "sampling_controls": False,
    }


def test_strict_json_schema_is_recorded_without_sending_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = _FakeCompletions([_response()])
    monkeypatch.setattr(
        "openai.OpenAI",
        lambda **kwargs: _FakeOpenAI(completions, **kwargs),
    )
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
        strict_json_schema=True,
    )

    response = transport.complete(_packet())

    assert "response_format" not in completions.requests[0]
    assert response.controls["strict_json_schema"] is True


def test_large_context_review_uses_profile_completion_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = _FakeCompletions([_response()])
    monkeypatch.setattr(
        "openai.OpenAI",
        lambda **kwargs: _FakeOpenAI(completions, **kwargs),
    )
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
        context_window_tokens=1_050_000,
        max_completion_tokens=32_000,
        review_fill_context=True,
    )

    response = transport.complete(_packet("plan_review"))

    assert completions.requests[0]["max_completion_tokens"] == 32_000
    assert response.controls["max_completion_tokens"] == 32_000


def test_service_tier_fallback_retries_once_with_fallback_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _rate_limit_error()
    completions = _FakeCompletions([error, _response()])
    monkeypatch.setattr(
        "openai.OpenAI",
        lambda **kwargs: _FakeOpenAI(completions, **kwargs),
    )
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
        service_tier="priority",
        service_tier_fallback="auto",
    )

    response = transport.complete(_packet())

    assert len(completions.requests) == 2
    assert completions.requests[0]["service_tier"] == "priority"
    assert completions.requests[1]["service_tier"] == "auto"
    assert completions.requests[1]["messages"] == completions.requests[0]["messages"]
    assert response.controls["service_tier"] == "auto"
    assert response.controls["service_tier_fallback_used"] is True
    assert response.controls["service_tier_requested"] == "priority"
    assert response.controls["service_tier_fallback"] == "auto"


def test_service_tier_fallback_stops_after_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = _FakeCompletions([_rate_limit_error(), _rate_limit_error()])
    monkeypatch.setattr(
        "openai.OpenAI",
        lambda **kwargs: _FakeOpenAI(completions, **kwargs),
    )
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
        service_tier="priority",
        service_tier_fallback="auto",
    )

    with pytest.raises(openai.RateLimitError):
        transport.complete(_packet())

    assert len(completions.requests) == 2


@pytest.mark.parametrize(
    ("service_tier", "service_tier_fallback"),
    [(None, "auto"), ("priority", None), (None, None)],
)
def test_service_tier_fallback_requires_both_tiers(
    monkeypatch: pytest.MonkeyPatch,
    service_tier: str | None,
    service_tier_fallback: str | None,
) -> None:
    completions = _FakeCompletions([_rate_limit_error()])
    monkeypatch.setattr(
        "openai.OpenAI",
        lambda **kwargs: _FakeOpenAI(completions, **kwargs),
    )
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
        service_tier=service_tier,
        service_tier_fallback=service_tier_fallback,
    )

    with pytest.raises(openai.RateLimitError):
        transport.complete(_packet())

    assert len(completions.requests) == 1


def test_service_tier_fallback_does_not_retry_non_rate_limit_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = _FakeCompletions([RuntimeError("provider failure")])
    monkeypatch.setattr(
        "openai.OpenAI",
        lambda **kwargs: _FakeOpenAI(completions, **kwargs),
    )
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
        service_tier="priority",
        service_tier_fallback="auto",
    )

    with pytest.raises(RuntimeError, match="provider failure"):
        transport.complete(_packet())

    assert len(completions.requests) == 1


def test_reasoning_tokens_are_retained_in_usage_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = CompletionUsage(
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        completion_tokens_details=CompletionTokensDetails(reasoning_tokens=17),
    )
    completions = _FakeCompletions(
        [
            _response(
                usage=usage,
            )
        ]
    )
    monkeypatch.setattr(
        "openai.OpenAI",
        lambda **kwargs: _FakeOpenAI(completions, **kwargs),
    )
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
    )

    response = transport.complete(_packet())

    assert response.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "completion_tokens_details": {"reasoning_tokens": 17},
    }


def test_empty_length_completion_remains_typed_response_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    completions = _FakeCompletions(
        [_response(content="", finish_reason="length", usage={"total_tokens": 32})]
    )
    monkeypatch.setattr(
        "openai.OpenAI",
        lambda **kwargs: _FakeOpenAI(completions, **kwargs),
    )
    transport = PrivateModelAuthoringTransport(
        base_url="https://private.invalid/v1",
        api_key="secret-value",
        model="luna",
    )

    response = transport.complete(_packet())

    assert response.raw == b""
    assert response.response_capture is not None
    assert response.response_capture["final_answer"]["state"] == "empty"
    assert response.response_capture["finish_reason"] == {
        "state": "value",
        "value": "length",
    }
    completions.responses.append(
        _response(content="", finish_reason="length", usage={"total_tokens": 32})
    )

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "empty-reasoning-package",
        task_id="empty-reasoning",
        correction_allowed=False,
        budget=AuthoringBudget(aggregate_limit=1, task_limit=1),
    ).run(load_input(HANDOFF), {}, {})

    assert result.status == "failed"
    assert result.findings[0].code == "response_parse_error"
