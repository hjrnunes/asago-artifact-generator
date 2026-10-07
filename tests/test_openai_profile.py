"""Offline tests for OpenAI-compatible reasoning-model request profiles."""

from __future__ import annotations

import math
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import openai
import pytest
from openai.types.completion_usage import CompletionTokensDetails, CompletionUsage

from asago_artifact_generator.authoring.context_budget import (
    _context_budget_estimate,
    _context_guard_ratio,
)
from asago_artifact_generator.authoring.core import (
    _CONTEXT_FRAMING_TOKEN_RESERVE,
    AUTHORING_CONTEXT_WINDOW_TOKENS,
    AUTHORING_MAX_COMPLETION_TOKENS,
    AUTHORING_THINKING_EXTRA_BODY,
    REVIEW_THINKING_EXTRA_BODY,
    PromptOverflowError,
)
from asago_artifact_generator.authoring.policy import AuthoringBudget
from asago_artifact_generator.input_adapter import load_input

from .support import (
    HANDOFF,
    chat_completion,
    fake_openai,
    private_transport,
    prompt_packet,
    stage_local_orchestrator,
    status_error,
    unreviewed_policy,
)


def test_gemma_like_profile_preserves_current_request_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = fake_openai(monkeypatch, [chat_completion()])
    transport = private_transport(
        model="gemma-4-26b-a4b-it",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        context_window_tokens=32_768,
        max_completion_tokens=8_192,
    )

    transport.complete(prompt_packet())

    assert completions.clients[0].init_kwargs == {
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
    completions = fake_openai(monkeypatch, [chat_completion()])
    transport = private_transport(
        reasoning_effort="high",
        service_tier="priority",
        timeout=900,
        context_window_tokens=1_050_000,
        max_completion_tokens=32_000,
    )

    transport.complete(prompt_packet())

    assert completions.clients[0].init_kwargs["timeout"] == 900
    request = completions.requests[0]
    assert request["reasoning_effort"] == "high"
    assert request["service_tier"] == "priority"
    assert request["max_completion_tokens"] == 32_000
    assert transport.context_window_tokens == 1_050_000


def test_sampling_controls_false_omits_all_sampling_and_thinking_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = fake_openai(monkeypatch, [chat_completion()])
    transport = private_transport(
        sampling_controls=False,
        extra_body=AUTHORING_THINKING_EXTRA_BODY,
        temperature=0.0,
    )

    response = transport.complete(prompt_packet())

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
    completions = fake_openai(monkeypatch, [chat_completion()])

    response = private_transport(strict_json_schema=True).complete(prompt_packet())

    assert "response_format" not in completions.requests[0]
    assert response.controls["strict_json_schema"] is True


def test_large_context_review_uses_profile_completion_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = fake_openai(monkeypatch, [chat_completion()])
    transport = private_transport(
        context_window_tokens=1_050_000,
        max_completion_tokens=32_000,
        review_fill_context=True,
    )

    response = transport.complete(prompt_packet("plan_review"))

    assert completions.requests[0]["max_completion_tokens"] == 32_000
    assert response.controls["max_completion_tokens"] == 32_000


def test_service_tier_fallback_retries_once_with_fallback_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = fake_openai(monkeypatch, [status_error(429), chat_completion()])
    transport = private_transport(service_tier="priority", service_tier_fallback="auto")

    response = transport.complete(prompt_packet())

    assert len(completions.requests) == 2
    assert completions.requests[0]["service_tier"] == "priority"
    assert completions.requests[1]["service_tier"] == "auto"
    assert completions.requests[1]["messages"] == completions.requests[0]["messages"]
    assert response.controls["service_tier"] == "auto"
    assert response.controls["service_tier_fallback_used"] is True
    assert response.controls["service_tier_requested"] == "priority"
    assert response.controls["service_tier_fallback"] == "auto"


@pytest.mark.parametrize(
    ("service_tier", "fallback", "failures", "error", "sent_tiers"),
    [
        pytest.param(
            "priority",
            "auto",
            [status_error(429), status_error(429)],
            openai.RateLimitError,
            ["priority", "auto"],
            id="stops-after-one-retry",
        ),
        pytest.param(
            None, "auto", [status_error(429)], openai.RateLimitError, [None], id="no-service-tier"
        ),
        pytest.param(
            "priority",
            None,
            [status_error(429)],
            openai.RateLimitError,
            ["priority"],
            id="no-fallback-tier",
        ),
        pytest.param(
            None, None, [status_error(429)], openai.RateLimitError, [None], id="neither-tier"
        ),
        pytest.param(
            "priority",
            "auto",
            [RuntimeError("provider failure")],
            RuntimeError,
            ["priority"],
            id="not-a-rate-limit-error",
        ),
    ],
)
def test_service_tier_fallback_retries_only_a_rate_limit_with_both_tiers_set(
    monkeypatch: pytest.MonkeyPatch,
    service_tier: str | None,
    fallback: str | None,
    failures: list[BaseException],
    error: type[BaseException],
    sent_tiers: list[str | None],
) -> None:
    completions = fake_openai(monkeypatch, failures)
    transport = private_transport(service_tier=service_tier, service_tier_fallback=fallback)

    with pytest.raises(error):
        transport.complete(prompt_packet())

    assert [request.get("service_tier") for request in completions.requests] == sent_tiers


def test_reasoning_tokens_are_retained_in_usage_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    usage = CompletionUsage(
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        completion_tokens_details=CompletionTokensDetails(reasoning_tokens=17),
    )
    fake_openai(monkeypatch, [chat_completion(usage=usage)])

    response = private_transport().complete(prompt_packet())

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
    empty = chat_completion("", finish_reason="length", usage={"total_tokens": 32})
    completions = fake_openai(monkeypatch, [empty])
    transport = private_transport()

    response = transport.complete(prompt_packet())

    assert response.raw == b""
    assert response.response_capture is not None
    assert response.response_capture["final_answer"]["state"] == "empty"
    assert response.response_capture["finish_reason"] == {
        "state": "value",
        "value": "length",
    }
    completions.responses.append(empty)

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "empty-reasoning-package",
        task_id="empty-reasoning",
        policy=unreviewed_policy(plan_max_corrections=0, artifact_max_corrections=0),
        budget=AuthoringBudget(aggregate_limit=1, task_limit=1),
    ).run(load_input(HANDOFF), {}, {})

    assert result.status == "unresolved"
    assert result.findings[0].code == "ambiguous_content"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_completion_tokens": True}, "max_completion_tokens must be a positive integer"),
        ({"max_completion_tokens": 0}, "max_completion_tokens must be a positive integer"),
        ({"max_completion_tokens": -1}, "max_completion_tokens must be a positive integer"),
        ({"max_completion_tokens": 1.5}, "max_completion_tokens must be a positive integer"),
        ({"max_completion_tokens": "8192"}, "max_completion_tokens must be a positive integer"),
        ({"context_window_tokens": False}, "context_window_tokens must be a positive integer"),
        ({"context_window_tokens": -1}, "context_window_tokens must be a positive integer"),
        (
            {"max_completion_tokens": 8_000, "context_window_tokens": 8_000},
            "max_completion_tokens leaves no room for the prompt in the context window",
        ),
        ({"sampling_controls": 1}, "sampling_controls must be a boolean"),
        ({"reasoning_effort": " "}, "reasoning_effort must be a nonblank string"),
        ({"reasoning_effort": 3}, "reasoning_effort must be a nonblank string"),
        ({"service_tier": ""}, "service_tier must be a nonblank string"),
        ({"service_tier_fallback": "\t"}, "service_tier_fallback must be a nonblank string"),
        ({"strict_json_schema": "yes"}, "strict_json_schema must be a boolean"),
        ({"timeout": True}, "timeout must be a positive number"),
        ({"timeout": 0}, "timeout must be a positive number"),
        ({"timeout": "10"}, "timeout must be a positive number"),
        ({"review_fill_context": True}, "review_fill_context requires context_window_tokens"),
    ],
)
def test_transport_rejects_each_invalid_option(options: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        private_transport(**options)


def test_transport_reports_the_first_invalid_option_in_a_fixed_order() -> None:
    every_option_invalid = {
        "max_completion_tokens": 0,
        "context_window_tokens": 0,
        "sampling_controls": None,
        "reasoning_effort": "",
        "service_tier": "",
        "service_tier_fallback": "",
        "strict_json_schema": "yes",
        "timeout": 0,
        "review_fill_context": True,
    }
    order = []
    while every_option_invalid:
        with pytest.raises(ValueError) as raised:
            private_transport(**every_option_invalid)
        name = str(raised.value).split(" ", 1)[0]
        order.append(name)
        every_option_invalid.pop(name)

    assert order == [
        "max_completion_tokens",
        "context_window_tokens",
        "sampling_controls",
        "reasoning_effort",
        "service_tier",
        "service_tier_fallback",
        "strict_json_schema",
        "timeout",
        "review_fill_context",
    ]


def test_transport_keeps_valid_options_and_passes_the_timeout_to_the_client() -> None:
    transport = private_transport(
        max_completion_tokens=1_000,
        context_window_tokens=32_000,
        review_fill_context=True,
        reasoning_effort="high",
        service_tier="flex",
        service_tier_fallback="auto",
        strict_json_schema=False,
        timeout=2.5,
    )

    assert (transport.max_completion_tokens, transport.context_window_tokens) == (1_000, 32_000)
    assert transport.review_fill_context is True
    assert (transport.service_tier, transport.service_tier_fallback) == ("flex", "auto")
    assert transport.strict_json_schema is False
    assert transport.timeout == 2.5
    assert transport._client.timeout == 2.5
    assert transport._client.max_retries == 0


def test_controls_record_extra_body_that_survives_disabled_sampling_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = fake_openai(monkeypatch, [chat_completion()])

    response = private_transport(sampling_controls=False, extra_body={"custom": 1}).complete(
        prompt_packet()
    )

    assert completions.requests[0]["extra_body"] == {"custom": 1}
    assert response.controls == {
        "max_retries": 0,
        "extra_body": {"custom": 1},
        "sampling_controls": False,
    }


def test_private_model_transport_constructs_with_zero_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = fake_openai(monkeypatch, [])

    private_transport(api_key="secret-value")

    init_kwargs = completions.clients[0].init_kwargs
    assert init_kwargs["max_retries"] == 0
    assert init_kwargs["base_url"] == "https://private.invalid/v1"


def test_private_model_transport_sends_thinking_off_extra_body_for_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = fake_openai(monkeypatch, [chat_completion()] * 4)
    transport = private_transport(extra_body=deepcopy(AUTHORING_THINKING_EXTRA_BODY))
    responses = [
        transport.complete(prompt_packet(stage))
        for stage in ("call1", "call2", "correction", "plan_review")
    ]

    thinking_off = {"chat_template_kwargs": {"enable_thinking": False}}
    assert AUTHORING_THINKING_EXTRA_BODY == thinking_off
    assert completions.clients[0].init_kwargs["max_retries"] == 0
    assert [request["extra_body"] for request in completions.requests] == [thinking_off] * 4
    assert all(
        response.controls
        == {
            "temperature": 0.0,
            "max_retries": 0,
            "extra_body": thinking_off,
        }
        for response in responses
    )


def test_private_model_transport_applies_review_extra_body_only_to_review_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review_answer = chat_completion('{"decision":"accept"}', reasoning_content="review reasoning")
    completions = fake_openai(monkeypatch, [review_answer] * 5)
    transport = private_transport(
        extra_body=deepcopy(AUTHORING_THINKING_EXTRA_BODY),
        review_extra_body={"chat_template_kwargs": {"enable_thinking": True}},
    )
    stages = ("call1", "plan_review", "correction", "call2", "artifact_review")
    responses = [transport.complete(prompt_packet(stage)) for stage in stages]

    thinking_off = {"chat_template_kwargs": {"enable_thinking": False}}
    thinking_on = {"chat_template_kwargs": {"enable_thinking": True}}
    assert REVIEW_THINKING_EXTRA_BODY == thinking_off
    expected = [thinking_off, thinking_on, thinking_off, thinking_off, thinking_on]
    assert [request["extra_body"] for request in completions.requests] == expected
    assert [response.controls["extra_body"] for response in responses] == expected
    review = responses[1]
    assert review.raw == b'{"decision":"accept"}'
    assert review.response_capture["reasoning"]["content"] == "review reasoning"
    assert b"review reasoning" not in review.raw


def test_private_model_transport_fills_remaining_context_for_review_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = fake_openai(monkeypatch, [chat_completion()] * 3)
    transport = private_transport(
        context_window_tokens=32_768,
        max_completion_tokens=8_192,
        review_fill_context=True,
    )
    packets = [
        prompt_packet(stage, user="x" * 40_000)
        for stage in ("call1", "plan_review", "artifact_review")
    ]
    responses = [transport.complete(packet) for packet in packets]

    # Each stage has its own calibrated ratio, so each review fills from its own estimate.
    estimates = [_context_budget_estimate(packet)["estimated_prompt_tokens"] for packet in packets]
    filled = [32_768 - estimate - _CONTEXT_FRAMING_TOKEN_RESERVE for estimate in estimates[1:]]
    assert all(limit > 8_192 for limit in filled)
    expected = [8_192, *filled]
    assert [request["max_completion_tokens"] for request in completions.requests] == expected
    assert [response.controls["max_completion_tokens"] for response in responses] == expected
    assert all(
        estimate + limit + _CONTEXT_FRAMING_TOKEN_RESERVE <= 32_768
        for estimate, limit in zip(estimates, expected, strict=True)
    )


def test_private_model_transport_preserves_default_request_shape_and_captures_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = fake_openai(
        monkeypatch,
        [
            chat_completion(
                '{"answer":"ok"}',
                reasoning_content="private reasoning",
                usage={"prompt_tokens": 3, "completion_tokens": 2},
            )
        ],
    )

    response = private_transport().complete(prompt_packet())

    assert completions.requests == [
        {
            "model": "luna",
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "user"},
            ],
        }
    ]
    assert response.raw == b'{"answer":"ok"}'
    assert response.usage == {"prompt_tokens": 3, "completion_tokens": 2}
    assert response.controls == {
        "temperature": 0.0,
        "max_retries": 0,
        "extra_body": None,
    }
    assert response.response_capture == {
        "schema_version": "authoring-response-capture-v1",
        "final_answer": {"state": "text", "content": '{"answer":"ok"}'},
        "reasoning": {
            "state": "text",
            "content": "private reasoning",
            "source_field": "reasoning_content",
        },
        "finish_reason": {"state": "value", "value": "stop"},
    }


@pytest.mark.parametrize(
    ("message", "expected_state"),
    [
        (SimpleNamespace(), "absent"),
        (SimpleNamespace(content=None), "null"),
        (SimpleNamespace(content=""), "empty"),
        (SimpleNamespace(content="final"), "text"),
        (SimpleNamespace(content=["non-text"]), "non_text"),
    ],
    ids=["absent", "null", "empty", "text", "non-text"],
)
def test_private_model_transport_distinguishes_final_content_states(
    monkeypatch: pytest.MonkeyPatch,
    message: SimpleNamespace,
    expected_state: str,
) -> None:
    fake_openai(monkeypatch, [chat_completion(message=message, finish_reason=None)])

    response = private_transport().complete(prompt_packet())

    expected_raw = b"final" if expected_state == "text" else b""
    assert response.raw == expected_raw
    assert response.response_capture is not None
    assert response.response_capture["final_answer"]["state"] == expected_state
    assert response.response_capture["reasoning"]["state"] == "absent"
    assert response.response_capture["finish_reason"] == {"state": "null"}


def test_private_model_transport_distinguishes_an_absent_finish_reason_from_a_null_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completion = chat_completion("final")
    del completion.choices[0].finish_reason
    fake_openai(monkeypatch, [completion])

    response = private_transport().complete(prompt_packet())

    assert response.response_capture is not None
    assert response.response_capture["finish_reason"] == {"state": "absent"}


def test_private_model_transport_captures_empty_reasoning_and_length_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = fake_openai(
        monkeypatch,
        [
            chat_completion(
                "",
                reasoning_content="analysis",
                finish_reason="length",
                usage={"total_tokens": 8192},
            )
        ],
    )
    transport = private_transport(max_completion_tokens=8192)

    response = transport.complete(prompt_packet("artifact_review"))

    assert completions.requests[0]["max_completion_tokens"] == 8192
    assert response.raw == b""
    assert response.controls["max_completion_tokens"] == 8192
    assert response.response_capture == {
        "schema_version": "authoring-response-capture-v1",
        "final_answer": {"state": "empty", "content": ""},
        "reasoning": {
            "state": "text",
            "content": "analysis",
            "source_field": "reasoning_content",
        },
        "finish_reason": {"state": "value", "value": "length"},
    }


def test_private_model_transport_rejects_context_overflow_before_provider_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completions = fake_openai(monkeypatch, [])
    transport = private_transport(
        context_window_tokens=AUTHORING_CONTEXT_WINDOW_TOKENS,
        max_completion_tokens=AUTHORING_MAX_COMPLETION_TOKENS,
    )
    # One byte past the largest correction prompt that fits the 24,320-token input budget.
    fitting_bytes = math.floor(24_320 * _context_guard_ratio("correction"))
    packet = prompt_packet("correction", user="x" * (fitting_bytes + 1 - len("system") - 128))

    with pytest.raises(PromptOverflowError, match="context window") as overflow:
        transport.complete(packet)
    assert completions.requests == []
    assert overflow.value.estimated_prompt_tokens == 24_321
    assert overflow.value.remaining_input_budget == 24_320
