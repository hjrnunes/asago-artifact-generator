"""Offline validation of neutral, prompt-side conversation records."""

from __future__ import annotations

import copy
import json

import pytest

from asago_artifact_generator.garak.compile import compile_execution_artifact
from asago_artifact_generator.garak.conversation import (
    CONVERSATION_SCHEMA_VERSION,
    validate_conversation_case,
    validate_conversation_trace,
)
from asago_artifact_generator.models._base import compute_framed_digest
from tests.test_stpa_garak_platform import _ready_plan


def _case(messages, tools=None):
    body = {
        "schema_version": CONVERSATION_SCHEMA_VERSION,
        "messages": messages,
        "structured_oracle": {"kind": "output_text"},
        "tools": [_declaration()] if tools is None else tools,
    }
    return {**body, "semantic_digest": compute_framed_digest(CONVERSATION_SCHEMA_VERSION, body)}


def _declaration():
    return {
        "type": "function",
        "function": {
            "name": "lookup",
            "description": "Look up a neutral example record.",
            "parameters": {
                "type": "object",
                "properties": {"reference": {"type": "string"}},
                "required": ["reference"],
                "additionalProperties": False,
            },
        },
    }


def _call(call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"reference":"example"}'},
    }


def _history():
    return [
        {"role": "user", "content": "Look up the example record."},
        {"role": "assistant", "content": None, "tool_calls": [_call()]},
        {"role": "tool", "tool_call_id": "call-1", "name": "lookup", "content": "Found."},
    ]


@pytest.mark.parametrize(
    "message",
    [
        None,
        1,
        "text",
        {},
        {"role": "unknown", "content": "x"},
        {"role": [], "content": "x"},
        {"role": "user"},
        {"role": "user", "content": None},
        {"role": "user", "content": {}},
        {"role": "user", "content": "x", "tool_calls": [_call()]},
    ],
)
def test_malformed_message_returns_errors_instead_of_passing_or_crashing(message):
    assert validate_conversation_case(_case([message]))


@pytest.mark.parametrize(
    "calls", [None, [], {}, [None], [{}], [{"id": "x", "type": "function", "function": None}]]
)
def test_malformed_tool_calls_return_errors(calls):
    assert validate_conversation_case(
        _case(
            [
                {"role": "assistant", "content": None, "tool_calls": calls},
                {"role": "user", "content": "Continue."},
            ]
        )
    )


@pytest.mark.parametrize("arguments", [None, {}, "not json", "[]", '{"x":NaN}'])
def test_tool_arguments_must_be_a_json_object_string(arguments):
    history = _history()
    history[1]["tool_calls"][0]["function"]["arguments"] = arguments
    assert validate_conversation_case(_case(history))


@pytest.mark.parametrize(
    "change",
    ["missing", "orphan", "duplicate_result", "duplicate_call", "wrong_name", "interleaved"],
)
def test_history_requires_exact_complete_tool_result_pairing(change):
    history = _history()
    if change == "missing":
        history.pop()
    elif change == "orphan":
        history[2]["tool_call_id"] = "other"
    elif change == "duplicate_result":
        history.append(copy.deepcopy(history[2]))
    elif change == "duplicate_call":
        history[1]["tool_calls"].append(_call())
    elif change == "wrong_name":
        history[2]["name"] = "other"
    else:
        history.insert(2, {"role": "user", "content": "Continue."})
    assert validate_conversation_case(_case(history))


def test_valid_parallel_tool_results_can_arrive_in_reverse_order():
    history = _history()
    history[1]["tool_calls"].append(_call("call-2"))
    history.insert(2, {"role": "tool", "tool_call_id": "call-2", "content": "Also found."})
    assert validate_conversation_case(_case(history)) == []


def test_valid_text_history_preserves_historical_assistant_reply():
    assert (
        validate_conversation_case(
            _case(
                [
                    {"role": "system", "content": "Use the supplied record."},
                    {"role": "user", "content": "Hello."},
                    {"role": "assistant", "content": "Hello."},
                    {"role": "user", "content": "Please continue."},
                ]
            )
        )
        == []
    )


@pytest.mark.parametrize(
    "arguments", ["{}", '{"reference": 3}', '{"reference":"example","extra":1}']
)
def test_history_arguments_must_match_declared_schema(arguments):
    history = _history()
    history[1]["tool_calls"][0]["function"]["arguments"] = arguments
    errors = validate_conversation_case(_case(history))
    assert any("arguments" in error for error in errors)


def test_history_cannot_use_an_undeclared_tool():
    errors = validate_conversation_case(_case(_history(), tools=[]))
    assert any("undeclared" in error for error in errors)


@pytest.mark.parametrize("tools", [[None], [{}], [_declaration(), _declaration()]])
def test_malformed_or_duplicate_declarations_return_errors(tools):
    assert validate_conversation_case(_case(_history(), tools=tools))


def test_unsupported_declared_schema_is_not_treated_as_valid():
    declaration = _declaration()
    declaration["function"]["parameters"]["$ref"] = "#/unknown"
    assert validate_conversation_case(_case(_history(), tools=[declaration]))


def _rehash(document, key):
    document.pop(key, None)
    document[key] = compute_framed_digest(document["schema_version"], document)


@pytest.fixture
def compiled_neutral_case():
    plan = _ready_plan()
    compiled = compile_execution_artifact(
        plan, prebound_texts={"slot-1": "Review the example record."}
    )
    return plan, json.loads(json.dumps(compiled.artifact)), json.loads(json.dumps(compiled.trace))


@pytest.mark.parametrize(
    "section,field",
    [
        ("source", "scenario_id"),
        ("source", "run_id"),
        ("source", "candidate_id"),
        ("source", "ica_id"),
        ("source", "ica_slot_id"),
        ("source", "bundle_digest"),
        ("source", "scenario_content_sha256"),
        ("source", "projection_semantic_digest"),
        ("source", "claim_scope"),
        ("binding", "binding_set_id"),
        ("binding", "binding_set_digest"),
    ],
)
def test_consistent_rehashed_artifact_and_trace_cannot_replace_plan_identity(
    compiled_neutral_case, section, field
):
    plan, case, trace = compiled_neutral_case
    case[section][field] = "different"
    trace[section][field] = "different"
    _rehash(case, "semantic_digest")
    _rehash(trace, "trace_digest")
    assert any(section in error for error in validate_conversation_case(case, plan, trace))
    assert any(section in error for error in validate_conversation_case(case, plan))
    assert any(section in error for error in validate_conversation_trace(trace, plan))


def test_top_level_case_id_matches_ready_plan(compiled_neutral_case):
    plan, case, trace = compiled_neutral_case
    case["case_id"] = "different"
    _rehash(case, "semantic_digest")
    assert any("case_id" in error for error in validate_conversation_case(case, plan, trace))


def test_intact_artifact_plan_and_trace_pass_together(compiled_neutral_case):
    plan, case, trace = compiled_neutral_case
    assert validate_conversation_case(case, plan, trace) == []
    assert validate_conversation_trace(trace, plan) == []
