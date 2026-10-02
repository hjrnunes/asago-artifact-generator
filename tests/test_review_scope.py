from __future__ import annotations

import json
from pathlib import Path

from asago_artifact_generator.authoring import (
    ARTIFACT_REVIEW_QUESTION_IDS,
    PLAN_REVIEW_QUESTION_IDS,
    AuthoringOrchestrator,
    AuthoringPolicy,
    build_artifact_review_packet,
    build_plan_review_packet,
    parse_review_response,
)

from .support import ScriptedAuthoringTransport
from .test_versioned_authoring_wire import (
    _framed,
    _inventory,
    _metadata,
    _plan,
    _runtime_contract,
    _source,
    _view,
)


def _review(decision: str, findings: list[dict] | None = None) -> bytes:
    return json.dumps(
        {
            "decision": decision,
            "summary": f"scripted {decision}",
            "findings": findings or [],
        }
    ).encode()


def _finding(question: str) -> dict[str, str]:
    return {
        "question": question,
        "location": "candidate_plan.observation_claim.violation",
        "problem": f"problem for {question}",
        "basis": f"basis for {question}",
        "required_change": f"change for {question}",
    }


def _orchestrator(tmp_path: Path, responses: list[object]) -> AuthoringOrchestrator:
    return AuthoringOrchestrator(
        transport=ScriptedAuthoringTransport(responses),
        package_dir=tmp_path / "package",
        task_id="review-scope",
        policy=AuthoringPolicy(),
        wire_version="v2",
    )


def test_review_schema_requires_question_and_keeps_strict_single_object_framing() -> None:
    missing = _review("revise", [_finding("scenario_fidelity")])
    missing_object = json.loads(missing)
    del missing_object["findings"][0]["question"]
    parsed = parse_review_response(json.dumps(missing_object))
    assert "question" not in parsed.findings[0]

    unknown = parse_review_response(_review("revise", [_finding("not_a_question")]))
    assert unknown.findings[0]["question"] == "not_a_question"


def test_all_out_of_scope_revise_becomes_accept_and_is_recorded(tmp_path: Path) -> None:
    orchestrator = _orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            _review("revise", [_finding("not_a_question")]),
            _framed(),
            _review("accept"),
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
    assert review["out_of_scope_findings"] == [_finding("not_a_question")]
    assert result.review_status["plan"] == "accepted"


def test_only_in_scope_findings_reach_correction_prompt(tmp_path: Path) -> None:
    out_of_scope = _finding("detector_implements_plan")
    in_scope = _finding("scenario_fidelity")
    transport = ScriptedAuthoringTransport(
        [
            json.dumps(_plan()),
            _review("revise", [in_scope, out_of_scope]),
            json.dumps(_plan()),
            _review("accept"),
            _framed(),
            _review("accept"),
        ]
    )
    orchestrator = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="review-correction-scope",
        policy=AuthoringPolicy(),
        wire_version="v2",
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


def test_review_prompts_render_closed_questions_guarantees_and_one_object_example() -> None:
    view = _view()
    inventory = _inventory()
    runtime = _runtime_contract()
    plan_packet = build_plan_review_packet(view, _plan(), inventory, runtime)
    artifact_packet = build_artifact_review_packet(
        view,
        _plan(),
        _metadata(),
        _source(),
        [],
        inventory,
        runtime,
    )

    for packet, question_ids in (
        (plan_packet, PLAN_REVIEW_QUESTION_IDS),
        (artifact_packet, ARTIFACT_REVIEW_QUESTION_IDS),
    ):
        assert "REVIEW QUESTIONS" in packet.user
        assert "MECHANICAL GUARANTEES (NOT REVIEW QUESTIONS)" in packet.user
        assert all(question_id in packet.user for question_id in question_ids)
        assert "exactly one JSON object as the whole response" in packet.user
        assert "no text before or after" in packet.user
        assert "example_finding" in packet.user
        assert "value.<key>.record_key" in packet.user
