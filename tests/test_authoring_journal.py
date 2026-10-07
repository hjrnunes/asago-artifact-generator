"""The authoring journal: ledger, failure evidence, and review records are projections."""

from __future__ import annotations

import json
from pathlib import Path

from asago_artifact_generator.authoring.core import Finding, PromptPacket
from asago_artifact_generator.authoring.journal import (
    AttemptFailed,
    AuthoringJournal,
    CandidateDigested,
    DecodedOutputRecorded,
    DispatchOpened,
    DispatchRequested,
    FailureControlsRecorded,
    Finished,
    ResponseReturned,
    ReviewControlsRecorded,
    ReviewEvidenceRecorded,
    ValidationTransformed,
)
from asago_artifact_generator.authoring.policy import AuthoringPolicy

from .support import (
    load_failure_evidence,
    review_finding,
    review_response,
    scripted_orchestrator,
    world_builders,
)

_framed, _inventory, _plan, _runtime_contract, _view = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract", "view"
)

_PACKET = PromptPacket(
    stage="plan_review",
    version="review-test",
    system="system",
    user="user",
    payload={"candidate_plan": {"a": 1}, "scenario": "s"},
)


def _open_review(journal: AuthoringJournal, pinned: dict[str, str]) -> None:
    journal.append(
        DispatchRequested(_PACKET.stage),
        DispatchOpened(
            dispatch_index=len(journal.dispatches()) + 1,
            attempt_index=1,
            correction_index=0,
            role="reviewer",
            task_id="journal",
            packet=_PACKET,
            policy={"max_retries": 0},
            raw_response_path="authoring/01-plan_review.raw",
            model_identity={"requested_model": None},
            review={**pinned, "review": {"status": "pending"}},
        ),
    )


def _open_author(journal: AuthoringJournal, stage: str = "call1") -> None:
    packet = PromptPacket(stage=stage, version="v", system="s", user="u", payload={})
    journal.append(
        DispatchRequested(stage),
        DispatchOpened(
            dispatch_index=len(journal.dispatches()) + 1,
            attempt_index=1,
            correction_index=0,
            role="author",
            task_id="journal",
            packet=packet,
            policy={"max_retries": 0},
            raw_response_path=f"authoring/01-{stage}.raw",
            model_identity={"requested_model": None},
        ),
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


def test_dispatch_controls_match_the_latest_ledger_record(tmp_path: Path) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    assert journal.dispatch_controls() == {"max_retries": 0}

    def response(controls: dict | None) -> ResponseReturned:
        return ResponseReturned("dispatch:1", b"x", None, controls, None, None)

    _open_author(journal)
    seen = [journal.dispatch_controls()]
    for event in (
        FailureControlsRecorded({"temperature": 0, "api_key": "secret"}),
        response({"seed": 3}),
        response(None),
    ):
        journal.append(event)
        seen.append(journal.dispatch_controls())
        assert seen[-1] == journal.ledger[-1]["controls"]
    _open_review(journal, _PINNED)
    seen.append(journal.dispatch_controls())
    journal.append(ReviewControlsRecorded({"model": "m", "max_retries": 0}))
    seen.append(journal.dispatch_controls())

    assert seen[0] == seen[3] == seen[4] == {"max_retries": 0}
    assert seen[1]["temperature"] == 0
    assert "secret" not in json.dumps(seen[1])
    assert seen[2] == {"seed": 3}
    assert seen[5] == journal.ledger[-1]["controls"] == {"model": "m", "max_retries": 0}


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


def _projected(journal: AuthoringJournal) -> tuple[str, str, str]:
    return (
        json.dumps(journal.ledger, sort_keys=True),
        json.dumps(journal.evidence, sort_keys=True, default=str),
        json.dumps(journal.reviews, sort_keys=True),
    )


def test_review_evidence_without_any_dispatch_is_ignored(tmp_path: Path) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    before = _projected(journal)

    journal.append(_evidence_event())

    assert _projected(journal) == before
    assert journal.events == []


def test_review_evidence_after_only_an_author_dispatch_is_ignored(tmp_path: Path) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    _open_author(journal)
    before = _projected(journal)

    journal.append(_evidence_event())

    assert _projected(journal) == before
    assert "review" not in journal.ledger[-1]
    assert "review" not in journal.evidence["attempts"][-1]
    assert journal.reviews == {}


def test_review_evidence_for_a_review_dispatch_that_never_opened_is_ignored(
    tmp_path: Path,
) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    _open_author(journal)
    journal.append(DispatchRequested("plan_review"))
    before = _projected(journal)

    journal.append(_evidence_event())

    assert _projected(journal) == before
    assert "review" not in journal.ledger[-1]


def test_review_evidence_after_a_later_request_that_never_opened_is_ignored(
    tmp_path: Path,
) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    _open_review(journal, _PINNED)
    journal.append(DispatchRequested("correction"))
    before = _projected(journal)

    journal.append(_evidence_event(status="unavailable"))

    assert _projected(journal) == before
    assert journal.ledger[-1]["review"] == {"status": "pending"}


def test_open_dispatch_follows_the_latest_request(tmp_path: Path) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    assert journal.open_dispatch() is None
    _open_author(journal)
    assert journal.open_dispatch() is journal.dispatches()[-1]
    journal.append(DispatchRequested("plan_review"))
    assert journal.open_dispatch() is None
    _open_review(journal, _PINNED)
    assert journal.open_dispatch() is journal.dispatches()[-1]


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


def test_validation_rewrites_reach_the_ledger_record_and_the_evidence_attempt(
    tmp_path: Path,
) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    _open_author(journal)
    earlier = {"transformation": "outer_fence_removed"}
    rewrite = {"transformation": "binding_consumer_added", "binding": "owner"}

    journal.append(ValidationTransformed((rewrite,), (earlier, rewrite)))
    path = journal.flush()

    assert journal.ledger[-1]["transformations"] == [rewrite]
    attempt = journal.evidence["attempts"][-1]
    assert attempt["transformations"] == [rewrite]
    assert journal.evidence["transformations"] == [earlier, rewrite]
    saved = load_failure_evidence(path)
    assert saved["attempts"][-1]["transformations"] == [rewrite]
    assert saved["transformations"] == [earlier, rewrite]


def test_dispatch_controls_ignore_events_that_set_none(tmp_path: Path) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    _open_author(journal)
    journal.append(ResponseReturned("dispatch:1", b"x", None, {"seed": 3}, None, None))

    journal.append(CandidateDigested("digest"), DecodedOutputRecorded({}, on_ledger=True))

    assert journal.dispatch_controls() == {"seed": 3} == journal.ledger[-1]["controls"]


def test_written_names_the_sidecar_once_a_flush_wrote_it(tmp_path: Path) -> None:
    journal = AuthoringJournal("journal", tmp_path / "package")
    _open_author(journal)
    assert journal.written is None

    path = journal.flush()

    assert journal.written == path
    assert path.is_file()
    assert path == tmp_path / "package.failure-evidence.json"


def test_projections_are_a_function_of_the_events(tmp_path: Path) -> None:
    orchestrator, _transport = scripted_orchestrator(
        tmp_path,
        [
            json.dumps(_plan()),
            review_response("revise", [review_finding()]),
            json.dumps(_plan()),
            review_response(),
            b"not json",
            _framed(),
            review_response(),
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
