"""The consumer's tool-call condition evaluator reproduces the tool-call-condition-v2 vectors."""

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
_V1 = "tool-call-condition-v1"
_V2 = "tool-call-condition-v2"
# The v1 evaluate vectors whose result v2 changes; the v2 README names them.
_CHANGED_IN_V2 = {
    "completeness.order-clean.complete-false",
    "order.same-argument-bool-int",
    "order.same-argument-null-both-missing-on-prior",
}


def _vectors(version: str, name: str) -> list[dict[str, Any]]:
    path = _KIT / version / "vectors" / name
    return json.loads(path.read_text(encoding="utf-8"))["vectors"]


_EVALUATE = _vectors(_V2, "evaluate.json")
_VALIDATE = _vectors(_V2, "validate.json")


def test_the_lock_pins_every_kit_file_and_names_v2() -> None:
    lock = json.loads((_KIT / "CONTRACT.lock").read_text(encoding="utf-8"))
    files = {
        str(path.relative_to(_KIT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(_KIT.rglob("*"))
        if path.is_file() and path.name != "CONTRACT.lock"
    }

    assert lock["version"] == _V2
    assert lock["files"] == files
    assert _V2 in (_KIT / "VERSIONING.md").read_text(encoding="utf-8")


def test_v2_changes_only_the_named_v1_evaluate_results() -> None:
    v2 = {vector["id"]: vector for vector in _EVALUATE}
    v1 = _vectors(_V1, "evaluate.json")
    changed = {vector["id"] for vector in v1 if v2[vector["id"]] != vector}

    assert changed == _CHANGED_IN_V2
    assert _vectors(_V1, "validate.json") == _VALIDATE


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
