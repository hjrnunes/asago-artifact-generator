"""Preparation runs every binding conformance case of the artifact-package contract.

``fixtures/binding-cases/`` is a byte-identical copy of the consumer's
``contracts/artifact-package/binding-cases/``, which authoring runs too. Each case
gives binding declarations, the inventory they select from (a supplied fact's
``value`` is the source value), the runtime contract, and optionally a stimulus.
``expected`` names the outcome of each check it covers as a category:
``accepted``, or ``rejected`` with a reason. ``_REASONS`` maps a reason to the
code and wording of ``BindingError``.

* ``declaration``: ``validate_binding_declarations``.
* ``values``: ``resolve_bindings`` over the supplied facts (``values`` lists them).
* ``slots``: the static stimulus check, and rendering with the resolved values
  when they resolve.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_core.bindings import (
    BindingError,
    RuntimeBinding,
    parse_binding,
    resolve_bindings,
    validate_binding_declarations,
    validate_stimulus_declarations,
)
from asago_bundle_core.preparation import _render_stimulus, supplied_fact_values

_ROOT = Path(__file__).resolve().parent / "fixtures" / "binding-cases"
_CONTRACT_ROOT = (
    Path(__file__).resolve().parents[3] / "contracts" / "artifact-package" / "binding-cases"
)
_CHECKS = ("declaration", "values", "slots")
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_REASONS = {
    "consumers_empty": r"^binding_invalid: binding consumers must be non-empty strings",
    "consumer_not_closed": r"^binding_invalid: binding consumer is not a closed path",
    "selector_undocumented": r"^binding_missing: undocumented selector for binding ",
    "selector_through_array": r"^binding_invalid: selector for binding .* steps into "
    r"the array at ",
    "selector_root_mismatch": r"^binding_invalid: selector for binding \S+ must be "
    r"rooted at (value for supplied_input|result for setup_output), not ",
    "type_mismatch": r"^binding_invalid: binding type mismatch for ",
    "setup_not_permitted": r"^binding_invalid: setup operation is not permitted: ",
    "value_missing": r"^binding_missing: binding_missing:",
    "slot_undeclared": rf"^binding_invalid: undeclared slot: {_IDENTIFIER}$",
    "slot_non_scalar": r"^binding_invalid: non-scalar slot value: ",
    # Preparation reports a slot whose name is not an identifier as undeclared.
    "slot_name_invalid": rf"^binding_invalid: undeclared slot: (?!{_IDENTIFIER}$)",
}


def _cases() -> list[Path]:
    return sorted(_ROOT.glob("*.json"))


def _case(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _outcome(call: Callable[[], Any]) -> tuple[str | None, Any]:
    """Return ``(None, result)``, or ``("code: message", None)`` if ``call`` raises."""

    try:
        return None, call()
    except BindingError as exc:
        return f"{exc.code}: {exc}", None


def _declarations(case: dict[str, Any]) -> tuple[RuntimeBinding, ...]:
    return tuple(parse_binding(item) for item in case["bindings"])


def _resolve(case: dict[str, Any]) -> tuple[str | None, Any]:
    supplied = supplied_fact_values({"inventory": case["inventory"]})
    return _outcome(
        lambda: resolve_bindings(_declarations(case), setup_outputs={}, supplied_inputs=supplied)[
            0
        ]
    )


def _declaration(case: dict[str, Any]) -> list[str | None]:
    error, _ = _outcome(
        lambda: validate_binding_declarations(
            case["bindings"],
            inventory=case["inventory"],
            runtime_contract=case["runtime_contract"],
        )
    )
    return [error]


def _values(case: dict[str, Any]) -> list[str | None]:
    error, values = _resolve(case)
    if error is not None:
        return [error]
    expected = case["expected"]["values"].get("values", values)
    return [None if values == expected else f"values_differ: {values}"]


def _slots(case: dict[str, Any]) -> list[str | None]:
    declarations = _declarations(case)
    static, _ = _outcome(lambda: validate_stimulus_declarations(case["stimulus"], declarations))
    error, values = _resolve(case)
    if error is not None:
        return [static]
    rendered, _ = _outcome(lambda: _render_stimulus(case["stimulus"], values, declarations))
    return [static, rendered]


_RUNNERS = {"declaration": _declaration, "values": _values, "slots": _slots}


def test_the_fixtures_are_the_contract_cases() -> None:
    contract = sorted(_CONTRACT_ROOT.glob("*.json"))

    assert [path.name for path in _cases()] == [path.name for path in contract]
    assert all(path.read_bytes() == (_CONTRACT_ROOT / path.name).read_bytes() for path in _cases())


def test_the_cases_cover_every_check_and_reason_this_runner_maps() -> None:
    expected = [_case(path)["expected"] for path in _cases()]
    checks = {check for item in expected for check in item}
    reasons = {result.get("reason") for item in expected for result in item.values()}

    assert _cases()
    assert checks == set(_CHECKS)
    assert reasons - {None} == set(_REASONS)


@pytest.mark.parametrize("path", _cases(), ids=lambda path: path.stem)
def test_preparation_gives_every_case_its_expected_outcome(path: Path) -> None:
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
