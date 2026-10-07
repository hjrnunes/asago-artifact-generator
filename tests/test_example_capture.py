"""Structured example captures and the authoring-time oracle self-test."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.contracts import (
    _call2_contract_v2,
    neutral_artifact_plan_v2,
    neutral_artifact_response_without_source,
)

from .example_capture_support import artifact_findings, call, capture, neutral_metadata
from .test_versioned_authoring_wire import _assemble, _metadata, _plan


def _reply_plan() -> dict[str, Any]:
    plan = neutral_artifact_plan_v2()
    plan["observation_claim"]["claim_level"] = "reply"
    plan["semantic_judge"] = {"needed": True, "scope": "the reply"}
    return plan


def test_neutral_example_carries_a_capture_on_unsafe_and_safe() -> None:
    examples = neutral_artifact_response_without_source()["examples"]

    assert examples["unsafe"]["capture"] == capture(call(id="neutral-1", value=4))
    assert examples["safe"]["capture"] == capture(call(id="neutral-1", value=3))
    assert "capture" not in examples["inconclusive"]
    assert artifact_findings(neutral_artifact_response_without_source()) == []


def _example_schema(plan: dict[str, Any] | None, label: str) -> dict[str, Any]:
    return _call2_contract_v2(plan)["schema"]["properties"]["examples"]["properties"][label]


def test_command_attempt_contract_requires_a_capture_on_unsafe_and_safe_only() -> None:
    for label in ("unsafe", "safe"):
        schema = _example_schema(neutral_artifact_plan_v2(), label)
        assert schema["required"] == ["label", "description", "capture"]
        shape = schema["properties"]["capture"]
        assert shape["required"] == ["tool_calls", "complete"]
        assert shape["properties"]["tool_calls"]["items"]["required"] == [
            "operation",
            "arguments",
        ]
    inconclusive = _example_schema(neutral_artifact_plan_v2(), "inconclusive")
    assert "capture" not in inconclusive["properties"]
    assert inconclusive["required"] == ["label", "description"]


def test_reply_contract_has_no_capture() -> None:
    contract = _call2_contract_v2(_reply_plan())

    assert "capture" not in json.dumps(contract["schema"])
    assert "example_capture_meaning" not in contract


def test_planless_contract_offers_the_capture_without_requiring_it() -> None:
    schema = _example_schema(None, "unsafe")

    assert "capture" in schema["properties"]
    assert schema["required"] == ["label", "description"]


def test_command_attempt_contract_explains_the_capture() -> None:
    meaning = _call2_contract_v2(neutral_artifact_plan_v2())["example_capture_meaning"]

    assert "in the order" in meaning
    assert "complete" in meaning
    assert "inconclusive" in meaning


@pytest.mark.parametrize("label", ["unsafe", "safe"])
def test_command_attempt_example_without_a_capture_is_a_missing_field(label: str) -> None:
    findings = artifact_findings(neutral_metadata(**{label: None}))

    assert [(f.code, f.path) for f in findings] == [("missing_field", f"examples.{label}.capture")]


@pytest.mark.parametrize("label", ["unsafe", "safe", "inconclusive"])
def test_a_missing_example_is_a_missing_field(label: str) -> None:
    metadata = neutral_metadata()
    del metadata["examples"][label]

    findings = artifact_findings(metadata)

    assert [(f.code, f.path) for f in findings] == [("missing_field", f"examples.{label}")]


def test_an_example_that_is_not_author_proposed_is_an_example_shape_finding() -> None:
    metadata = neutral_metadata()
    metadata["examples"]["safe"]["label"] = "supplied"

    findings = artifact_findings(metadata)

    assert [(f.code, f.path) for f in findings] == [("example_shape", "examples.safe")]


def test_a_description_that_is_not_a_string_is_a_type_error() -> None:
    metadata = neutral_metadata()
    metadata["examples"]["unsafe"]["description"] = 3

    findings = artifact_findings(metadata)

    assert [(f.code, f.path) for f in findings] == [("type_error", "examples.unsafe.description")]


def test_inconclusive_example_cannot_carry_a_capture() -> None:
    metadata = neutral_metadata()
    metadata["examples"]["inconclusive"]["capture"] = capture()

    findings = artifact_findings(metadata)

    assert [(f.code, f.path) for f in findings] == [
        ("unexpected_field", "examples.inconclusive.capture")
    ]


@pytest.mark.parametrize("label", ["unsafe", "safe"])
def test_reply_example_cannot_carry_a_capture(label: str) -> None:
    metadata = neutral_metadata()
    metadata["semantic_judge_spec"] = {"question": "Is it?", "criteria": "c", "fact_refs": []}

    findings = artifact_findings(metadata, _reply_plan())

    assert {(f.code, f.path) for f in findings} == {
        ("unexpected_field", "examples.unsafe.capture"),
        ("unexpected_field", "examples.safe.capture"),
    }


def test_reply_example_without_a_capture_is_unchanged() -> None:
    metadata = neutral_metadata(unsafe=None, safe=None)
    metadata["semantic_judge_spec"] = {"question": "Is it?", "criteria": "c", "fact_refs": []}

    assert artifact_findings(metadata, _reply_plan()) == []


@pytest.mark.parametrize(
    ("capture", "code", "path"),
    [
        ("calls", "type_error", "examples.unsafe.capture"),
        ({"tool_calls": []}, "missing_field", "examples.unsafe.capture.complete"),
        ({"complete": True}, "missing_field", "examples.unsafe.capture.tool_calls"),
        (
            {"tool_calls": [], "complete": True, "note": "x"},
            "unexpected_field",
            "examples.unsafe.capture.note",
        ),
        (
            {"tool_calls": {}, "complete": True},
            "type_error",
            "examples.unsafe.capture.tool_calls",
        ),
        (
            {"tool_calls": [], "complete": "yes"},
            "type_error",
            "examples.unsafe.capture.complete",
        ),
        (
            {"tool_calls": ["inspect_record"], "complete": True},
            "type_error",
            "examples.unsafe.capture.tool_calls[0]",
        ),
        (
            {"tool_calls": [{"arguments": {}}], "complete": True},
            "missing_field",
            "examples.unsafe.capture.tool_calls[0].operation",
        ),
        (
            {"tool_calls": [{"operation": "inspect_record"}], "complete": True},
            "missing_field",
            "examples.unsafe.capture.tool_calls[0].arguments",
        ),
        (
            {"tool_calls": [{"operation": 3, "arguments": {}}], "complete": True},
            "type_error",
            "examples.unsafe.capture.tool_calls[0].operation",
        ),
        (
            {"tool_calls": [{"operation": "inspect_record", "arguments": []}], "complete": True},
            "type_error",
            "examples.unsafe.capture.tool_calls[0].arguments",
        ),
        (
            {
                "tool_calls": [{"operation": "inspect_record", "arguments": {}, "result": 1}],
                "complete": True,
            },
            "unexpected_field",
            "examples.unsafe.capture.tool_calls[0].result",
        ),
    ],
)
def test_malformed_capture_is_one_shape_finding(capture: Any, code: str, path: str) -> None:
    findings = artifact_findings(neutral_metadata(unsafe=capture))

    assert [(f.code, f.path) for f in findings] == [(code, path)]


def test_capture_of_an_operation_outside_the_inventory_is_a_structural_finding() -> None:
    unknown = capture(call("delete_everything", id="x"), call(id="neutral-1", value=4))

    findings = artifact_findings(neutral_metadata(unsafe=unknown))

    [finding] = findings
    assert finding.code == "example_capture_unknown_operation"
    assert finding.path == "examples.unsafe.capture.tool_calls[0].operation"
    assert "delete_everything" in finding.detail
    assert "inspect_record" in finding.detail


def test_agreeing_examples_produce_no_finding() -> None:
    assert artifact_findings(neutral_metadata()) == []


def test_package_examples_member_keeps_the_captures_as_written(tmp_path: Path) -> None:
    document = _metadata()

    result = _assemble(tmp_path, _plan(), document)

    assert result.status == "accepted", result.findings
    stored = json.loads(result.package.members["examples.json"])
    assert stored == document["examples"]
    assert stored["unsafe"]["capture"]["tool_calls"][0]["operation"] == "process_refund"
    assert "capture" not in stored["inconclusive"]
