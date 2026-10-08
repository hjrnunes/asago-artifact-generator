"""Context-window guard: measured bytes-per-token calibration and the
pre-dispatch estimate that rejects an oversized prompt without spending a request.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from fractions import Fraction
from typing import Any

from .core import (
    _CONTEXT_FRAMING_TOKEN_RESERVE,
    _CONTEXT_MESSAGE_SCHEMA_OVERHEAD_BYTES,
    AUTHORING_CONTEXT_WINDOW_TOKENS,
    AUTHORING_MAX_COMPLETION_TOKENS,
    PromptOverflowError,
    PromptPacket,
)
from .prompt_safety import assert_no_prompt_duplicates, assert_no_prompt_secrets

# Calibrate from provider measurements of dispatched authoring prompts. Each
# record uses only provider-reported prompt_tokens and the same system+user+128
# byte measurement as the guard, and is the lowest bytes/token ratio measured
# for its stage on one of two measured open-weight models. A model whose
# tokenizer needs more tokens per byte than both would exceed the estimate.
# Prompt families tokenize differently (review prompts
# hold more JSON punctuation than correction prompts), so each stage uses its
# own lowest measured ratio; an unlisted stage uses the lowest ratio of all.
# A 5% margin lowers each ratio further so the token estimate rounds up.
_CONTEXT_GUARD_CALIBRATION_SOURCES = (
    {
        "stage": "call1",
        "prompt_version": "authoring-call1-v14",
        "model_facing_utf8_bytes": 76_665,
        "provider_reported_prompt_tokens": 19_938,
    },
    {
        "stage": "call2",
        "prompt_version": "authoring-call2-v18",
        "model_facing_utf8_bytes": 78_535,
        "provider_reported_prompt_tokens": 20_412,
    },
    {
        "stage": "correction",
        "prompt_version": "authoring-correction-v21",
        "model_facing_utf8_bytes": 84_275,
        "provider_reported_prompt_tokens": 21_179,
    },
    {
        "stage": "plan_review",
        "prompt_version": "authoring-plan-review-v13",
        "model_facing_utf8_bytes": 73_134,
        "provider_reported_prompt_tokens": 19_642,
    },
    {
        "stage": "artifact_review",
        "prompt_version": "authoring-artifact-review-v6",
        "model_facing_utf8_bytes": 80_079,
        "provider_reported_prompt_tokens": 21_310,
    },
    {
        "stage": "call1",
        "prompt_version": "authoring-call1-v17",
        "model_facing_utf8_bytes": 80_722,
        "provider_reported_prompt_tokens": 18_946,
    },
    {
        "stage": "correction",
        "prompt_version": "authoring-correction-v28",
        "model_facing_utf8_bytes": 81_894,
        "provider_reported_prompt_tokens": 19_054,
    },
    {
        "stage": "plan_review",
        "prompt_version": "authoring-plan-review-v15",
        "model_facing_utf8_bytes": 66_229,
        "provider_reported_prompt_tokens": 15_503,
    },
)


def _measured_ratio(record: Mapping[str, Any]) -> Fraction:
    return Fraction(
        record["model_facing_utf8_bytes"],
        record["provider_reported_prompt_tokens"],
    )


_CONTEXT_GUARD_MARGIN = Fraction(5, 100)
_CONTEXT_GUARD_OBSERVED_STAGE_RATIOS = {
    stage: min(
        _measured_ratio(record)
        for record in _CONTEXT_GUARD_CALIBRATION_SOURCES
        if record["stage"] == stage
    )
    for stage in dict.fromkeys(record["stage"] for record in _CONTEXT_GUARD_CALIBRATION_SOURCES)
}
_CONTEXT_GUARD_OBSERVED_RATIO = min(_CONTEXT_GUARD_OBSERVED_STAGE_RATIOS.values())
_CONTEXT_GUARD_STAGE_RATIOS = {
    stage: ratio * (1 - _CONTEXT_GUARD_MARGIN)
    for stage, ratio in _CONTEXT_GUARD_OBSERVED_STAGE_RATIOS.items()
}
_CONTEXT_GUARD_CALIBRATED_RATIO = _CONTEXT_GUARD_OBSERVED_RATIO * (1 - _CONTEXT_GUARD_MARGIN)


def _context_guard_ratio(stage: str) -> Fraction:
    """Return the calibrated bytes-per-token ratio for one prompt stage."""

    return _CONTEXT_GUARD_STAGE_RATIOS.get(stage, _CONTEXT_GUARD_CALIBRATED_RATIO)


def _enforce_prompt_size(
    packet: PromptPacket,
    maximum: int,
    *,
    allowed_urls: Collection[str] = (),
) -> None:
    assert_no_prompt_secrets(packet, allowed_urls=allowed_urls)
    assert_no_prompt_duplicates(packet)
    if maximum <= 0:
        raise PromptOverflowError("prompt size limit must be positive")
    rendered = packet.byte_size
    if rendered > maximum:
        estimate = _context_budget_estimate(packet)
        remaining_input_budget_estimate = (
            AUTHORING_CONTEXT_WINDOW_TOKENS
            - AUTHORING_MAX_COMPLETION_TOKENS
            - _CONTEXT_FRAMING_TOKEN_RESERVE
        )
        raise PromptOverflowError(
            f"{packet.stage} prompt exceeds the rendered-prompt byte limit: "
            f"rendered_bytes={rendered}, limit_bytes={maximum}, "
            f"estimated_prompt_tokens={estimate['estimated_prompt_tokens']}, "
            f"remaining_input_budget_estimate={remaining_input_budget_estimate}; "
            "supply an explicitly scoped input package",
            estimated_prompt_tokens=estimate["estimated_prompt_tokens"],
            remaining_input_budget=remaining_input_budget_estimate,
            total_model_facing_utf8_bytes=estimate["model_facing_utf8_bytes"],
        )


def _enforce_context_budget(
    packet: PromptPacket,
    *,
    context_window_tokens: int,
    max_completion_tokens: int,
) -> dict[str, int | float | str]:
    """Estimate model-facing prompt tokens and reject before dispatch if needed."""

    if context_window_tokens <= 0:
        raise PromptOverflowError("context window token limit must be positive")
    if max_completion_tokens <= 0:
        raise PromptOverflowError("completion token limit must be positive")
    estimate = _context_budget_estimate(packet)
    estimated_prompt_tokens = estimate["estimated_prompt_tokens"]
    model_facing_utf8_bytes = estimate["model_facing_utf8_bytes"]
    reserved = max_completion_tokens + _CONTEXT_FRAMING_TOKEN_RESERVE
    remaining_input_budget = context_window_tokens - reserved
    if estimated_prompt_tokens > remaining_input_budget:
        raise PromptOverflowError(
            f"{packet.stage} prompt exceeds the context window: "
            f"estimated_prompt_tokens={estimated_prompt_tokens}, "
            f"remaining_input_budget_estimate={remaining_input_budget}, "
            f"model-facing UTF-8-byte input estimate={model_facing_utf8_bytes}; "
            f"input budget excludes {max_completion_tokens} completion tokens and "
            f"{_CONTEXT_FRAMING_TOKEN_RESERVE} framing tokens in a "
            f"{context_window_tokens}-token context window",
            estimated_prompt_tokens=estimated_prompt_tokens,
            remaining_input_budget=remaining_input_budget,
            total_model_facing_utf8_bytes=model_facing_utf8_bytes,
        )
    return estimate


def _context_budget_estimate(packet: PromptPacket) -> dict[str, int | float | str]:
    """Return a conservative token estimate from every model-facing UTF-8 byte."""

    system_utf8_bytes = len(packet.system.encode("utf-8"))
    user_utf8_bytes = len(packet.user.encode("utf-8"))
    model_facing_utf8_bytes = (
        system_utf8_bytes + user_utf8_bytes + _CONTEXT_MESSAGE_SCHEMA_OVERHEAD_BYTES
    )
    ratio = _context_guard_ratio(packet.stage)
    return {
        # Keep the byte fields explicit. Token estimates use the estimate
        # suffix and calibrated ratio below.
        "estimator": "utf8_bytes_conservative_prompt_estimate",
        "system_bytes": system_utf8_bytes,
        "user_bytes": user_utf8_bytes,
        "schema_message_overhead_bytes": _CONTEXT_MESSAGE_SCHEMA_OVERHEAD_BYTES,
        "estimated_prompt_bytes": model_facing_utf8_bytes,
        "system_utf8_bytes": system_utf8_bytes,
        "user_utf8_bytes": user_utf8_bytes,
        "schema_message_overhead_utf8_bytes": _CONTEXT_MESSAGE_SCHEMA_OVERHEAD_BYTES,
        "model_facing_utf8_bytes": model_facing_utf8_bytes,
        "calibrated_bytes_per_token_estimate": float(ratio),
        "estimated_prompt_tokens": math.ceil(Fraction(model_facing_utf8_bytes, 1) / ratio),
    }
