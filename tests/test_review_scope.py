from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring.core import ReviewResponseError
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import AuthoringPolicy
from asago_artifact_generator.authoring.review import (
    ARTIFACT_REVIEW_QUESTION_IDS,
    PLAN_REVIEW_QUESTION_IDS,
    build_artifact_review_packet,
    build_plan_review_packet,
    parse_review_response,
)

from .support import ScriptedAuthoringTransport, review_finding, review_response, world_builders

_framed, _inventory, _metadata, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "metadata", "plan", "runtime_contract", "view"
)


def _orchestrator(tmp_path: Path, responses: list[object]) -> AuthoringOrchestrator:
    return AuthoringOrchestrator(
        transport=ScriptedAuthoringTransport(responses),
        package_dir=tmp_path / "package",
        task_id="review-scope",
        policy=AuthoringPolicy(),
    )


def test_review_schema_requires_question_and_keeps_strict_single_object_framing() -> None:
    missing = review_response("revise", [review_finding("scenario_fidelity")])
    missing_object = json.loads(missing)
    del missing_object["findings"][0]["question"]
    parsed = parse_review_response(json.dumps(missing_object).encode())
    assert "question" not in parsed.findings[0]

    unknown = parse_review_response(review_response("revise", [review_finding("not_a_question")]))
    assert unknown.findings[0]["question"] == "not_a_question"


def test_all_out_of_scope_revise_becomes_accept_and_is_recorded(tmp_path: Path) -> None:
    orchestrator = _orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            review_response("revise", [review_finding("not_a_question")]),
            _framed(),
            review_response("accept"),
        ],
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    assert [record["stage"] for record in result.ledger] == [
        "call1",
        "plan_review",
        "call2",
        "artifact_review",
    ]
    review = result.ledger[1]["review"]
    assert review["decision"] == "revise"
    assert review["original_decision"] == "revise"
    assert review["decision_after_scope_filter"] == "accept"
    assert review["findings"] == []
    assert review["out_of_scope_findings"] == [review_finding("not_a_question")]
    assert result.review_status["plan"] == "accepted"


def test_only_in_scope_findings_reach_correction_prompt(tmp_path: Path) -> None:
    out_of_scope = review_finding("judge_spec_implements_plan")
    in_scope = review_finding("scenario_fidelity")
    transport = ScriptedAuthoringTransport(
        [
            json.dumps(_plan()),
            review_response("revise", [in_scope, out_of_scope]),
            json.dumps(_plan()),
            review_response("accept"),
            _framed(),
            review_response("accept"),
        ]
    )
    orchestrator = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="review-correction-scope",
        policy=AuthoringPolicy(),
    )

    result = orchestrator.run(_view(), _inventory(), _runtime_contract())

    assert result.status == "accepted"
    correction = next(
        request for request in transport.requests if request["stage"] == "correction"
    )
    assert in_scope["problem"] in correction["user"]
    assert out_of_scope["problem"] not in correction["user"]
    review = result.ledger[1]["review"]
    assert review["findings"] == [in_scope]
    assert review["out_of_scope_findings"] == [out_of_scope]


def test_review_prompts_render_every_closed_question() -> None:
    view = _view()
    inventory = _inventory()
    runtime = _runtime_contract()
    plan_packet = build_plan_review_packet(view, _plan(), inventory, runtime)
    artifact_packet = build_artifact_review_packet(
        view,
        _plan(),
        _metadata(),
        inventory,
        runtime,
    )

    for packet, question_ids in (
        (plan_packet, PLAN_REVIEW_QUESTION_IDS),
        (artifact_packet, ARTIFACT_REVIEW_QUESTION_IDS),
    ):
        assert all(question_id in packet.user for question_id in question_ids)


def _review_problems(raw: str | bytes) -> list[dict]:
    with pytest.raises(ReviewResponseError) as error:
        parse_review_response(raw.encode() if isinstance(raw, str) else raw)
    return [finding.to_dict() for finding in error.value.findings]


@pytest.mark.parametrize("raw", [42, '{"decision": "accept", "summary": "ok"}'])
def test_review_response_must_be_bytes(raw: object) -> None:
    with pytest.raises(TypeError, match="review response must be bytes"):
        parse_review_response(raw)  # type: ignore[arg-type]


def test_review_response_must_be_utf8() -> None:
    (problem,) = _review_problems(b'{"decision": "\xff"}')

    assert problem["code"] == "invalid_json"
    assert problem["detail"].startswith("review response is not valid UTF-8: ")
    assert problem["path"] == "review"


def test_review_envelope_problems_are_reported_together_in_order() -> None:
    raw = json.dumps({"decision": "maybe", "summary": " ", "findings": {}, "extra": 1, "more": 2})

    assert _review_problems(raw) == [
        {
            "code": "review_schema",
            "detail": "review response has unknown fields: extra, more",
            "path": "review",
        },
        {
            "code": "review_schema",
            "detail": "review decision must be one of accept, revise, blocked",
            "path": "review",
        },
        {
            "code": "review_schema",
            "detail": "review summary must be a nonblank string",
            "path": "review",
        },
        {"code": "review_schema", "detail": "review findings must be a list", "path": "review"},
    ]


def test_review_decision_must_agree_with_its_findings() -> None:
    accept = json.loads(review_response("accept", [review_finding("scenario_fidelity")]))
    blocked = json.loads(review_response("blocked"))
    revise_without_list = {"decision": "revise", "summary": "s"}

    assert _review_problems(json.dumps(accept)) == [
        {
            "code": "review_contradiction",
            "detail": "accept requires an empty findings array",
            "path": "review",
        }
    ]
    assert _review_problems(json.dumps(blocked)) == [
        {
            "code": "review_contradiction",
            "detail": "blocked requires at least one complete finding",
            "path": "review",
        }
    ]
    assert _review_problems(json.dumps(revise_without_list)) == [
        {"code": "review_schema", "detail": "review findings must be a list", "path": "review"},
        {
            "code": "review_contradiction",
            "detail": "revise requires at least one complete finding",
            "path": "review",
        },
    ]


def test_accepted_review_keeps_decision_summary_and_no_findings() -> None:
    parsed = parse_review_response(review_response("accept"))

    assert (parsed.decision, parsed.summary, parsed.findings) == ("accept", "scripted accept", ())
