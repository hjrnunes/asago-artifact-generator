from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from scripts.fresh_trial.accounting import PersistedAuthoringBudget
from scripts.fresh_trial.run_fresh_authoring_trial import (
    CASE_ORDER,
    run_authoring_batch,
)

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
    ScriptedAuthoringTransport,
    _context_budget_estimate,
    _enforce_context_budget,
)

from .test_fresh_authoring_trial import _case_inputs, _successful_responses
from .test_versioned_authoring_wire import (
    _inventory,
    _plan,
    _runtime_contract,
    _view,
)


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


def test_saved_provider_usage_records_replay_at_or_above_reported_prompt_tokens() -> None:
    consumer_root = Path(__file__).resolve().parents[1]
    sources = CONTEXT_GUARD_CALIBRATION["sources"]

    assert len(sources) == 3
    observed_ratios: list[float] = []
    for source in sources:
        sidecar = json.loads((consumer_root / source["path"]).read_text(encoding="utf-8"))
        task_id, dispatch_label = source["record_id"].rsplit("#", maxsplit=1)
        dispatch_index = int(dispatch_label.removeprefix("dispatch-"))
        record = next(
            item
            for item in sidecar["attempts"]
            if item.get("task_id") == task_id and item.get("dispatch_index") == dispatch_index
        )

        prompt = record["prompt"]
        usage_record = record["usage"]
        assert record["stage"] == "plan_review"
        assert prompt["version"] == source["prompt_version"]
        assert record["controls"]["value"]["model"] == source["model"]
        assert record["controls"]["value"]["review_model_profile"] == source["model_profile"]
        assert usage_record["availability"] == "available"
        usage = usage_record["value"]
        reported_prompt_tokens = usage["prompt_tokens"]
        assert isinstance(reported_prompt_tokens, int)
        assert not isinstance(reported_prompt_tokens, bool)

        independently_measured_bytes = (
            len(prompt["system"].encode("utf-8")) + len(prompt["user"].encode("utf-8")) + 128
        )
        replay = _context_budget_estimate(
            PromptPacket(
                stage=record["stage"],
                version=prompt["version"],
                system=prompt["system"],
                user=prompt["user"],
                payload={},
            )
        )

        assert independently_measured_bytes == source["model_facing_utf8_bytes"]
        assert reported_prompt_tokens == source["provider_reported_prompt_tokens"]
        assert replay["model_facing_utf8_bytes"] == independently_measured_bytes
        assert replay["estimated_prompt_tokens"] >= reported_prompt_tokens
        observed_ratios.append(independently_measured_bytes / reported_prompt_tokens)

    assert CONTEXT_GUARD_CALIBRATION["observed_conservative_bytes_per_token"] == min(
        observed_ratios
    )
    assert CONTEXT_GUARD_CALIBRATION["margin"] == 0.12
    assert CONTEXT_GUARD_CALIBRATION["ratio_formula"] == (
        "calibrated_ratio = observed_conservative_ratio * (1 - margin)"
    )
    assert CONTEXT_GUARD_CALIBRATION["calibrated_bytes_per_token"] == pytest.approx(
        min(observed_ratios) * 0.88
    )


def test_call1_overflow_is_case_local_and_does_not_spend_a_dispatch(tmp_path: Path) -> None:
    cases = list(_case_inputs())
    oversized_view = replace(
        cases[0].input_view,
        gherkin_text="x" * 100_000,
    )
    cases[0] = replace(cases[0], input_view=oversized_view)
    run_dir = tmp_path / "fresh-consumer-five-case-20260923T000010Z"
    run_dir.mkdir()
    budget = PersistedAuthoringBudget.load(run_dir / "authoring" / "budget-ledger.json")
    transport = _ContextGuardedScriptedTransport(_successful_responses(4))

    status = run_authoring_batch(
        run_dir,
        cases=cases,
        budget=budget,
        transport_factory=lambda: transport,
        raw_evidence_root=tmp_path / "raw",
    )

    assert status["status"] == "completed"
    assert status["cases"]["G07"]["status"] == "prompt_overflow"
    assert status["cases"]["G07"]["reason"] == "prompt_overflow"
    assert status["cases"]["G07"]["prompt_overflow"]["estimated_prompt_tokens"] > 24_320
    assert status["cases"]["G07"]["prompt_overflow"]["remaining_input_budget_estimate"] == 24_320
    assert status["cases"]["A03"]["status"] == "accepted"
    assert all(status["cases"][case_id]["status"] == "accepted" for case_id in CASE_ORDER[1:])

    receipt = json.loads(
        (run_dir / "authoring" / "G07" / "case-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "prompt_overflow"
    assert receipt["reason"] == "prompt_overflow"
    assert receipt["terminal_stage"] == "call1"
    assert receipt["prompt_overflow"] == status["cases"]["G07"]["prompt_overflow"]
    assert budget.dispatched_by_task.get("G07", 0) == 0
    assert budget.total_dispatched == 16
    assert len(transport.requests) == 16
    assert all(reservation["task_id"] != "G07" for reservation in budget.reservations)
    saved_budget = json.loads(
        (run_dir / "authoring" / "budget-ledger.json").read_text(encoding="utf-8")
    )
    assert saved_budget["total_dispatched"] == 16
    assert all(reservation["task_id"] != "G07" for reservation in saved_budget["reservations"])


def test_rendered_prompt_size_overflow_reports_estimates_and_is_case_local(
    tmp_path: Path,
) -> None:
    cases = list(_case_inputs())
    oversized_view = replace(
        cases[0].input_view,
        gherkin_text="x" * MAX_RENDERED_PROMPT_BYTES,
    )
    cases[0] = replace(cases[0], input_view=oversized_view)
    run_dir = tmp_path / "fresh-consumer-five-case-20260923T000012Z"
    run_dir.mkdir()
    budget = PersistedAuthoringBudget.load(run_dir / "authoring" / "budget-ledger.json")
    transport = _ContextGuardedScriptedTransport(_successful_responses(4))

    status = run_authoring_batch(
        run_dir,
        cases=cases,
        budget=budget,
        transport_factory=lambda: transport,
        raw_evidence_root=tmp_path / "raw",
    )

    assert status["status"] == "completed"
    assert status["cases"]["G07"]["status"] == "prompt_overflow"
    overflow = status["cases"]["G07"]["prompt_overflow"]
    assert overflow["estimated_prompt_tokens"] > 24_320
    assert overflow["remaining_input_budget_estimate"] == 24_320
    assert status["cases"]["A03"]["status"] == "accepted"
    assert all(status["cases"][case_id]["status"] == "accepted" for case_id in CASE_ORDER[1:])
    assert budget.dispatched_by_task.get("G07", 0) == 0
    assert budget.total_dispatched == 16
    assert len(transport.requests) == 16

    receipt = json.loads(
        (run_dir / "authoring" / "G07" / "case-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "prompt_overflow"
    assert receipt["reason"] == "prompt_overflow"
    assert receipt["terminal_stage"] == "call1"
    assert receipt["prompt_overflow"] == overflow
    detail = receipt["findings_summary"][0]["detail"]
    assert "estimated_prompt_tokens=" in detail
    assert "remaining_input_budget_estimate=" in detail
    assert receipt["dispatch_count"] == 0
    saved_budget = json.loads(
        (run_dir / "authoring" / "budget-ledger.json").read_text(encoding="utf-8")
    )
    assert saved_budget["total_dispatched"] == 16
    assert all(reservation["task_id"] != "G07" for reservation in saved_budget["reservations"])


def test_later_correction_overflow_is_case_local_and_does_not_spend_correction_dispatch(
    tmp_path: Path,
) -> None:
    cases = list(_case_inputs())
    responses = [
        *_successful_responses(1)[:2],
        b"not-json" + b"x" * 100_000,
        *_successful_responses(4),
    ]
    run_dir = tmp_path / "fresh-consumer-five-case-20260923T000011Z"
    run_dir.mkdir()
    budget = PersistedAuthoringBudget.load(run_dir / "authoring" / "budget-ledger.json")
    transport = _ContextGuardedScriptedTransport(responses)

    status = run_authoring_batch(
        run_dir,
        cases=cases,
        budget=budget,
        transport_factory=lambda: transport,
        raw_evidence_root=tmp_path / "raw",
    )

    assert status["status"] == "completed"
    assert status["cases"]["G07"]["status"] == "prompt_overflow"
    assert status["cases"]["G07"]["reason"] == "prompt_overflow"
    assert status["cases"]["G07"]["prompt_overflow"]["estimated_prompt_tokens"] > 24_320
    assert status["cases"]["A03"]["status"] == "accepted"

    receipt = json.loads(
        (run_dir / "authoring" / "G07" / "case-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["terminal_stage"] == "correction"
    assert receipt["reason"] == "prompt_overflow"
    assert receipt["dispatch_count"] == 3
    assert receipt["prompt_overflow"]["remaining_input_budget_estimate"] == 24_320
    assert [request["stage"] for request in transport.requests[:4]] == [
        "call1",
        "plan_review",
        "call2",
        "call1",
    ]
    assert sum(reservation["task_id"] == "G07" for reservation in budget.reservations) == 3
    assert budget.dispatched_by_task_role["G07"]["author"] == 2
    assert budget.dispatched_by_task_role["G07"]["reviewer"] == 1
    assert budget.total_dispatched == 19
    assert len(transport.requests) == 19
    saved_budget = json.loads(
        (run_dir / "authoring" / "budget-ledger.json").read_text(encoding="utf-8")
    )
    g07_reservations = [
        reservation
        for reservation in saved_budget["reservations"]
        if reservation["task_id"] == "G07"
    ]
    assert [reservation["stage"] for reservation in g07_reservations] == [
        "call1",
        "plan_review",
        "call2",
    ]
    assert saved_budget["total_dispatched"] == 19


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
