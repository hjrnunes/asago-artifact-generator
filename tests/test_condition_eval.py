"""The consumer's tool-call condition evaluator reproduces the tool-call-condition-v1 vectors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.condition_eval import (
    evaluate_condition,
    validate_condition,
)

_VECTORS = (
    Path(__file__).resolve().parents[1]
    / "contracts/tool-call-condition/tool-call-condition-v1/vectors"
)


def _vectors(name: str) -> list[dict[str, Any]]:
    return json.loads((_VECTORS / name).read_text(encoding="utf-8"))["vectors"]


_EVALUATE = _vectors("evaluate.json")
_VALIDATE = _vectors("validate.json")


@pytest.mark.parametrize("vector", _EVALUATE, ids=[v["id"] for v in _EVALUATE])
def test_evaluate_vector(vector: dict[str, Any]) -> None:
    result = evaluate_condition(vector["condition"], vector["calls"], vector["complete"])

    assert (result.outcome, result.reason, list(result.matched_calls)) == (
        vector["outcome"],
        vector["reason"],
        vector["matched_calls"],
    )


@pytest.mark.parametrize("vector", _VALIDATE, ids=[v["id"] for v in _VALIDATE])
def test_validate_vector(vector: dict[str, Any]) -> None:
    problem = validate_condition(vector["condition"])

    assert (problem is None) == vector["valid"]
    assert problem == vector["error"]


_ARGUMENT = {"source": "argument", "operation": "a", "argument": "x"}


def _value(op: str, right: Any) -> dict[str, Any]:
    return {
        "comparisons": [
            {
                "kind": "value",
                "left": _ARGUMENT,
                "op": op,
                "right": {"source": "literal", "value": right},
            }
        ]
    }


@pytest.mark.parametrize(
    "condition",
    [
        {"comparisons": [{"kind": ["value"]}]},
        {"comparisons": [{"kind": {"k": 1}}]},
        {"comparisons": [{"kind": "value", "left": {"source": []}, "op": "eq", "right": 1}]},
        {"comparisons": [{"kind": "value", "left": {"source": {}}, "op": "eq", "right": 1}]},
        {"comparisons": [{"kind": "value", "left": _ARGUMENT, "op": {"x": 1}, "right": 1}]},
    ],
)
def test_unhashable_discriminators_are_problems_not_crashes(condition: dict[str, Any]) -> None:
    problem = validate_condition(condition)

    assert isinstance(problem, str)
    assert "unknown" in problem


@pytest.mark.parametrize("op", ["eq", "ne", "gt", "ge", "lt", "le"])
def test_integers_beyond_float_range_saturate(op: str) -> None:
    calls = [{"name": "a", "arguments": {"x": 10**400}}]

    result = evaluate_condition(_value(op, 10**401), calls, True)

    expected = {"eq": True, "ne": False, "gt": False, "ge": True, "lt": False, "le": True}[op]
    assert (result.outcome == "detected") is expected


def test_negative_integer_beyond_float_range_is_negative_infinity() -> None:
    calls = [{"name": "a", "arguments": {"x": -(10**400)}}]

    assert evaluate_condition(_value("lt", 0), calls, True).outcome == "detected"
    assert evaluate_condition(_value("eq", -(10**500)), calls, True).outcome == "detected"


def test_huge_integers_in_where_and_membership_do_not_crash() -> None:
    calls = [{"name": "a", "arguments": {"x": 10**400}}]
    where = {
        "comparisons": [
            {
                "kind": "not_called",
                "operation": "a",
                "where": [{"argument": "x", "value": 10**400}],
            }
        ]
    }

    assert evaluate_condition(where, calls, True).outcome == "not_detected"
    assert evaluate_condition(_value("in", [10**400]), calls, True).outcome == "detected"
    assert evaluate_condition(_value("in", [True]), calls, True).outcome == "not_detected"


def test_a_call_without_an_arguments_key_is_inconclusive() -> None:
    # The contract's call always carries "arguments"; this reader also accepts
    # a call that omits the key and treats it like unusable arguments.
    assert evaluate_condition(_value("eq", 1), [{"name": "a"}], True).outcome == "inconclusive"
