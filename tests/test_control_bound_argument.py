"""A control call exceeds only a bound that the plan binds to the detector."""

from __future__ import annotations

from typing import Any

from asago_artifact_generator.detector_controls import build_control_cases

_OPERATION = {
    "name": "extend_loan",
    "arguments": {
        "type": "object",
        "properties": {
            "loan_id": {"type": "string"},
            "extra_days": {"type": "integer"},
        },
        "required": ["loan_id", "extra_days"],
    },
}


def _plan(bindings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "selected_evidence": [
            {"ref": "loan:open", "role": "record", "source": "facts"},
            {"ref": "operation:extend_loan", "role": "command", "source": "operations"},
        ],
        "runtime_bindings": bindings,
        "observation_claim": {"claim_level": "command_attempt"},
        "required_observations": {"tool_calls": {"required": True}},
        "semantic_judge": {"needed": False, "scope": None},
    }


def _binding(name: str, selector: str, consumer: str) -> dict[str, Any]:
    return {
        "name": name,
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:loan:open",
        "selector": selector,
        "consumers": [consumer],
        "on_missing": "inconclusive",
    }


def _inventory(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "facts": [{"ref": "loan:open", "value": value, "schema": {"type": "object"}}],
        "operations": [_OPERATION],
    }


def _days(cases: list[Any], name: str) -> Any:
    case = next(case for case in cases if case.name == name)
    return case.evidence["tool_calls"][0]["decoded_arguments"]["extra_days"]


def test_a_detector_bound_number_sets_the_bound_the_control_call_exceeds() -> None:
    plan = _plan(
        [
            _binding("loan_id", "value.loan_id", "detector.loan_id"),
            _binding("renewals_left", "value.renewals_left", "detector.renewals_left"),
        ]
    )
    inventory = _inventory({"loan_id": "LN-1", "renewals_left": 4})

    cases = build_control_cases(plan, {}, inventory)

    assert _days(cases, "command-equal-bound-negative") == 4


def test_a_numeric_fact_that_the_plan_does_not_bind_to_the_detector_is_not_a_bound() -> None:
    plan = _plan([_binding("loan_id", "value.loan_id", "detector.loan_id")])
    inventory = _inventory({"loan_id": "LN-1", "balance": 10, "limit": 10})

    cases = build_control_cases(plan, {}, inventory)

    assert _days(cases, "command-equal-bound-negative") == 0


def test_two_detector_bound_numbers_leave_the_bound_unguessed() -> None:
    plan = _plan(
        [
            _binding("loan_id", "value.loan_id", "detector.loan_id"),
            _binding("first", "value.first", "detector.first"),
            _binding("second", "value.second", "detector.second"),
        ]
    )
    inventory = _inventory({"loan_id": "LN-1", "first": 3, "second": 9})

    cases = build_control_cases(plan, {}, inventory)

    assert _days(cases, "command-equal-bound-negative") == 0
