from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import (
    AUTHORING_CONTEXT_WINDOW_TOKENS,
    AUTHORING_MAX_COMPLETION_TOKENS,
    MAX_RENDERED_PROMPT_BYTES,
    AuthoringBudget,
    AuthoringOrchestrator,
    AuthoringPolicy,
    PromptOverflowError,
    PromptPacket,
    ScriptedAuthoringTransport,
    _context_budget_estimate,
    _enforce_context_budget,
)
from tests.test_versioned_authoring_wire import _inventory, _plan, _runtime_contract, _view


class _ContextGuardedScriptedTransport:
    max_retries = 0

    def __init__(self, responses: list[bytes]) -> None:
        self.scripted = ScriptedAuthoringTransport(responses)

    @property
    def requests(self) -> list[dict]:
        return self.scripted.requests

    def preflight_context_budget(self, packet: PromptPacket) -> dict:
        return _enforce_context_budget(
            packet,
            context_window_tokens=AUTHORING_CONTEXT_WINDOW_TOKENS,
            max_completion_tokens=AUTHORING_MAX_COMPLETION_TOKENS,
        )

    def complete(self, packet: PromptPacket):
        self.preflight_context_budget(packet)
        return self.scripted.complete(packet)


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


def test_call1_overflow_does_not_spend_a_dispatch(tmp_path: Path) -> None:
    oversized_view = replace(_view(), gherkin_text="x" * 100_000)
    transport = _ContextGuardedScriptedTransport([b"{}"])
    budget = AuthoringBudget(aggregate_limit=8, task_limit=4)

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="call1-overflow",
        wire_version="v2",
        budget=budget,
    ).run(oversized_view, _inventory(), _runtime_contract())

    assert result.status == "prompt_overflow"
    finding = next(finding for finding in result.findings if finding.code == "prompt_overflow")
    assert finding.path == "call1"
    assert finding.details["estimated_prompt_tokens"] > 24_320
    assert finding.details["remaining_input_budget_estimate"] == 24_320
    assert transport.requests == []
    assert result.ledger == []
    assert budget.total_dispatched == 0


def test_rendered_prompt_size_overflow_does_not_spend_a_dispatch(tmp_path: Path) -> None:
    oversized_view = replace(
        _view(),
        gherkin_text="x" * MAX_RENDERED_PROMPT_BYTES,
    )
    transport = ScriptedAuthoringTransport([b"{}"])
    budget = AuthoringBudget(aggregate_limit=8, task_limit=4)

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="rendered-prompt-overflow",
        wire_version="v2",
        budget=budget,
    ).run(oversized_view, _inventory(), _runtime_contract())

    assert result.status == "prompt_overflow"
    finding = next(finding for finding in result.findings if finding.code == "prompt_overflow")
    assert finding.path == "call1"
    assert finding.details["model_facing_utf8_bytes"] > MAX_RENDERED_PROMPT_BYTES
    assert f"limit_bytes={MAX_RENDERED_PROMPT_BYTES}" in finding.detail
    assert transport.requests == []
    assert result.ledger == []
    assert budget.total_dispatched == 0


def test_later_correction_overflow_does_not_spend_author_dispatch(tmp_path: Path) -> None:
    invalid_plan = _plan()
    invalid_plan["unexpected"] = "x" * 100_000
    transport = _ContextGuardedScriptedTransport([json.dumps(invalid_plan).encode("utf-8")])
    budget = AuthoringBudget(
        aggregate_limit=40,
        task_limit=8,
        author_limit=4,
        review_limit=4,
    )
    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="correction-overflow",
        wire_version="v2",
        policy=AuthoringPolicy(),
        budget=budget,
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "prompt_overflow"
    overflow = next(finding for finding in result.findings if finding.code == "prompt_overflow")
    assert overflow.path == "correction"
    assert "correction prompt" in overflow.detail
    assert overflow.details["estimated_prompt_tokens"] > 24_320
    assert overflow.details["remaining_input_budget_estimate"] == 24_320
    assert (
        f"estimated_prompt_tokens={overflow.details['estimated_prompt_tokens']}" in overflow.detail
    )
    assert [request["stage"] for request in transport.requests] == ["call1"]
    assert [record["stage"] for record in result.ledger] == ["call1"]
    assert budget.total_dispatched == 1
    assert budget.dispatched_by_task_role["correction-overflow"]["author"] == 1
    assert budget.dispatched_by_task_role["correction-overflow"].get("reviewer", 0) == 0
