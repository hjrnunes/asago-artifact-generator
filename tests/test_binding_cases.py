"""Authoring runs every binding conformance case of the artifact-package contract.

The cases live in ``contracts/artifact-package/binding-cases/`` and orch runs the
same files from its mirror through its execute functions. Each case gives binding
declarations, the inventory they select from (a supplied fact's ``value`` is the
source value), the runtime contract, and optionally a stimulus. ``expected`` names
the outcome of each check it covers as a category: ``accepted``, or ``rejected``
with a reason. A reason is case vocabulary; ``_REASONS`` maps it to this
repository's finding code and wording, so each repository keeps its own messages.

* ``declaration``: the plan's binding checks accept or reject the declarations.
* ``values``: the supplied values each declaration selects (``values`` lists them);
  ``value_missing`` when one is absent.
* ``slots``: the slot checks of both stages (the plan's request and history, and the
  artifact's final text and history turns).

Where authoring and execute disagree, a case expects the stricter outcome, so that
authoring never accepts a package that execute rejects.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.checks import (
    _collect_binding_findings,
    _plan_stimulus_slot_findings,
    _slot_names_in_order,
    _stimulus_slot_findings,
)
from asago_artifact_generator.authoring.core import Finding
from asago_artifact_generator.bindings import (
    normalize_binding_declarations,
    supplied_binding_values,
)

_ROOT = Path(__file__).resolve().parents[1] / "contracts" / "artifact-package" / "binding-cases"
_CHECKS = ("declaration", "values", "slots")
_REASONS = {
    "selector_undocumented": (
        r"^plan_binding_validation: (undocumented selector for binding |selector must be an "
        r"exact documented dot path)"
    ),
    "selector_through_array": r"^selector_through_array: selector for binding ",
    "selector_root_mismatch": (
        r"^plan_binding_validation: selector for binding \S+ must be rooted at "
        r"(value for supplied_input|result for setup_output), not "
    ),
    "type_mismatch": r"^plan_binding_validation: binding type mismatch for ",
    "setup_not_permitted": r"^plan_binding_validation: setup operation is not permitted: ",
    "value_missing": r"^value_missing: ",
    "slot_undeclared": r"^undeclared_slot: ",
    "slot_non_scalar": r"^non_scalar_slot: ",
    "slot_name_invalid": r"^invalid_slot_name: ",
}
_SLOT_CODES = frozenset({"undeclared_slot", "non_scalar_slot", "invalid_slot_name"})


def _cases() -> list[Path]:
    return sorted(_ROOT.glob("*.json"))


def _case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _outcome(findings: list[Finding]) -> str | None:
    """Return the first finding as ``code: detail``, or None when there is none."""

    return f"{findings[0].code}: {findings[0].detail}" if findings else None


def _declaration(case: dict[str, Any]) -> list[str | None]:
    bindings = copy.deepcopy(case["bindings"])
    normalize_binding_declarations(bindings, inventory=case["inventory"])
    findings = _collect_binding_findings(
        bindings,
        case["inventory"],
        case["runtime_contract"],
        finding_code="plan_binding_validation",
    )
    return [_outcome(findings)]


def _values(case: dict[str, Any]) -> list[str | None]:
    values = supplied_binding_values(copy.deepcopy(case["bindings"]), case["inventory"])
    missing = [item["name"] for item in case["bindings"] if item["name"] not in values]
    if missing:
        return [f"value_missing: {', '.join(missing)}"]
    expected = case["expected"]["values"].get("values", values)
    return [None if values == expected else f"values_differ: {values}"]


def _slots(case: dict[str, Any]) -> list[str | None]:
    stimulus = case["stimulus"]
    history = stimulus.get("history", [])
    plan = {
        "stimulus_approach": {
            "request": stimulus["user_text"],
            "history": [turn["content"] for turn in history],
        },
        "runtime_bindings": copy.deepcopy(case["bindings"]),
    }
    artifact = {
        "user_text": stimulus["user_text"],
        "history": copy.deepcopy(history),
        "slots": _slot_names_in_order(stimulus["user_text"]),
    }
    return [
        _slot_outcome(_plan_stimulus_slot_findings(plan, case["inventory"])),
        _slot_outcome(_stimulus_slot_findings(artifact, copy.deepcopy(case["bindings"]))),
    ]


def _slot_outcome(findings: list[Finding]) -> str | None:
    """Keep the slot findings; the plan check also reports user-text consumer mismatches."""

    return _outcome([finding for finding in findings if finding.code in _SLOT_CODES])


_RUNNERS = {"declaration": _declaration, "values": _values, "slots": _slots}


def test_the_cases_cover_every_check_and_reason_this_runner_maps() -> None:
    expected = [_case(path)["expected"] for path in _cases()]
    checks = {check for item in expected for check in item}
    reasons = {result.get("reason") for item in expected for result in item.values()} - {None}

    assert _cases()
    assert checks == set(_CHECKS)
    assert reasons == set(_REASONS)


@pytest.mark.parametrize("path", _cases(), ids=lambda path: path.stem)
def test_authoring_gives_every_case_its_expected_outcome(path: Path) -> None:
    case = _case(path)
    assert case["expected"] and set(case["expected"]) <= set(_CHECKS), case["expected"]

    for check, expected in case["expected"].items():
        for actual in _RUNNERS[check](case):
            if expected["outcome"] == "accepted":
                assert actual is None, (check, actual)
            else:
                assert expected["outcome"] == "rejected", (check, expected)
                assert actual is not None, (check, "accepted")
                assert re.search(_REASONS[expected["reason"]], actual), (check, actual)
