"""The consumer reads the shared evaluator: the kit's vectors and the producer's behaviour."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.condition_eval import (
    evaluate_condition,
    validate_condition,
)

_KIT = Path(__file__).resolve().parents[1] / "contracts/tool-call-condition"
_KIT_VERSION = "tool-call-condition-v1"


def _vectors(version: str, name: str) -> list[dict[str, Any]]:
    path = _KIT / version / "vectors" / name
    return json.loads(path.read_text(encoding="utf-8"))["vectors"]


_EVALUATE = _vectors(_KIT_VERSION, "evaluate.json")
_VALIDATE = _vectors(_KIT_VERSION, "validate.json")


def test_the_lock_pins_every_kit_file_and_names_its_version() -> None:
    lock = json.loads((_KIT / "CONTRACT.lock").read_text(encoding="utf-8"))
    files = {
        str(path.relative_to(_KIT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(_KIT.rglob("*"))
        if path.is_file() and path.name != "CONTRACT.lock"
    }

    assert lock["version"] == _KIT_VERSION
    assert lock["files"] == files


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


# Every way the consumer's former evaluator differed from the producer's, named by
# the difference it pins. The producer's behaviour wins; ``asago_handoff`` and the
# producer's own suite assert the same cases.
DIFFERENCES: dict[str, tuple[dict[str, Any], list[dict[str, Any]], bool, Any]] = {
    "argument-value-beyond-float-range": (
        _value("gt", 5),
        [{"name": "a", "arguments": {"x": 10**400}}],
        True,
        OverflowError,
    ),
    "literal-value-beyond-float-range": (
        _value("lt", 10**400),
        [{"name": "a", "arguments": {"x": 1}}],
        True,
        OverflowError,
    ),
    "not-called-where-value-beyond-float-range": (
        {
            "comparisons": [
                {
                    "kind": "not_called",
                    "operation": "a",
                    "where": [{"argument": "x", "value": 10**400}],
                }
            ]
        },
        [{"name": "a", "arguments": {"x": 1}}],
        True,
        OverflowError,
    ),
    "two-literal-comparison-is-not-evaluated": (
        {
            "comparisons": [
                {
                    "kind": "value",
                    "left": {"source": "literal", "value": 2},
                    "op": "gt",
                    "right": {"source": "literal", "value": 1},
                }
            ]
        },
        [],
        True,
        ("detected", "the condition holds on the captured calls", []),
    ),
    "call-without-arguments-raises": (_value("eq", 1), [{"name": "a"}], True, KeyError),
    "call-without-name-raises": (_value("eq", 1), [{"arguments": {"x": 1}}], True, KeyError),
}


@pytest.mark.parametrize("case", DIFFERENCES.values(), ids=list(DIFFERENCES))
def test_each_evaluator_difference_pins_the_producer_behaviour(
    case: tuple[dict[str, Any], list[dict[str, Any]], bool, Any],
) -> None:
    condition, calls, complete, expected = case
    if isinstance(expected, type) and issubclass(expected, Exception):
        with pytest.raises(expected):
            evaluate_condition(condition, calls, complete)
        return
    result = evaluate_condition(condition, calls, complete)
    assert (result.outcome, result.reason, result.matched_calls) == expected


def test_the_consumer_name_is_the_shared_evaluator() -> None:
    from asago_handoff import evaluate_condition as shared

    assert evaluate_condition is shared
