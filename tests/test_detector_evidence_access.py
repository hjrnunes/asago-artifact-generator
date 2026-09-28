from __future__ import annotations

from asago_artifact_generator.bindings import (
    find_stimulus_user_text_consumer_mismatches,
    supplied_binding_values,
)
from asago_artifact_generator.detector_controls import run_detector_controls
from asago_artifact_generator.detector_runtime import validate_detector_evidence_access


def _owner_binding(*, consumers: list[str]) -> dict:
    return {
        "name": "owner",
        "expected_type": "string",
        "source_kind": "supplied_input",
        "source_ref": "facts:order",
        "selector": "value.customer_id",
        "consumers": consumers,
        "on_missing": "stop",
    }


def _inventory() -> dict:
    return {
        "facts": [
            {
                "ref": "order",
                "value": {"customer_id": "customer-a"},
                "schema": {
                    "type": "object",
                    "properties": {"customer_id": {"type": "string"}},
                },
            }
        ]
    }


def test_undeclared_evidence_root_is_flagged() -> None:
    source = b"""
def evaluate(evidence):
    owner = evidence.get("state", {}).get("owner")
    return {
        "outcome": "inconclusive",
        "reason": str(owner),
        "evidence_refs": ["state"],
        "claim_level": "command_attempt",
    }
"""

    findings = validate_detector_evidence_access(
        source,
        observations={"tool_calls": {"availability": "captured"}},
        bindings=[],
        judge_enabled=False,
    )

    assert [finding["code"] for finding in findings] == [
        "undeclared_evidence_access",
        "undeclared_evidence_access",
    ]
    assert "undeclared evidence root 'state'" in findings[0]["detail"]
    assert "returns evidence_refs root 'state'" in findings[1]["detail"]


def test_declared_detector_binding_access_is_accepted() -> None:
    source = b"""
def evaluate(evidence):
    owner = evidence["bindings"].get("owner")
    return {
        "outcome": "inconclusive",
        "reason": str(owner),
        "evidence_refs": ["bindings.owner"],
        "claim_level": "command_attempt",
    }
"""

    assert (
        validate_detector_evidence_access(
            source,
            observations={"tool_calls": {"availability": "captured"}},
            bindings=[_owner_binding(consumers=["detector.owner"])],
            judge_enabled=False,
        )
        == ()
    )


def test_declared_binding_access_is_accepted_without_detector_consumer() -> None:
    source = b"""
def evaluate(evidence):
    owner = evidence["bindings"].get("owner")
    return {
        "outcome": "inconclusive",
        "reason": str(owner),
        "evidence_refs": ["bindings.owner"],
        "claim_level": "command_attempt",
    }
"""

    assert (
        validate_detector_evidence_access(
            source,
            observations={"tool_calls": {"availability": "captured"}},
            bindings=[_owner_binding(consumers=["stimulus.user_text"])],
            judge_enabled=False,
        )
        == ()
    )


def test_detector_controls_return_static_findings_before_execution() -> None:
    findings, controls = run_detector_controls(
        b"""
def evaluate(evidence):
    return {
        "outcome": "inconclusive",
        "reason": str(evidence.get("state")),
        "evidence_refs": ["state"],
        "claim_level": "command_attempt",
    }
""",
        cases=(),
        plan={"runtime_bindings": []},
    )

    assert controls == []
    assert findings
    assert findings[0]["code"] == "undeclared_evidence_access"


def test_user_text_consumer_fails_when_value_is_absent_and_passes_when_present() -> None:
    binding = _owner_binding(consumers=["stimulus.user_text"])
    values = supplied_binding_values([binding], _inventory())

    absent = find_stimulus_user_text_consumer_mismatches(
        [binding],
        "Please process the order.",
        resolved_values=values,
    )
    present = find_stimulus_user_text_consumer_mismatches(
        [binding],
        "Please process the order for customer-a.",
        resolved_values=values,
    )
    slot = find_stimulus_user_text_consumer_mismatches(
        [binding],
        "Please process {{owner}}'s order.",
        resolved_values=values,
    )

    assert absent[0]["binding_name"] == "owner"
    assert present == ()
    assert slot == ()
