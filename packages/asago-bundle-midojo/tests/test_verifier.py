"""The bundle's verifier follows tool-call-condition-v1 and writes the three-way verdict."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from asago_bundle_midojo.compiler import SUITE_PACKAGE, compile_package, write_bundle_code

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


@pytest.fixture(scope="module")
def verifiers(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ModuleType]:
    """The verifier module as a bundle imports it, from a suite package the compiler writes."""

    root = tmp_path_factory.mktemp("suite")
    write_bundle_code(root)
    sys.path.insert(0, str(root))
    try:
        yield importlib.import_module(f"{SUITE_PACKAGE}.asago_verifiers")
    finally:
        sys.path.remove(str(root))
        for name in [n for n in sys.modules if n.split(".")[0] == SUITE_PACKAGE]:
            del sys.modules[name]


def record(name: str, arguments: Any, error: str | None = None) -> SimpleNamespace:
    """A MiDojo function-call record, reduced to the fields the verifier reads."""

    return SimpleNamespace(function=name, args=arguments, error=error, result="", blocked=False)


def context(calls: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        agent_output="", function_calls=calls, pre_environment=None, post_environment=None
    )


def captured(vector: dict[str, Any], unsettled: str) -> list[SimpleNamespace]:
    """Return the vector's calls as records; an incomplete capture ends in an unsettled call."""

    calls = [record(call["name"], call["arguments"]) for call in vector["calls"]]
    if vector["complete"]:
        return calls
    if calls:
        calls[-1].error = unsettled
        return calls
    return [record("unsettled", {}, unsettled)]


def spec(path: Path, condition: dict[str, Any] = ONE_CALL) -> dict[str, Any]:
    return {"condition": condition, "verdict_file": str(path)}


@pytest.mark.parametrize("vector", EVALUATE, ids=[v["id"] for v in EVALUATE])
def test_every_evaluate_vector_gives_the_stated_verdict(
    verifiers: ModuleType, vector: dict[str, Any], tmp_path: Path
) -> None:
    verifier = verifiers.ToolCallConditionVerifier()
    verdict_file = tmp_path / "verdict.json"
    parsed = verifier.parse(spec(verdict_file, vector["condition"]))

    verdict = verifier.grade(parsed, context(captured(vector, verifiers.UNSETTLED)))

    assert (verdict.outcome, verdict.reason, list(verdict.matched_calls)) == (
        vector["outcome"],
        vector["reason"],
        vector["matched_calls"],
    )
    written = json.loads(verdict_file.read_text(encoding="utf-8"))
    assert written["outcome"] == vector["outcome"]


@pytest.mark.parametrize("vector", VALIDATE, ids=[v["id"] for v in VALIDATE])
def test_every_validate_vector_gives_the_stated_problem(
    verifiers: ModuleType, vector: dict[str, Any]
) -> None:
    assert verifiers.validate_condition(vector["condition"]) == vector["error"]


@pytest.mark.parametrize("vector", VALIDATE, ids=[v["id"] for v in VALIDATE])
def test_parse_accepts_exactly_the_valid_conditions(
    verifiers: ModuleType, vector: dict[str, Any]
) -> None:
    verifier = verifiers.ToolCallConditionVerifier()
    if vector["valid"]:
        assert verifier.parse(spec(Path("v.json"), vector["condition"])) is not None
    else:
        with pytest.raises(ValueError, match="condition"):
            verifier.parse(spec(Path("v.json"), vector["condition"]))


@pytest.mark.parametrize(
    ("x", "unsettled", "outcome"),
    [(3, False, "detected"), (4, False, "not_detected"), (4, True, "inconclusive")],
)
def test_each_outcome_is_written_to_the_verdict_file(
    verifiers: ModuleType, tmp_path: Path, x: int, unsettled: bool, outcome: str
) -> None:
    calls = [record("a", {"x": x}, verifiers.UNSETTLED if unsettled else None)]
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
def test_parse_refuses_a_malformed_spec(verifiers: ModuleType, bad: Any) -> None:
    with pytest.raises(ValueError):
        verifiers.ToolCallConditionVerifier().parse(bad)


def test_only_a_detected_verdict_passes_the_check(
    verifiers: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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


def test_the_neutral_check_always_passes(
    verifiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_register_adds_the_verifiers_once(
    verifiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    assert registered == ["asago_tool_call_condition", "asago_rubric_judge", "asago_neutral"]


def test_the_bundle_code_runs_without_the_consumer_or_midojo(
    command_package: Path, tmp_path: Path
) -> None:
    template = tmp_path / "template"
    compile_package(command_package, template)
    vector = next(v for v in EVALUATE if v["outcome"] == "detected")
    code = (
        "import importlib.util, json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "for name in ('asago_artifact_generator', 'asago_bundle_midojo', 'midojo'):\n"
        "    assert importlib.util.find_spec(name) is None, name\n"
        "from asago_suite import asago_verifiers as v\n"
        "vector = json.loads(sys.argv[2])\n"
        "c, calls, complete = vector['condition'], vector['calls'], vector['complete']\n"
        "verdict = v.evaluate_condition(c, calls, complete)\n"
        "print(json.dumps([verdict.outcome, verdict.reason, list(verdict.matched_calls)]))\n"
    )

    done = subprocess.run(
        [sys.executable, "-S", "-E", "-s", "-c", code, str(template), json.dumps(vector)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    expected = [vector["outcome"], vector["reason"], vector["matched_calls"]]
    assert json.loads(done.stdout) == expected
