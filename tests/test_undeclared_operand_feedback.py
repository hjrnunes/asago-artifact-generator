"""Undeclared-binding feedback names the supplied fact operands' exact binding forms."""

from __future__ import annotations

from asago_artifact_generator.detector_controls import (
    run_detector_controls,
    supplied_fact_operand_bindings,
)

# Neutral domain: a caller edits a ticket owned by someone else.
_CONDITION = {
    "comparisons": [
        {
            "kind": "value",
            "left": {"source": "fact", "path": "TARGET-STATE.tickets.TK-9.owner_id"},
            "op": "ne",
            "right": {"source": "fact", "path": "TARGET-STATE.caller_id"},
        }
    ],
}
_INVENTORY = {
    "facts": [
        {"ref": "state:caller_id", "value": "U-1"},
        {"ref": "state:tickets", "value": {"TK-9": {"owner_id": "U-2"}}},
        {"ref": "state:tickets:records", "value": {"TK-9": {"record_key": "TK-9"}}},
    ],
    "operations": [],
}
_PLAN = {
    "runtime_bindings": [
        {
            "name": "caller_id",
            "source_kind": "supplied_input",
            "source_ref": "facts:state:caller_id",
            "selector": "value",
            "consumers": ["detector.caller_id"],
        },
        {
            "name": "target_ticket_id",
            "source_kind": "supplied_input",
            "source_ref": "facts:state:tickets:records",
            "selector": "value.TK-9.record_key",
            "consumers": ["detector.target_ticket_id"],
        },
    ],
    "observation_claim": {"claim_level": "command_attempt"},
    "required_observations": {"tool_calls": {"required": True}},
    "semantic_judge": {"needed": False, "scope": None},
}
_READS_TICKET_RECORD = (
    b"def evaluate(evidence):\n"
    b"    record = evidence['bindings']['ticket_record']\n"
    b"    return {'outcome': 'inconclusive', 'reason': str(record),\n"
    b"            'evidence_refs': ['bindings'], 'claim_level': 'command_attempt'}\n"
)


def test_operand_binding_forms_are_derived_from_supplied_facts() -> None:
    assert supplied_fact_operand_bindings(_CONDITION, _INVENTORY, _PLAN) == [
        {
            "path": "TARGET-STATE.tickets.TK-9.owner_id",
            "source_kind": "supplied_input",
            "source_ref": "facts:state:tickets",
            "selector": "value.TK-9.owner_id",
            "declared_binding": None,
        },
        {
            "path": "TARGET-STATE.caller_id",
            "source_kind": "supplied_input",
            "source_ref": "facts:state:caller_id",
            "selector": "value",
            "declared_binding": "caller_id",
        },
    ]


def test_unresolvable_and_non_fact_operands_are_omitted() -> None:
    condition = {
        "comparisons": [
            {
                "kind": "value",
                "left": {"source": "fact", "path": "TARGET-STATE.tickets.TK-404.owner_id"},
                "op": "eq",
                "right": {"source": "literal", "value": "U-2"},
            }
        ]
    }

    assert supplied_fact_operand_bindings(condition, _INVENTORY, _PLAN) == []
    assert supplied_fact_operand_bindings(None, _INVENTORY, _PLAN) == []


def test_undeclared_binding_feedback_names_plan_ownership_and_operand_forms() -> None:
    [finding], _ = run_detector_controls(
        _READS_TICKET_RECORD,
        cases=[],
        plan=_PLAN,
        inventory=_INVENTORY,
        condition=_CONDITION,
    )

    detail = finding["detail"]
    assert finding["code"] == "undeclared_evidence_access"
    assert "'ticket_record'" in detail
    assert "cannot add" in detail
    assert "declare a runtime binding named" not in detail
    assert (
        "TARGET-STATE.tickets.TK-9.owner_id: source_kind supplied_input, source_ref "
        "facts:state:tickets, selector value.TK-9.owner_id, declared binding: none"
    ) in detail
    assert "declared binding: caller_id" in detail
    assert "target_ticket_id (source_ref facts:state:tickets:records, selector " in detail


def test_feedback_without_a_condition_is_unchanged() -> None:
    [plain], _ = run_detector_controls(
        _READS_TICKET_RECORD, cases=[], plan=_PLAN, inventory=_INVENTORY
    )

    assert "declare a runtime binding named 'ticket_record'" in plain["detail"]
    assert "cannot add" not in plain["detail"]
