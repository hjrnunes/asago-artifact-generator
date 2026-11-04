"""The consumer runs the producer's schema and conformance cases.

The producer's v5 kit carries ``invalid/schema-*.json``, one broken field of
a bound handoff each, with the producer's codes in ``expected-violations.json``.
This reader reports the same codes in the same order on the raised error's
``codes``. The shared validator owns every rule, the schema version included,
so no case stops before the code comparison.

``conformance-cases.json`` names the cases for the five rules the shared
validator owns: safe-call arguments, the procedure chain, reference order, the
ownership walker, and the feature companion. Each rule carries at least one
valid and one invalid case; the producer runs the same manifest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from asago_artifact_generator.input_adapter import (
    HandoffSchemaInvalid,
    InputKind,
    InputSourceError,
    load_input,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff"
_KIT = _CONTRACT / "handoff-v5"
_EXPECTED = json.loads((_KIT / "expected-violations.json").read_text("utf-8"))
_MANIFEST = json.loads((_KIT / "conformance-cases.json").read_text("utf-8"))
RULE_NAMES = frozenset(
    {
        "safe-call-arguments",
        "procedure-chain",
        "reference-order",
        "ownership-walker",
        "feature-companion",
    }
)


@dataclass(frozen=True)
class CaseSummary:
    """How many of a runner's cases passed, and how many it checked."""

    label: str
    passed: int
    total: int

    def __str__(self) -> str:
        return f"{self.label}: {self.passed}/{self.total}"


def _cases(verdict: str, kind: str) -> list[str]:
    return [
        relative
        for rule in _MANIFEST["rules"]
        if rule["kind"] == kind
        for relative in rule[verdict]
    ]


def _companion(tmp_path: Path, relative: str) -> tuple[Path, str]:
    case = json.loads((_KIT / relative).read_text(encoding="utf-8"))
    handoff = tmp_path / "handoff.json"
    handoff.write_bytes((_KIT / case["handoff"]).read_bytes())
    (tmp_path / "handoff.feature").write_text(case["text"], encoding="utf-8")
    return handoff, case["text"]


def _schema_cases() -> list[str]:
    return [path.stem for path in sorted((_KIT / "invalid").glob("schema-*.json"))]


def _expected_violation_summary() -> CaseSummary:
    """Check every entry of the kit's expectation file through the public reader."""

    passed = 0
    for relative, codes in _EXPECTED.items():
        with pytest.raises(HandoffSchemaInvalid) as raised:
            load_input(_KIT / relative)
        if raised.value.codes == tuple(codes):
            passed += 1
    return CaseSummary("expected-violations", passed, len(_EXPECTED))


def _conformance_case_holds(tmp_path: Path, kind: str, valid: bool, relative: str) -> bool:
    if kind == "companion":
        handoff, text = _companion(tmp_path, relative)
        try:
            view = load_input(handoff)
        except InputSourceError as raised:
            return not valid and str(raised).startswith("Gherkin companion does not match")
        return valid and view.gherkin_text == text
    if valid:
        return load_input(_KIT / relative).kind is InputKind.SCENARIO_HANDOFF_V5
    with pytest.raises(HandoffSchemaInvalid) as raised:
        load_input(_KIT / relative)
    return raised.value.codes == tuple(_EXPECTED[relative])


def _conformance_summary(tmp_path: Path) -> CaseSummary:
    """Check every case the kit's conformance manifest names through the public reader."""

    entries = [
        (rule["kind"], verdict == "valid", relative)
        for rule in _MANIFEST["rules"]
        for verdict in ("valid", "invalid")
        for relative in rule[verdict]
    ]
    passed = sum(
        1
        for kind, valid, relative in entries
        if _conformance_case_holds(tmp_path, kind, valid, relative)
    )
    return CaseSummary("conformance", passed, len(entries))


@pytest.mark.parametrize("name", _schema_cases())
def test_the_reader_rejects_each_schema_case_with_the_kits_codes(name: str) -> None:
    codes = _EXPECTED[f"invalid/{name}.json"]

    with pytest.raises(InputSourceError) as raised:
        load_input(_KIT / "invalid" / f"{name}.json")

    assert codes
    assert raised.value.codes == tuple(codes)
    assert str(raised.value).startswith(f"handoff schema invalid: {', '.join(codes)}")


def test_the_case_runner_checks_every_expected_violation_and_conformance_case(
    tmp_path: Path,
) -> None:
    expected = _expected_violation_summary()
    conformance = _conformance_summary(tmp_path)

    assert (str(expected), str(conformance)) == (
        "expected-violations: 153/153",
        "conformance: 33/33",
    )


def test_every_invalid_fixture_carries_its_expected_codes() -> None:
    fixtures = {path.relative_to(_KIT).as_posix() for path in (_KIT / "invalid").glob("*.json")}

    assert len(_EXPECTED) == 153
    assert set(_EXPECTED) == fixtures


def test_the_kit_carries_a_case_for_each_of_the_five_rules() -> None:
    rules = _MANIFEST["rules"]

    assert _MANIFEST["contract"] == "scenario-handoff"
    assert _MANIFEST["kit"] == "handoff-v5"
    assert {rule["name"] for rule in rules} == RULE_NAMES
    assert len(rules) == len(RULE_NAMES)
    for rule in rules:
        assert rule["valid"], rule["name"]
        assert rule["invalid"], rule["name"]
        assert rule["kind"] in {"handoff", "companion"}


def test_every_named_case_is_a_file_in_the_kit() -> None:
    for rule in _MANIFEST["rules"]:
        for relative in [*rule["valid"], *rule["invalid"]]:
            assert (_KIT / relative).is_file(), relative


@pytest.mark.parametrize("relative", _cases("valid", "handoff"))
def test_the_reader_accepts_each_valid_rule_case(relative: str) -> None:
    view = load_input(_KIT / relative)

    assert view.kind is InputKind.SCENARIO_HANDOFF_V5


@pytest.mark.parametrize("relative", _cases("invalid", "handoff"))
def test_the_reader_refuses_each_invalid_rule_case_with_the_kits_codes(
    relative: str,
) -> None:
    codes = _EXPECTED[relative]

    with pytest.raises(InputSourceError) as raised:
        load_input(_KIT / relative)

    assert codes
    assert raised.value.codes == tuple(codes)


@pytest.mark.parametrize("relative", _cases("valid", "companion"))
def test_the_reader_accepts_a_matching_feature_companion(tmp_path: Path, relative: str) -> None:
    handoff, text = _companion(tmp_path, relative)

    view = load_input(handoff)

    assert view.gherkin_text == text


@pytest.mark.parametrize("relative", _cases("invalid", "companion"))
def test_the_reader_refuses_a_mismatched_feature_companion(tmp_path: Path, relative: str) -> None:
    handoff, _ = _companion(tmp_path, relative)

    with pytest.raises(InputSourceError, match="Gherkin companion does not match"):
        load_input(handoff)
