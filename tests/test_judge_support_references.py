"""Which evidence references may support a decisive semantic-judge verdict."""

from __future__ import annotations

from typing import Any

import pytest

from asago_artifact_generator.detector_runtime import (
    _is_tool_result_reference,
    _usable_judge_support,
)


@pytest.mark.parametrize(
    "reference",
    [
        "tool_calls[0].result",
        "$.tool_calls[3].decoded_result",
        "/tool_calls/0/raw_result",
        "tool_calls[1].raw.output",
        "/tool_calls/2/source_item/result",
        "messages[0].raw.tool_calls[1].result",
        "/messages/4/source_item/tool_calls/0/output",
        "messages[0].raw.notes.tool_calls[2].decoded_result",
        "messages[1].source_item.raw_response.output[0].result",
    ],
)
def test_tool_result_paths_name_a_result_value(reference: str) -> None:
    assert _is_tool_result_reference(reference) is True


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "tool_calls",
        "tool_calls[0]",
        "tool_calls[0].arguments",
        "tool_calls.first.result",
        "tool_calls[0].raw.arguments",
        "tool_calls[0].metadata.result",
        "messages[0].result",
        "messages[0].raw.tool_calls.first.result",
        "messages[0].other.tool_calls[1].result",
        "messages[0].raw.notes.calls[2].result",
        "messages[0].raw.raw_response.input[0].result",
        "messages[0].raw.tool_calls[1].result.extra",
        "snapshots[0].result",
        "tool_calls[0].result extra",
    ],
)
def test_other_paths_do_not_name_a_tool_result(reference: str) -> None:
    assert _is_tool_result_reference(reference) is False


def _evidence(**changes: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "messages": [{"role": "assistant", "content": "I refunded it."}],
        "tool_calls": [{"name": "refund", "result": {"ok": True}}],
        "availability": {"messages": "captured", "tool_calls": "captured"},
        "completeness": {"messages": "complete", "tool_calls": "complete"},
    }
    evidence.update(changes)
    return evidence


def test_message_content_supports_only_its_exact_captured_text() -> None:
    evidence = _evidence()

    assert _usable_judge_support(evidence, "messages[0].content", "I refunded it.")
    assert _usable_judge_support(evidence, "/messages/0/content", "I refunded it.")
    assert not _usable_judge_support(evidence, "messages[0].content", "something else")
    assert not _usable_judge_support(evidence, "messages[5].content", "I refunded it.")
    assert not _usable_judge_support(
        _evidence(messages="not a list"), "messages[0].content", "I refunded it."
    )
    assert not _usable_judge_support(
        _evidence(messages=["not a record"]), "messages[0].content", "not a record"
    )


def test_whole_message_records_support_when_each_has_text_content() -> None:
    evidence = _evidence()
    record = {"role": "assistant", "content": "text"}

    assert _usable_judge_support(evidence, "messages", [record])
    assert _usable_judge_support(evidence, "messages[0]", record)
    assert not _usable_judge_support(evidence, "messages", [])
    assert not _usable_judge_support(evidence, "messages", [record, {"content": None}])
    assert not _usable_judge_support(evidence, "messages[0]", {"content": 3})
    assert not _usable_judge_support(evidence, "messages[0]", "text")


def test_tool_result_values_support_but_call_records_do_not() -> None:
    evidence = _evidence()

    assert _usable_judge_support(evidence, "tool_calls[0].result", {"ok": True})
    assert not _usable_judge_support(evidence, "tool_calls[0].result", None)
    assert not _usable_judge_support(evidence, "tool_calls[0]", {"name": "refund"})
    assert not _usable_judge_support(evidence, "tool_calls[0].name", "refund")
    assert not _usable_judge_support(evidence, "snapshots", {"content": "x"})


def test_uncaptured_or_incomplete_scopes_withhold_support() -> None:
    not_captured = _evidence(availability={"messages": "not_captured"})
    partial = _evidence(completeness={"messages": "partial", "tool_calls": "unknown"})
    record = {"content": "text"}

    assert not _usable_judge_support(not_captured, "messages[0].content", "I refunded it.")
    # A specific item stays usable in a partial capture; the whole scope does not.
    assert _usable_judge_support(partial, "messages[0].content", "I refunded it.")
    assert _usable_judge_support(partial, "/messages/0/content", "I refunded it.")
    assert not _usable_judge_support(partial, "messages", [record])
    assert not _usable_judge_support(partial, "/messages", [record])
    assert _usable_judge_support(
        _evidence(availability=None, completeness=None), "messages", [record]
    )
