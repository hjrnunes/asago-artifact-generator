"""The authoring journal: ledger, failure evidence, and review records are projections."""

from __future__ import annotations

import json
from pathlib import Path

from asago_artifact_generator.authoring.core import Finding, PromptPacket
from asago_artifact_generator.authoring.journal import (
    AttemptFailed,
    AuthoringJournal,
    DispatchOpened,
    Finished,
    ResponseReturned,
    ReviewEvidenceRecorded,
)
from asago_artifact_generator.authoring.policy import AuthoringPolicy

from .support import load_failure_evidence, scripted_orchestrator
from .test_stage_local_orchestration import _finding, _review
from .test_versioned_authoring_wire import _framed, _inventory, _plan, _runtime_contract, _view

_PACKET = PromptPacket(
    stage="plan_review",
    version="review-test",
    system="system",
    user="user",
    payload={"candidate_plan": {"a": 1}, "scenario": "s"},
)


def _open_review(journal: AuthoringJournal, pinned: dict[str, str]) -> None:
    journal.append(
        DispatchOpened(
            dispatch_index=1,
            attempt_index=1,
            correction_index=0,
            role="reviewer",
            task_id="journal",
            packet=_PACKET,
            policy={"max_retries": 0},
            raw_response_path="authoring/01-plan_review.raw",
            model_identity={"requested_model": None},
            review={**pinned, "review": {"status": "pending"}},
        )
    )


def _evidence_event(**changes: object) -> ReviewEvidenceRecorded:
    fields: dict = {
        "review_stage": "plan",
        "status": "accepted",
        "effective_controls": {"thinking": "off"},
        "prompt_version": _PACKET.version,
        "prompt_sha256": _PACKET.sha256,
        "input_sha256": "packet-input",
        "candidate_sha256": "packet-candidate",
        "contract_sha256": "contract",
        "configuration_sha256": "configuration",
    }
    return ReviewEvidenceRecorded(**{**fields, **changes})


_PINNED = {
    "reviewed_input_sha256": "input",
    "reviewed_candidate_sha256": "candidate",
    "candidate_bytes_sha256": "bytes",
}


def test_review_evidence_copies_only_the_decision_fields(tmp_path: Path) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    _open_review(journal, _PINNED)
    review = {
        "decision": "revise",
        "original_decision": "revise",
        "decision_after_scope_filter": "accept",
        "summary": "scripted",
        "findings": [{"question": "q"}],
        "out_of_scope_findings": [],
        "question_ids": ["q"],
        "unrelated": "ignored",
    }

    journal.append(_evidence_event(review=review))

    evidence = journal.ledger[-1]["review"]
    assert {key: evidence[key] for key in review if key != "unrelated"} == {
        key: value for key, value in review.items() if key != "unrelated"
    }
    assert "unrelated" not in evidence
    assert evidence["status"] == "accepted"
    assert evidence["effective_controls"] == {"thinking": "off"}
    assert journal.evidence["attempts"][-1]["review"] == evidence
    assert journal.reviews["plan"] == evidence
    assert evidence["findings"] is not review["findings"]


def test_review_evidence_keeps_the_digests_pinned_when_the_dispatch_opened(
    tmp_path: Path,
) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    _open_review(journal, _PINNED)
    journal.append(
        ResponseReturned("dispatch:1", b"raw", None, None, None, None),
        _evidence_event(status="pending", raw_response_key="dispatch:1", raw=b"raw"),
    )

    evidence = journal.ledger[-1]["review"]
    assert evidence["reviewed_input_sha256"] == "input"
    assert evidence["reviewed_candidate_sha256"] == "candidate"
    assert evidence["candidate_bytes_sha256"] == "bytes"
    assert evidence["raw_response_key"] == "dispatch:1"
    assert evidence["raw_response_bytes"] == 3
    assert journal.evidence["attempts"][-1]["review"] == evidence
    assert "decision" not in journal.reviews["plan"]


def test_attempt_failures_and_the_finish_reach_only_their_projections(tmp_path: Path) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    _open_review(journal, _PINNED)
    finding = Finding("review_unavailable", "bad review", "plan_review")

    journal.append(
        AttemptFailed((finding,)),
        Finished("review_unavailable", (finding,), {"stage": "plan", "reason": finding.code}),
    )
    path = journal.flush()

    attempt = journal.evidence["attempts"][-1]
    assert attempt["findings"] == [finding.to_dict()]
    assert attempt["failure"] == {
        "phase": "post_response",
        "code": "review_unavailable",
        "detail": "bad review",
    }
    assert "findings" not in journal.ledger[-1]
    assert journal.ledger[-1]["stage_status"] == "review_unavailable"
    assert load_failure_evidence(path) == json.loads(json.dumps(journal.evidence))


def test_projections_are_a_function_of_the_events(tmp_path: Path) -> None:
    orchestrator, _transport = scripted_orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            _review("revise", [_finding()]),
            json.dumps(_plan()),
            _review(),
            b"not json",
            _framed(),
            _review(),
        ],
        task_id="replayed",
        policy=AuthoringPolicy(),
    )
    result = orchestrator.run(_view(), _inventory(), _runtime_contract())
    assert result.status == "accepted"

    replayed = AuthoringJournal("replayed", tmp_path / "package")
    replayed.append(*orchestrator._journal.events)

    assert replayed.ledger == orchestrator._journal.ledger == result.ledger
    assert replayed.evidence == orchestrator._journal.evidence
    assert replayed.reviews == orchestrator._journal.reviews
    assert load_failure_evidence(tmp_path / "package.failure-evidence.json") == json.loads(
        json.dumps(replayed.evidence)
    )
