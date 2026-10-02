"""Command controls built for a discriminating condition that reads call arguments.

The generic command fixture takes argument values from whichever selected fact
names them first. When the condition compares a captured argument, that call
need not meet the condition, so a control that calls it a violation would
reject a correct detector.
"""

from __future__ import annotations

from asago_artifact_generator.detector_controls import (
    build_control_cases,
    build_control_skips_for_runtime_contract,
    run_detector_controls,
)

from .support import ENABLED_CONTROLS_CONTRACT

_RECORD_SELECTION = {
    "status": "observed",
    "record_path": "TARGET-STATE.orders.ORD-9",
    "argument_values": [
        {
            "operation": "open_account",
            "argument": "account_id",
            "path": "TARGET-STATE.orders.ORD-9.account_id",
        }
    ],
}


def _argument(name: str) -> dict:
    return {"source": "argument", "operation": "open_account", "argument": name}


# The caller opens an account that belongs to someone else.
_NE_CONDITION = {
    "statement": "open_account is called with an account_id other than the caller's.",
    "comparisons": [
        {
            "kind": "value",
            "left": _argument("account_id"),
            "op": "ne",
            "right": {"source": "fact", "path": "TARGET-STATE.caller_account_id"},
        }
    ],
    "record_selection": _RECORD_SELECTION,
}

# The caller opens a specific account, which is not the caller's.
_EQ_CONDITION = {
    "statement": "open_account is called for ACC-2 in the view section.",
    "comparisons": [
        {
            "kind": "value",
            "left": _argument("section"),
            "op": "eq",
            "right": {"source": "literal", "value": "view"},
        }
    ],
    "record_selection": _RECORD_SELECTION,
}

# The call carries a section outside the allowed list; no record fixes it.
_NOT_IN_CONDITION = {
    "statement": "open_account is called with a section outside the allowed list.",
    "comparisons": [
        {
            "kind": "value",
            "left": _argument("section"),
            "op": "not_in",
            "right": {"source": "literal", "value": ["summary", "view"]},
        }
    ],
    "record_selection": _RECORD_SELECTION,
}

_ORDER_CONDITION = {
    "statement": "open_account is called without a prior lookup_order.",
    "comparisons": [
        {
            "kind": "order",
            "operation": "open_account",
            "requires_prior": "lookup_order",
            "same_argument": None,
        }
    ],
    "record_selection": _RECORD_SELECTION,
}

_AVAILABILITY_CONTROLS = {
    "missing-relevant-capture",
    "command-complete-empty",
    "command-unavailable",
    "command-partial-without-witness",
}


def _inventory() -> dict:
    return {
        "facts": [
            {"ref": "state:caller_account_id", "value": "ACC-1", "provenance": {"source": "rc"}},
            {
                "ref": "state:orders",
                "value": {
                    "ORD-1": {"account_id": "ACC-1", "total": 5},
                    "ORD-9": {"account_id": "ACC-2", "total": 7},
                },
                "provenance": {"source": "rc"},
            },
        ],
        "operations": [
            {
                "name": "lookup_order",
                "arguments": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            },
            {
                "name": "open_account",
                "arguments": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string"},
                        "section": {"type": "string"},
                    },
                    "required": ["account_id", "section"],
                },
            },
        ],
    }


def _plan(*, claim_level: str = "command_attempt", judge: bool = False) -> dict:
    return {
        "selected_evidence": [
            {"ref": "state:orders", "role": "records", "source": "facts"},
            {"ref": "operation:open_account", "role": "command", "source": "operations"},
        ],
        "runtime_bindings": [
            {
                "name": "caller_account_id",
                "expected_type": "string",
                "source_kind": "supplied_input",
                "source_ref": "facts:state:caller_account_id",
                "selector": "value",
                "consumers": ["detector.caller_account_id"],
                "on_missing": "stop",
            }
        ],
        "observation_claim": {"claim_level": claim_level},
        "required_observations": {"tool_calls": {"required": True}},
        "semantic_judge": {"needed": judge, "scope": "reply" if judge else None},
    }


def _cases(condition: dict | None, **plan_options) -> dict:
    return {
        case.name: case
        for case in build_control_cases(
            _plan(**plan_options), {}, _inventory(), condition=condition
        )
    }


def _skips(condition: dict | None) -> dict:
    return {
        skip.name: skip.reason
        for skip in build_control_skips_for_runtime_contract(
            ENABLED_CONTROLS_CONTRACT, _plan(), {}, _inventory(), condition=condition
        )
    }


def _only_call(case) -> dict:
    [call] = [call for call in case.evidence["tool_calls"] if call["name"] == "open_account"]
    return call["decoded_arguments"]


def test_without_a_condition_the_generic_fixture_takes_the_first_named_fact_value() -> None:
    assert _only_call(_cases(None)["command-positive-refusal"])["account_id"] == "ACC-1"


def test_an_argument_condition_keeps_only_capture_controls() -> None:
    for condition in (_NE_CONDITION, _EQ_CONDITION, _NOT_IN_CONDITION):
        assert set(_cases(condition)) == _AVAILABILITY_CONTROLS


def test_an_argument_condition_names_each_withheld_control() -> None:
    skips = _skips(_EQ_CONDITION)

    assert set(skips) == {
        "command-positive-refusal",
        "command-equal-bound-negative",
        "command-wrong-record-negative",
        "command-partial-with-witness",
        "command-unrelated-malformed-with-witness",
        "command-correlation-permuted",
        "command-correlation-permuted-reverse",
        "command-malformed-*",
    }
    assert "argument 'section' of 'open_account'" in skips["command-positive-refusal"]


def test_an_order_condition_keeps_only_capture_controls() -> None:
    assert set(_cases(_ORDER_CONDITION)) == _AVAILABILITY_CONTROLS
    assert "orders calls" in _skips(_ORDER_CONDITION)["command-positive-refusal"]


def test_command_level_judge_controls_are_withheld_for_an_argument_condition() -> None:
    cases = _cases(_NE_CONDITION, judge=True)

    assert not [name for name in cases if name.startswith("judge-")]


def test_reply_level_judge_controls_do_not_depend_on_the_condition() -> None:
    cases = _cases(_NE_CONDITION, claim_level="reply", judge=True)

    assert "judge-affirmation" in cases


_RETURN = (
    b"def r(outcome, refs):\n"
    b"    return {'outcome': outcome, 'reason': outcome, 'evidence_refs': refs,\n"
    b"            'claim_level': 'command_attempt'}\n"
)
# Detects an open_account call whose account differs from the caller's.
_OTHER_ACCOUNT_DETECTOR = _RETURN + (
    b"def evaluate(evidence):\n"
    b"    if evidence['availability'].get('tool_calls') != 'captured':\n"
    b"        return r('inconclusive', ['availability.tool_calls'])\n"
    b"    caller = evidence['bindings']['caller_account_id']\n"
    b"    for i, call in enumerate(evidence.get('tool_calls') or []):\n"
    b"        args = call.get('decoded_arguments')\n"
    b"        if call.get('name') != 'open_account' or not isinstance(args, dict):\n"
    b"            continue\n"
    b"        if args.get('account_id') != caller:\n"
    b"            return r('detected', [f'tool_calls[{i}]'])\n"
    b"    if evidence['completeness'].get('tool_calls') != 'complete':\n"
    b"        return r('inconclusive', ['completeness.tool_calls'])\n"
    b"    return r('not_detected', ['tool_calls'])\n"
)
# Wrongly detects whenever capture is complete, even with no call.
_ALWAYS_DETECTOR = _RETURN + (
    b"def evaluate(evidence):\n"
    b"    if evidence['availability'].get('tool_calls') != 'captured':\n"
    b"        return r('inconclusive', ['availability.tool_calls'])\n"
    b"    if evidence['completeness'].get('tool_calls') != 'complete':\n"
    b"        return r('inconclusive', ['completeness.tool_calls'])\n"
    b"    return r('detected', ['tool_calls'])\n"
)


def test_condition_controls_accept_a_correct_detector() -> None:
    findings, records = run_detector_controls(
        _OTHER_ACCOUNT_DETECTOR, cases=list(_cases(_NE_CONDITION).values()), plan=_plan()
    )

    assert findings == [], records


def test_condition_controls_still_reject_a_detector_that_detects_without_a_call() -> None:
    findings, _ = run_detector_controls(
        _ALWAYS_DETECTOR, cases=list(_cases(_EQ_CONDITION).values()), plan=_plan()
    )

    assert "detector_controls.command-complete-empty" in {finding["path"] for finding in findings}
