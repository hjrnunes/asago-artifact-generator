"""A reply of several ```json blocks separated by prose decodes to its last block."""

from __future__ import annotations

import json

import pytest

from asago_artifact_generator.authoring.core import (
    Call1FramingError,
    Call2FramingError,
    FramingError,
    ReviewResponseError,
)
from asago_artifact_generator.authoring.response_decode import _decode_stage_response
from asago_artifact_generator.authoring.review import parse_review_response

from .support import ScriptedAuthoringTransport, stage_local_orchestrator, world_builders

_view, _inventory, _runtime_contract, _plan, _framed = world_builders(
    "refund", "view", "inventory", "runtime_contract", "plan", "framed"
)

_STAGE_ERRORS = {
    "call1": Call1FramingError,
    "call2": Call2FramingError,
    "review": ReviewResponseError,
}


def _block(value: object) -> bytes:
    return b"```json\n" + json.dumps(value).encode("utf-8") + b"\n```"


def _reply(*parts: bytes) -> bytes:
    return b"\n".join(parts)


@pytest.mark.parametrize("stage", ["call1", "call2", "review"])
def test_two_blocks_around_prose_decode_to_the_last_block(stage: str) -> None:
    raw = _reply(_block({"draft": 1}), b"Wait, the field is wrong.", _block({"final": 2}))

    decoded, transformation = _decode_stage_response(stage, raw)

    assert decoded == {"final": 2}
    assert transformation == "last_fence_taken:2"


def test_several_blocks_with_crlf_and_trailing_whitespace_decode_to_the_last() -> None:
    raw = (
        b'```json\r\n{"a": 1}\r\n```\r\nWait, again.\r\n'
        b'```json\r\n{"a": 2}\r\n```\r\nWait once more.\r\n'
        b'```json\r\n{"a": 3}\r\n```\r\n  \n'
    )

    decoded, transformation = _decode_stage_response("call1", raw)

    assert decoded == {"a": 3}
    assert transformation == "last_fence_taken:3"


def test_a_repeated_object_decodes_once_per_block_rule() -> None:
    raw = _reply(_block({"a": 1}), b"Wait, I will state it again.", _block({"a": 1}))

    assert _decode_stage_response("call1", raw) == ({"a": 1}, "last_fence_taken:2")


def test_one_block_keeps_the_outer_fence_transformation() -> None:
    assert _decode_stage_response("call1", _block({"a": 1})) == ({"a": 1}, "outer_fence_removed")


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        pytest.param(
            _reply(_block({"a": 1}), b"Wait.", _block({"a": 2}), b"Done."),
            "trailing_content",
            id="text-after-the-last-fence",
        ),
        pytest.param(
            _reply(_block({"a": 1}), b"Wait.", b'```json\n{"a": 2\n```'),
            "invalid_json",
            id="last-block-does-not-decode",
        ),
        pytest.param(
            _reply(b"```json\n{broken}\n```", b"Wait.", _block({"a": 2})),
            "invalid_json",
            id="first-block-does-not-decode",
        ),
        pytest.param(
            _reply(_block({"a": 1}), b"Wait.", b"```json\n[1]\n```", b"Wait.", _block({"a": 3})),
            "json_object_required",
            id="middle-block-is-not-an-object",
        ),
        pytest.param(
            _reply(_block({"a": 1}), b"Wait.", b"```json\n{}\n{}\n```"),
            "multiple_json_objects",
            id="two-objects-in-one-block",
        ),
        pytest.param(
            _reply(_block({"a": 1}), b"Wait.", b'```json\n{"a": 2}'),
            "truncated_fence",
            id="later-fence-not-closed",
        ),
        pytest.param(
            _reply(_block({"a": 1}), b"Wait.", b'```JSON\n{"a": 2}\n```'),
            "trailing_content",
            id="later-fence-is-uppercase",
        ),
        pytest.param(
            _reply(_block({"a": 1}), b"Wait.", b"```\n{}\n```"),
            "trailing_content",
            id="later-fence-is-bare",
        ),
        pytest.param(
            _reply(_block({"a": 1}), _block({"a": 2})),
            "multiple_json_blocks",
            id="adjacent-blocks-without-text",
        ),
        pytest.param(
            _reply(_block({"a": 1}), b"Wait.", _block({"a": 2}), _block({"a": 3})),
            "multiple_json_blocks",
            id="adjacent-blocks-after-text",
        ),
        pytest.param(
            _reply(_block({"a": 1}), b'Wait, it is plain: {"a": 2}'),
            "trailing_content",
            id="prose-and-no-second-block",
        ),
    ],
)
@pytest.mark.parametrize("stage", ["call1", "call2", "review"])
def test_other_multi_block_replies_stay_rejected(stage: str, raw: bytes, code: str) -> None:
    with pytest.raises(FramingError) as caught:
        _decode_stage_response(stage, raw)

    assert isinstance(caught.value, _STAGE_ERRORS[stage])
    assert [finding.code for finding in caught.value.findings] == [code]
    assert caught.value.findings[0].path == stage


def test_a_review_reply_takes_the_last_verdict() -> None:
    revise = {
        "decision": "revise",
        "summary": "The plan misses the reply claim.",
        "findings": [
            {
                "question": "scenario_fidelity",
                "location": "candidate_plan.observation_claim",
                "problem": "No reply claim.",
                "basis": "The failure criterion names the reply.",
                "required_change": "Add a reply claim.",
            }
        ],
    }
    accept = {
        "decision": "accept",
        "summary": "On re-reading, the plan is faithful.",
        "findings": [],
    }

    review = parse_review_response(
        _reply(_block(revise), b"Wait, I misread the claim.", _block(accept))
    )

    assert review.decision == "accept"
    assert review.transformation == "last_fence_taken:2"


def test_a_review_reply_whose_last_block_breaks_the_schema_is_rejected() -> None:
    accept = {"decision": "accept", "summary": "Faithful.", "findings": []}
    contradictory = {"decision": "accept", "summary": "Faithful.", "findings": [{"question": "x"}]}

    with pytest.raises(ReviewResponseError) as caught:
        parse_review_response(_reply(_block(accept), b"Wait.", _block(contradictory)))

    assert caught.value.findings


def test_call1_reply_with_a_draft_then_the_plan_is_accepted_and_keeps_its_bytes(tmp_path) -> None:
    raw_plan = _reply(
        _block({"draft": True}), b"Wait, that is not the plan shape.", _block(_plan())
    )

    result = stage_local_orchestrator(
        transport=ScriptedAuthoringTransport([raw_plan, _framed()]),
        package_dir=tmp_path / "package",
        task_id="last-fence-call1",
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert result.raw_responses["call1"] == raw_plan
    assert result.transformations == ["last_fence_taken:2"]
    assert result.ledger[0]["transformation"] == "last_fence_taken:2"


def test_a_correction_reply_with_several_blocks_continues_the_stage(tmp_path) -> None:
    invalid_plan = json.dumps({"unexpected": True}).encode("utf-8")
    corrected = _reply(
        _block({"unexpected": True}), b"Wait, I returned the old plan.", _block(_plan())
    )

    result = stage_local_orchestrator(
        transport=ScriptedAuthoringTransport([invalid_plan, corrected, _framed()]),
        package_dir=tmp_path / "package",
        task_id="last-fence-correction",
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert result.raw_responses["correction"] == corrected
    assert result.transformations == ["last_fence_taken:2"]
    assert result.ledger[1]["transformation"] == "last_fence_taken:2"


def test_a_correction_reply_with_text_after_the_last_fence_still_stops_the_stage(tmp_path) -> None:
    invalid_plan = json.dumps({"unexpected": True}).encode("utf-8")
    corrected = _reply(_block(_plan()), b"Wait.", _block(_plan()), b"Hope that helps.")

    result = stage_local_orchestrator(
        transport=ScriptedAuthoringTransport([invalid_plan, corrected]),
        package_dir=tmp_path / "package",
        task_id="last-fence-trailing",
    ).run(_view(), _inventory(), _runtime_contract())

    assert result.status == "unresolved"
    assert any(finding.code == "trailing_content" for finding in result.findings)
