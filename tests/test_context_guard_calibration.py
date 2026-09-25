from __future__ import annotations

import pytest

from asago_artifact_generator.authoring import (
    AUTHORING_CONTEXT_WINDOW_TOKENS,
    AUTHORING_MAX_COMPLETION_TOKENS,
    PromptOverflowError,
    PromptPacket,
    _context_budget_estimate,
    _enforce_context_budget,
)


def _packet_with_model_facing_bytes(total_bytes: int) -> PromptPacket:
    system = "é"
    user = "x" * (total_bytes - len(system.encode("utf-8")) - 128)
    return PromptPacket(
        stage="call1",
        version="context-guard-boundary-test",
        system=system,
        user=user,
        payload={},
    )


def test_context_guard_fits_exact_input_budget_and_rejects_one_estimated_token_over() -> None:
    remaining_input_budget = (
        AUTHORING_CONTEXT_WINDOW_TOKENS - AUTHORING_MAX_COMPLETION_TOKENS - 256
    )
    exact_boundary_packet = _packet_with_model_facing_bytes(84_852)
    estimate = _context_budget_estimate(exact_boundary_packet)

    assert estimate["model_facing_utf8_bytes"] == 84_852
    assert estimate["estimated_prompt_tokens"] == remaining_input_budget
    assert (
        _enforce_context_budget(
            exact_boundary_packet,
            context_window_tokens=AUTHORING_CONTEXT_WINDOW_TOKENS,
            max_completion_tokens=AUTHORING_MAX_COMPLETION_TOKENS,
        )["estimated_prompt_tokens"]
        == remaining_input_budget
    )

    one_token_over_packet = _packet_with_model_facing_bytes(84_853)
    with pytest.raises(PromptOverflowError) as overflow:
        _enforce_context_budget(
            one_token_over_packet,
            context_window_tokens=AUTHORING_CONTEXT_WINDOW_TOKENS,
            max_completion_tokens=AUTHORING_MAX_COMPLETION_TOKENS,
        )

    assert overflow.value.estimated_prompt_tokens == remaining_input_budget + 1
    assert overflow.value.remaining_input_budget == remaining_input_budget
