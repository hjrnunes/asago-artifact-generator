"""Decoded tool results are parsed values, and control feedback states value shapes."""

from __future__ import annotations

import json

from asago_artifact_generator.authoring import (
    _correction_detector_feedback_view,
    _evidence_packet_contract,
)
from asago_artifact_generator.detector_controls import describe_input_shapes

_RESULT_TEXT = '{"status": "closed", "items": []}'


def _fixture() -> dict:
    return {
        "bindings": {
            "record_text": _RESULT_TEXT,
            "record_envelope": {
                "content": [{"type": "text", "text": _RESULT_TEXT}],
                "structuredContent": {"result": _RESULT_TEXT},
            },
            "record_id": "R-1",
        },
        "tool_calls": [
            {
                "name": "lookup_item",
                "decoded_result": {"status": "closed", "items": []},
                "raw_result": _RESULT_TEXT,
            },
            {"name": "file_ticket", "decoded_result": None, "raw_result": None},
        ],
    }


def test_input_shapes_name_parsed_objects_and_json_text_strings() -> None:
    shapes = describe_input_shapes(_fixture())

    assert shapes["tool_calls[0].decoded_result"] == (
        "object with keys ['items', 'status']; parsed JSON, compare its fields"
    )
    assert shapes["tool_calls[0].raw_result"].startswith("string holding JSON text of an object")
    assert shapes["bindings.record_text"].startswith("string holding JSON text of an object")
    assert shapes["bindings.record_envelope"] == (
        "object with keys ['content', 'structuredContent']"
    )
    assert shapes["bindings.record_envelope.structuredContent.result"].startswith(
        "string holding JSON text of an object"
    )
    assert shapes["bindings.record_envelope.content[0].text"].startswith(
        "string holding JSON text of an object"
    )
    assert shapes["bindings.record_id"] == "string"
    assert shapes["tool_calls[1].decoded_result"] == "null"
    assert "tool_calls[1].raw_result" not in shapes


def test_failed_control_feedback_carries_input_shapes() -> None:
    view = _correction_detector_feedback_view(
        {
            "failed_controls": [
                {
                    "name": "omission-trigger-no-call",
                    "evidence": _fixture(),
                    "expected_outcome": "detected",
                    "expected_claim_level": "command_attempt",
                    "actual_result": {"outcome": "inconclusive"},
                    "actual_outcome": "inconclusive",
                    "outcome_class": "structurally_valid_wrong_outcome",
                }
            ],
            "passing_controls": [],
            "correction_guidance": "exact owner text",
        }
    )

    rendered = view["failed_controls"][0]
    assert rendered["input"] == _fixture()
    assert rendered["input_shapes"] == describe_input_shapes(_fixture())
    assert view["correction_guidance"] == "exact owner text"


def test_evidence_interface_says_decoded_result_is_parsed_and_bindings_keep_type() -> None:
    interface = json.dumps(_evidence_packet_contract())

    assert "not JSON text" in interface
    assert "str(decoded_result)" in interface
    assert "keeps the selected value's type" in interface
