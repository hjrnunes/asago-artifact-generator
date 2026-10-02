from __future__ import annotations

import json
import math
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import (
    AUTHORING_CONTEXT_WINDOW_TOKENS,
    AUTHORING_MAX_COMPLETION_TOKENS,
    CONTEXT_GUARD_CALIBRATION,
    MAX_RENDERED_PROMPT_BYTES,
    AuthoringBudget,
    AuthoringOrchestrator,
    AuthoringPolicy,
    PromptOverflowError,
    PromptPacket,
    _context_budget_estimate,
    _context_guard_ratio,
    _enforce_context_budget,
)
from tests.test_versioned_authoring_wire import _inventory, _plan, _runtime_contract, _view

from .support import ScriptedAuthoringTransport, stage_local_orchestrator


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


def _packet_with_model_facing_bytes(total_bytes: int, stage: str = "call1") -> PromptPacket:
    system = "é"
    user = "x" * (total_bytes - len(system.encode("utf-8")) - 128)
    return PromptPacket(
        stage=stage,
        version="context-guard-boundary-test",
        system=system,
        user=user,
        payload={},
    )


_STAGES = ("call1", "call2", "correction", "plan_review", "artifact_review")


def test_calibration_applies_the_stated_margin_below_every_measured_stage_ratio() -> None:
    sources = CONTEXT_GUARD_CALIBRATION["sources"]
    margin = Fraction(CONTEXT_GUARD_CALIBRATION["margin_fraction"])

    assert margin == Fraction(5, 100)
    assert all("model" not in source for source in sources)
    for stage in _STAGES:
        measured = min(
            Fraction(source["model_facing_utf8_bytes"], source["provider_reported_prompt_tokens"])
            for source in sources
            if source["stage"] == stage
        )
        assert _context_guard_ratio(stage) == measured * (1 - margin)
    lowest = min(
        Fraction(source["model_facing_utf8_bytes"], source["provider_reported_prompt_tokens"])
        for source in sources
    )
    assert _context_guard_ratio("legacy-stage") == lowest * (1 - margin)


def test_every_measured_prompt_estimates_at_least_its_provider_token_count() -> None:
    for source in CONTEXT_GUARD_CALIBRATION["sources"]:
        packet = _packet_with_model_facing_bytes(
            source["model_facing_utf8_bytes"], source["stage"]
        )
        estimate = _context_budget_estimate(packet)["estimated_prompt_tokens"]
        assert estimate >= source["provider_reported_prompt_tokens"], source


def test_correction_prompts_the_previous_ratio_rejected_now_fit_the_input_budget() -> None:
    remaining_input_budget = (
        AUTHORING_CONTEXT_WINDOW_TOKENS - AUTHORING_MAX_COMPLETION_TOKENS - 256
    )
    # Step 2 rejected correction prompts of these sizes at 3.489 bytes per token;
    # measured correction prompts need 3.98 or more bytes per token.
    for total_bytes in (85_051, 87_615, 89_494):
        packet = _packet_with_model_facing_bytes(total_bytes, "correction")
        estimate = _enforce_context_budget(
            packet,
            context_window_tokens=AUTHORING_CONTEXT_WINDOW_TOKENS,
            max_completion_tokens=AUTHORING_MAX_COMPLETION_TOKENS,
        )
        assert estimate["estimated_prompt_tokens"] <= remaining_input_budget


def test_context_guard_fits_exact_input_budget_and_rejects_one_estimated_token_over() -> None:
    remaining_input_budget = (
        AUTHORING_CONTEXT_WINDOW_TOKENS - AUTHORING_MAX_COMPLETION_TOKENS - 256
    )
    boundary_bytes = math.floor(remaining_input_budget * _context_guard_ratio("call1"))
    exact_boundary_packet = _packet_with_model_facing_bytes(boundary_bytes)
    estimate = _context_budget_estimate(exact_boundary_packet)

    assert estimate["model_facing_utf8_bytes"] == boundary_bytes
    assert estimate["estimated_prompt_tokens"] == remaining_input_budget
    assert (
        _enforce_context_budget(
            exact_boundary_packet,
            context_window_tokens=AUTHORING_CONTEXT_WINDOW_TOKENS,
            max_completion_tokens=AUTHORING_MAX_COMPLETION_TOKENS,
        )["estimated_prompt_tokens"]
        == remaining_input_budget
    )

    one_token_over_packet = _packet_with_model_facing_bytes(boundary_bytes + 1)
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

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="call1-overflow",
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

    result = stage_local_orchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="rendered-prompt-overflow",
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
