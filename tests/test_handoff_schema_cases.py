"""The consumer rejects every producer schema case, in its own wording.

The producer's v3 and v4 kits carry ``invalid/schema-*.json``, one broken
field of a bound handoff each, with the producer's codes in
``expected-violations.json``. This reader words most rejections differently,
so ``_WORDING`` maps each case's codes to the message this reader gives. The
same message applies to the v3 and v4 copy of a case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.input_adapter import InputSourceError, load_input

_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff"
_KITS = ("handoff-v3", "handoff-v4")
_OBSERVATION = "handoff observation "
_DEDUPLICATION = "handoff deduplication "
_SCHEMA = "handoff schema invalid: "

# case -> the start of this reader's message for the case's producer codes
_WORDING = {
    "schema-unknown-version": (
        "authoring source must be a producer scenario-handoff-v3 or scenario-handoff-v4 document"
    ),
    "schema-missing-narrative": "handoff schema invalid (missing=['narrative'], unknown=[])",
    "schema-unknown-field": "handoff schema invalid (missing=[], unknown=['bogus'])",
    "schema-kind-not-enumerated": "handoff kind is invalid",
    "schema-blank-narrative": "handoff field is blank or mistyped: narrative",
    "schema-safe-alternative-not-text": "handoff field is blank or mistyped: safe_alternative",
    "schema-scenario-version-not-integer": (
        "handoff schema invalid at scenario_version: 'one' is not of type 'integer'"
    ),
    "schema-unknown-not-text": (
        "handoff schema invalid at assumptions_and_unknowns.1: 1 is not of type 'string'"
    ),
    "schema-attack-tree-not-object": "handoff attack_tree and lineage must be objects",
    "schema-lineage-not-object": "handoff attack_tree and lineage must be objects",
    # The ownership scan reports the forbidden key before the lineage schema check.
    "schema-lineage-detector-field": "handoff ownership violation: artifact_design_field:detector",
    "schema-gherkin-not-object": "handoff Gherkin is invalid",
    "schema-gherkin-scenario-null": "handoff Gherkin is invalid",
    "schema-gherkin-unknown-field": "handoff Gherkin has unknown field: extra",
    "schema-gherkin-given-not-list": "handoff Gherkin field is not a list: given",
    "schema-observation-not-object": _OBSERVATION + "metadata must be an object",
    "schema-observation-missing-criteria": (
        _OBSERVATION + "metadata is invalid (missing=['criteria'], unknown=[])"
    ),
    "schema-observation-unknown-field": (
        _OBSERVATION + "metadata is invalid (missing=[], unknown=['extra'])"
    ),
    "schema-observation-criteria-empty": _OBSERVATION + "criteria must be a non-empty list",
    "schema-observation-criterion-not-object": _OBSERVATION + "criterion must be an object",
    "schema-criterion-missing-reason": _OBSERVATION + "criterion fields are invalid",
    "schema-criterion-unknown-field": _OBSERVATION + "criterion fields are invalid",
    "schema-criterion-blank-outcome": _OBSERVATION + "criterion text is invalid",
    "schema-criterion-observable-not-boolean": _OBSERVATION + "criterion observable is invalid",
    "schema-criterion-observable-without-claim": "observable criterion requires claim_level",
    "schema-criterion-analytical-with-operation": (
        "analytical-only criterion must omit operation_name"
    ),
    "schema-assessment-not-object": _OBSERVATION + "assessment must be an object",
    "schema-assessment-missing-reason": _OBSERVATION + "assessment fields are invalid",
    "schema-assessment-disposition-not-enumerated": _OBSERVATION + "disposition is invalid",
    "schema-assessment-blank-reason": _OBSERVATION + "assessment reason is invalid",
    "schema-assessment-supported-not-list": (
        _OBSERVATION + "assessment supported_criteria is invalid"
    ),
    "schema-deduplication-not-object": _DEDUPLICATION + "must be an object",
    "schema-deduplication-missing-key": _DEDUPLICATION + "fields are invalid",
    "schema-deduplication-unknown-field": _DEDUPLICATION + "fields are invalid",
    "schema-deduplication-blank-scenario-id": _DEDUPLICATION + "scenario_id is invalid",
    "schema-deduplication-status-not-enumerated": _DEDUPLICATION + "status is invalid",
    "schema-deduplication-key-not-object": _DEDUPLICATION + "key is invalid",
    "schema-deduplication-key-missing-claim": _DEDUPLICATION + "key is invalid",
    "schema-deduplication-key-blank-operation": _DEDUPLICATION + "operation_name is invalid",
    "schema-deduplication-key-claim-not-enumerated": _DEDUPLICATION + "claim_level is invalid",
    "schema-safe-outcome-not-object": "handoff safe_observable_outcome must be an object",
    "schema-safe-outcome-unknown-field": "handoff safe_observable_outcome fields are invalid",
    "schema-safe-outcome-empty": "handoff safe outcome observable is invalid",
    "schema-safe-outcome-blank-statement": "handoff safe outcome statement is invalid",
    "schema-safe-outcome-analytical-with-refs": (
        "analytical-only handoff safe outcome must omit record and fact references"
    ),
    "schema-condition-not-object": _SCHEMA + "schema_violation:discriminating_condition",
    "schema-missing-tool-call-status": (
        "handoff schema invalid (missing=['tool_call_condition_status'], unknown=[])"
    ),
    "schema-tool-call-status-not-enumerated": (
        _SCHEMA + "schema_violation:tool_call_condition_status"
    ),
    "schema-tool-call-reason-not-bound": (
        "handoff tool_call_condition_status reason must be bound exactly when the status is bound"
    ),
    "schema-tool-call-condition-empty": _SCHEMA + "schema_violation:tool_call_condition",
    "schema-bound-without-tool-call-condition": _SCHEMA + "schema_violation:<root>",
    "schema-not-executable-with-tool-call-condition": _SCHEMA + "schema_violation:<root>",
    # Decision 182: the closed schema rejects the top-level field before the
    # ownership scan sees the turn array inside it.
    "schema-stimulus-turn-field-top-level": (
        "handoff schema invalid (missing=[], unknown=['stimulus_turns'])"
    ),
}


def _cases() -> list[tuple[str, str]]:
    return [
        (kit, path.stem)
        for kit in _KITS
        for path in sorted((_CONTRACT / kit / "invalid").glob("schema-*.json"))
    ]


@pytest.mark.parametrize("kit", _KITS)
def test_every_producer_schema_case_has_this_readers_wording(kit: str) -> None:
    present = {path.stem for path in (_CONTRACT / kit / "invalid").glob("schema-*.json")}

    assert present == set(_WORDING)


@pytest.mark.parametrize(("kit", "name"), _cases(), ids=lambda value: value)
def test_the_reader_rejects_each_schema_case_in_its_mapped_wording(kit: str, name: str) -> None:
    relative = f"invalid/{name}.json"
    codes = json.loads((_CONTRACT / kit / "expected-violations.json").read_text("utf-8"))[relative]

    with pytest.raises(InputSourceError) as raised:
        load_input(_CONTRACT / kit / relative)

    assert codes
    assert str(raised.value).startswith(_WORDING[name]), (codes, str(raised.value))
