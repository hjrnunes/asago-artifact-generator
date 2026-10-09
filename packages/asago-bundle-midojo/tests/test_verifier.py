"""The bundle's verifier follows tool-call-condition-v1 and writes the three-way verdict."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from asago_artifact_generator.authoring import condition_eval
from asago_bundle_midojo.bundle_files import asago_verifiers as verifiers

VECTORS = (
    Path(__file__).resolve().parents[3]
    / "contracts/tool-call-condition/tool-call-condition-v1/vectors"
)


def load(name: str) -> list[dict[str, Any]]:
    return json.loads((VECTORS / name).read_text(encoding="utf-8"))["vectors"]


EVALUATE = load("evaluate.json")
VALIDATE = load("validate.json")
ONE_CALL = {
    "comparisons": [
        {
            "kind": "value",
            "left": {"source": "argument", "operation": "a", "argument": "x"},
            "op": "eq",
            "right": {"source": "literal", "value": 3},
        }
    ]
}


def record(name: str, arguments: Any, error: str | None = None) -> SimpleNamespace:
    """A MiDojo function-call record, reduced to the fields the verifier reads."""

    return SimpleNamespace(function=name, args=arguments, error=error, result="", blocked=False)


def context(calls: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        agent_output="", function_calls=calls, pre_environment=None, post_environment=None
    )


def captured(vector: dict[str, Any]) -> list[SimpleNamespace]:
    """Return the vector's calls as records; an incomplete capture ends in an unsettled call."""

    calls = [record(call["name"], call["arguments"]) for call in vector["calls"]]
    if vector["complete"]:
        return calls
    if calls:
        calls[-1].error = verifiers.UNSETTLED
        return calls
    return [record("unsettled", {}, verifiers.UNSETTLED)]


def spec(path: Path, condition: dict[str, Any] = ONE_CALL) -> dict[str, Any]:
    return {"condition": condition, "verdict_file": str(path)}


@pytest.mark.parametrize("vector", EVALUATE, ids=[v["id"] for v in EVALUATE])
def test_every_evaluate_vector_gives_the_stated_verdict(
    vector: dict[str, Any], tmp_path: Path
) -> None:
    verifier = verifiers.ToolCallConditionVerifier()
    verdict_file = tmp_path / "verdict.json"
    parsed = verifier.parse(spec(verdict_file, vector["condition"]))

    verdict = verifier.grade(parsed, context(captured(vector)))

    assert (verdict.outcome, verdict.reason, list(verdict.matched_calls)) == (
        vector["outcome"],
        vector["reason"],
        vector["matched_calls"],
    )
    written = json.loads(verdict_file.read_text(encoding="utf-8"))
    assert written["outcome"] == vector["outcome"]


@pytest.mark.parametrize("vector", EVALUATE, ids=[v["id"] for v in EVALUATE])
def test_the_bundle_copy_matches_the_consumer_evaluator(vector: dict[str, Any]) -> None:
    mine = verifiers.evaluate_condition(vector["condition"], vector["calls"], vector["complete"])
    theirs = condition_eval.evaluate_condition(
        vector["condition"], vector["calls"], vector["complete"]
    )

    assert (mine.outcome, mine.reason, mine.matched_calls) == (
        theirs.outcome,
        theirs.reason,
        theirs.matched_calls,
    )


@pytest.mark.parametrize("vector", VALIDATE, ids=[v["id"] for v in VALIDATE])
def test_every_validate_vector_gives_the_stated_problem(vector: dict[str, Any]) -> None:
    assert verifiers.validate_condition(vector["condition"]) == vector["error"]
    assert condition_eval.validate_condition(vector["condition"]) == vector["error"]


@pytest.mark.parametrize("vector", VALIDATE, ids=[v["id"] for v in VALIDATE])
def test_parse_accepts_exactly_the_valid_conditions(vector: dict[str, Any]) -> None:
    verifier = verifiers.ToolCallConditionVerifier()
    if vector["valid"]:
        assert verifier.parse(spec(Path("v.json"), vector["condition"])) is not None
    else:
        with pytest.raises(ValueError, match="condition"):
            verifier.parse(spec(Path("v.json"), vector["condition"]))


@pytest.mark.parametrize(
    ("calls", "outcome"),
    [
        ([record("a", {"x": 3})], "detected"),
        ([record("a", {"x": 4})], "not_detected"),
        ([record("a", {"x": 4}, verifiers.UNSETTLED)], "inconclusive"),
    ],
)
def test_each_outcome_is_written_to_the_verdict_file(
    tmp_path: Path, calls: list[SimpleNamespace], outcome: str
) -> None:
    verifier = verifiers.ToolCallConditionVerifier()
    verdict_file = tmp_path / "verdict.json"

    verdict = verifier.grade(verifier.parse(spec(verdict_file)), context(calls))

    written = json.loads(verdict_file.read_text(encoding="utf-8"))
    assert verdict.outcome == outcome
    assert written == {
        "claim_level": "command_attempt",
        "matched_calls": list(verdict.matched_calls),
        "outcome": outcome,
        "reason": verdict.reason,
    }


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"condition": ONE_CALL},
        {"verdict_file": "v.json"},
        {"condition": ONE_CALL, "verdict_file": ""},
        {"condition": ONE_CALL, "verdict_file": 3},
        {"condition": ONE_CALL, "verdict_file": "v.json", "extra": 1},
        {"condition": {"comparisons": []}, "verdict_file": "v.json"},
        "not an object",
    ],
)
def test_parse_refuses_a_malformed_spec(bad: Any) -> None:
    with pytest.raises(ValueError):
        verifiers.ToolCallConditionVerifier().parse(bad)


def test_only_a_detected_verdict_passes_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Result:
        def __init__(self, passed: bool, reason: str) -> None:
            self.passed, self.reason = passed, reason

    module = types.ModuleType("midojo.verifiers")
    module.VerificationResult = Result  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "midojo", types.ModuleType("midojo"))
    monkeypatch.setitem(sys.modules, "midojo.verifiers", module)
    verifier = verifiers.ToolCallConditionVerifier()
    parsed = verifier.parse(spec(tmp_path / "verdict.json"))

    outcomes = {
        "detected": [record("a", {"x": 3})],
        "not_detected": [record("a", {"x": 4})],
        "inconclusive": [record("a", {"x": 4}, verifiers.UNSETTLED)],
    }

    for outcome, calls in outcomes.items():
        result = verifier.assess(parsed, context(calls))
        assert result.passed is (outcome == "detected")
        assert verifier.evaluate(parsed, context(calls)) is result.passed
        assert json.loads((tmp_path / "verdict.json").read_text())["outcome"] == outcome


def test_the_neutral_check_always_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        def __init__(self, passed: bool, reason: str) -> None:
            self.passed, self.reason = passed, reason

    module = types.ModuleType("midojo.verifiers")
    module.VerificationResult = Result  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "midojo", types.ModuleType("midojo"))
    monkeypatch.setitem(sys.modules, "midojo.verifiers", module)
    neutral = verifiers.NeutralVerifier()

    parsed = neutral.parse({})

    assert neutral.evaluate(parsed, context([])) is True
    assert neutral.assess(parsed, context([])).passed is True
    assert "no utility check" in neutral.assess(parsed, context([])).reason
    with pytest.raises(ValueError):
        neutral.parse({"anything": 1})


def test_register_adds_both_verifiers_once(monkeypatch: pytest.MonkeyPatch) -> None:
    registered: list[str] = []

    def register_verifier(verifier: Any) -> None:
        if verifier.name in registered:
            raise ValueError("already registered")
        registered.append(verifier.name)

    module = types.ModuleType("midojo.verifiers")
    module.register_verifier = register_verifier  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "midojo", types.ModuleType("midojo"))
    monkeypatch.setitem(sys.modules, "midojo.verifiers", module)

    verifiers.register()
    verifiers.register()

    assert registered == ["asago_tool_call_condition", "asago_neutral"]
