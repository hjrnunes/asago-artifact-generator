"""Authoring rejects the stimulus slots that execution rejects, in every turn."""

from __future__ import annotations

from typing import Any

import pytest

from asago_artifact_generator.authoring.checks import (
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
)

from .support import world_builders

_inventory, _runtime_contract, _plan, _metadata = world_builders(
    "refund", "inventory", "runtime_contract", "plan", "metadata"
)

_SLOT_CODES = {"undeclared_slot", "non_scalar_slot"}


def _binding(name: str, expected_type: str, selector: str = "value") -> dict[str, Any]:
    return {
        "name": name,
        "expected_type": expected_type,
        "source_kind": "supplied_input",
        "source_ref": "facts:order:owned",
        "selector": selector,
        "consumers": ["judge.value"],
        "on_missing": "stop",
    }


def _plan_slot_findings(request: str, history: list[str], *bindings: dict[str, Any]) -> list[Any]:
    plan = _plan()
    plan["runtime_bindings"] = list(bindings)
    plan["stimulus_approach"]["request"] = request
    plan["stimulus_approach"]["history"] = history
    findings = collect_plan_findings_v2(plan, _inventory(), _runtime_contract())
    return [finding for finding in findings if finding.code in _SLOT_CODES]


def _artifact_slot_findings(
    user_text: str, history: list[str], *bindings: dict[str, Any]
) -> list[Any]:
    plan = _plan()
    plan["runtime_bindings"] = list(bindings)
    metadata = _metadata()
    metadata["stimulus"] = {
        "user_text": user_text,
        "delivery": plan["stimulus_approach"]["delivery"],
        "history": [{"role": "user", "content": turn} for turn in history],
        "slots": [],
    }
    findings = collect_artifact_findings_v2(metadata, plan, _inventory(), _runtime_contract())
    return [finding for finding in findings if finding.code in _SLOT_CODES]


def _summary(findings: list[Any]) -> list[tuple[str, str]]:
    return [(finding.code, finding.path) for finding in findings]


def test_an_undeclared_slot_in_an_earlier_plan_turn_is_flagged() -> None:
    findings = _plan_slot_findings(
        "Refund it.", ["My account is {{undeclared_account}}."], _binding("order_id", "string")
    )

    assert _summary(findings) == [
        ("undeclared_slot", "stimulus_approach.history[0]:undeclared_account")
    ]
    assert "undeclared_account" in findings[0].detail
    assert "order_id" in findings[0].detail


def test_an_undeclared_slot_in_an_earlier_artifact_turn_is_flagged() -> None:
    findings = _artifact_slot_findings(
        "Refund it.", ["My account is {{undeclared_account}}."], _binding("order_id", "string")
    )

    assert _summary(findings) == [
        ("undeclared_slot", "stimulus.history[0].content:undeclared_account")
    ]
    assert "undeclared_account" in findings[0].detail
    assert "order_id" in findings[0].detail


@pytest.mark.parametrize("expected_type", ["object", "array"])
def test_a_non_scalar_binding_in_the_final_plan_request_is_flagged(expected_type: str) -> None:
    findings = _plan_slot_findings("Refund {{order}}.", [], _binding("order", expected_type))

    assert _summary(findings) == [("non_scalar_slot", "stimulus_approach.request:order")]
    assert expected_type in findings[0].detail


@pytest.mark.parametrize("expected_type", ["object", "array"])
def test_a_non_scalar_binding_in_the_final_artifact_text_is_flagged(expected_type: str) -> None:
    findings = _artifact_slot_findings("Refund {{order}}.", [], _binding("order", expected_type))

    assert _summary(findings) == [("non_scalar_slot", "stimulus.user_text:order")]
    assert expected_type in findings[0].detail


def test_a_non_scalar_binding_in_history_is_flagged_in_plan_and_artifact() -> None:
    binding = _binding("order", "object")

    plan = _plan_slot_findings("Hello.", ["Order {{order}}."], binding)
    artifact = _artifact_slot_findings("Hello.", ["Order {{order}}."], binding)

    assert _summary(plan) == [("non_scalar_slot", "stimulus_approach.history[0]:order")]
    assert _summary(artifact) == [("non_scalar_slot", "stimulus.history[0].content:order")]


def test_a_non_scalar_binding_that_no_text_interpolates_is_not_flagged() -> None:
    binding = _binding("order", "object")

    assert _plan_slot_findings("Hello.", ["Hi."], binding) == []
    assert _artifact_slot_findings("Hello.", ["Hi."], binding) == []


def test_a_scalar_binding_used_only_in_history_passes() -> None:
    binding = _binding("order_id", "string", "value.id")

    assert _plan_slot_findings("Refund it.", ["Order {{order_id}}."], binding) == []
    assert _artifact_slot_findings("Refund it.", ["Order {{order_id}}."], binding) == []


def test_each_turn_reports_its_own_undeclared_slots() -> None:
    findings = _artifact_slot_findings(
        "Use {{first_missing}}.",
        ["Use {{second_missing}}."],
        _binding("order_id", "string"),
    )

    assert _summary(findings) == [
        ("undeclared_slot", "stimulus.user_text:first_missing"),
        ("undeclared_slot", "stimulus.history[0].content:second_missing"),
    ]
    assert "second_missing" not in findings[0].detail
    assert "first_missing" not in findings[1].detail
