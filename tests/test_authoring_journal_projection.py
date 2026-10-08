"""The journal's projections, pinned byte for byte on event sequences that apply every event.

Each sequence drives ``AuthoringJournal`` directly. The test compares the
canonical bytes of the ledger, the preserved reviews, and the flushed
failure-evidence sidecar with the files under ``fixtures/journal_projection/``.
Those files hold the bytes the journal wrote before its two projections were
folded into one, so any change to a recorded value, shape, or list order fails.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import journal as journal_module
from asago_artifact_generator.authoring.core import Finding, PromptPacket
from asago_artifact_generator.authoring.journal import (
    AllowancesRecorded,
    AttemptFailed,
    AuthoringJournal,
    BudgetRecorded,
    CandidateDigested,
    ChecksSkipped,
    CorrectionRecorded,
    DecodedOutputRecorded,
    DispatchErrored,
    DispatchOpened,
    DispatchRequested,
    FailureControlsRecorded,
    Finished,
    LedgerFindingsRecorded,
    ParseFailed,
    PolicyRecorded,
    ResponseReturned,
    ResponseUnavailable,
    ReviewControlsRecorded,
    ReviewDecided,
    ReviewEvidenceRecorded,
    ReviewFailed,
    ReviewStatusRecorded,
    RunFindingRecorded,
    TransformationRecorded,
    TransportRetried,
    ValidationPassed,
    ValidationTransformed,
)
from asago_artifact_generator.contract_kit import canonical_json

FIXTURES = Path(__file__).parent / "fixtures" / "journal_projection"
_PLAN_FINDING = Finding("missing_field", "plan lacks a field", "call1.plan", stage="plan")
_RUN_FINDING = Finding("budget_exhausted", "no author requests left", "call1")
_URL_CONTROLS = {"model": "m", "base_url": "https://models.invalid/v1", "max_retries": 0}


def _open(
    journal: AuthoringJournal,
    stage: str,
    *,
    role: str = "author",
    attempt: int = 1,
    correction: int = 0,
    review: dict | None = None,
) -> None:
    index = len(journal.dispatches()) + 1
    packet = PromptPacket(
        stage=stage,
        version=f"{stage}-v",
        system=f"{stage} system",
        user=f"{stage} user",
        payload={},
    )
    journal.append(
        DispatchRequested(stage),
        DispatchOpened(
            dispatch_index=index,
            attempt_index=attempt,
            correction_index=correction,
            role=role,
            task_id="projection",
            packet=packet,
            policy={"max_retries": 0, "review_plan": True},
            raw_response_path=f"authoring/{index:02d}-{stage}.raw",
            model_identity={"profile_alias": "p", "requested_model": "m"},
            review=review,
        ),
    )


def _pinned(stage: str) -> dict:
    return {
        "reviewed_input_sha256": f"{stage}-input",
        "reviewed_candidate_sha256": f"{stage}-candidate",
        "candidate_bytes_sha256": f"{stage}-bytes",
        "review": {"status": "pending", "prompt_version": f"{stage}-v"},
    }


def _review_evidence(stage: str, status: str, **changes: object) -> ReviewEvidenceRecorded:
    fields: dict = {
        "review_stage": "plan" if stage == "plan_review" else "artifact",
        "status": status,
        "effective_controls": {"temperature": 0, "max_retries": 0},
        "prompt_version": f"{stage}-v",
        "prompt_sha256": f"{stage}-sha",
        "input_sha256": "event-input",
        "candidate_sha256": "event-candidate",
        "contract_sha256": "contract",
        "configuration_sha256": "configuration",
    }
    return ReviewEvidenceRecorded(**{**fields, **changes})


def _transport_failure(journal: AuthoringJournal) -> None:
    """Run fields, a retried author request that raised, and a run-level finding."""

    journal.append(
        BudgetRecorded({"author": {"limit": 4, "used": 0}}),
        PolicyRecorded({"plan_max_corrections": 1}),
        AllowancesRecorded({"plan": 1, "artifact": 1}, None),
        ReviewStatusRecorded({"plan": "not_requested", "artifact": "not_requested"}),
    )
    _open(journal, "call1")
    journal.append(
        BudgetRecorded({"author": {"limit": 4, "used": 1}}),
        TransportRetried(1, {"type": "APIConnectionError", "status_code": None}),
        TransportRetried(2, {"type": "InternalServerError", "status_code": 500}),
        FailureControlsRecorded(_URL_CONTROLS),
        DispatchErrored("connection refused"),
        ResponseUnavailable("provider_failure", "connection refused", 12.34567),
        AttemptFailed((Finding("transport_failure", "connection refused", "call1"),)),
        RunFindingRecorded(_RUN_FINDING),
        AllowancesRecorded(None, {"plan": 0, "artifact": 0}),
        Finished(
            "transport_failure",
            (Finding("transport_failure", "connection refused", "call1"),),
            {"stage": "plan", "attempt_index": 0, "reason": "transport_failure"},
        ),
    )


def _author_correction(journal: AuthoringJournal) -> None:
    """A framing failure, a parse failure, a correction, a rewrite, and two finishes."""

    _open(journal, "call1")
    journal.append(
        ResponseReturned(
            "dispatch:1",
            b"```json\n{}\n```",
            {},
            _URL_CONTROLS,
            {"finish_reason": {"availability": "available", "value": "stop"}},
            "m-returned",
        ),
        TransformationRecorded("outer_fence_removed", ("outer_fence_removed",)),
        LedgerFindingsRecorded("framing_findings", (_PLAN_FINDING,)),
        ChecksSkipped(("plan_checks", "binding_checks", "plan_checks")),
        AttemptFailed((_PLAN_FINDING,)),
    )
    _open(journal, "correction", correction=1)
    journal.append(
        ResponseReturned("dispatch:2", b"not json", {"total_tokens": 7}, None, None, None),
        CorrectionRecorded("call1", "plan", b"```json\n{}\n```"),
        ParseFailed("Expecting value: line 1 column 1"),
        LedgerFindingsRecorded("findings", (_PLAN_FINDING,)),
        AttemptFailed((_PLAN_FINDING, _RUN_FINDING)),
    )
    _open(journal, "correction", attempt=2, correction=2)
    rewrite = {"transformation": "stimulus_slots_derived", "derived_slots": ["owner"]}
    journal.append(
        ResponseReturned("dispatch:3", b'{"plan": {}}', None, {"max_retries": 0}, None, "m"),
        CorrectionRecorded("call1", "plan", b""),
        DecodedOutputRecorded({"plan": {"from": "correction"}}, on_ledger=False),
        CandidateDigested("correction-digest"),
        ValidationTransformed((rewrite,), ("outer_fence_removed", rewrite)),
        ValidationPassed(),
    )
    _open(journal, "call2")
    journal.append(
        ResponseReturned("dispatch:4", b'{"stimulus": {}}', {"total_tokens": 9}, None, None, "m"),
        DecodedOutputRecorded({"stimulus": {"slots": []}}, on_ledger=True),
        CandidateDigested("call2-digest"),
        ValidationPassed(),
        Finished("accepted", (), {"stage": None, "attempt_index": 3, "reason": "accepted"}),
        Finished(
            "failed",
            (Finding("authoring_crashed", "KeyError: 'x'", "run", stage="artifact"),),
            {"stage": "artifact", "attempt_index": 3, "reason": "authoring_crashed"},
        ),
    )


def _reviews(journal: AuthoringJournal) -> None:
    """An accepted plan review, then an artifact review that errored and failed to parse."""

    _open(journal, "plan_review", role="reviewer", review=_pinned("plan_review"))
    journal.append(
        ResponseReturned(
            "dispatch:1", b'{"decision": "accept"}', {"total_tokens": 3}, None, None, "m"
        ),
        ReviewControlsRecorded({"temperature": 0, "max_retries": 0, "url": "https://x.invalid"}),
        _review_evidence("plan_review", "pending", raw_response_key="dispatch:1", raw=b"raw"),
        TransformationRecorded("outer_fence_removed", ("outer_fence_removed",)),
        ReviewDecided({"decision": "accept", "findings": []}),
        _review_evidence(
            "plan_review",
            "accepted",
            raw_response_key="dispatch:1",
            raw=b"raw",
            review={
                "decision": "accept",
                "original_decision": "accept",
                "decision_after_scope_filter": "accept",
                "summary": "fine",
                "findings": [],
                "out_of_scope_findings": [{"id": "x"}],
                "question_ids": ["q1"],
                "ignored": "not copied",
            },
        ),
    )
    _open(journal, "artifact_review", role="reviewer", review=_pinned("artifact_review"))
    journal.append(
        DispatchErrored("timed out"),
        ReviewControlsRecorded({"temperature": 0, "max_retries": 0}),
        _review_evidence("artifact_review", "unavailable"),
        LedgerFindingsRecorded("review_error", (_PLAN_FINDING,)),
        ReviewFailed({"phase": "post_response", "code": "review_unavailable", "detail": "bad"}),
        RunFindingRecorded(Finding("review_unavailable", "bad", "artifact_review")),
        Finished(
            "review_unavailable",
            (Finding("review_unavailable", "bad", "artifact_review"),),
            {"stage": "artifact", "attempt_index": 1, "reason": "review_unavailable"},
        ),
    )


def _no_attempts(journal: AuthoringJournal) -> None:
    """A run that stops before its first dispatch opens."""

    journal.append(
        BudgetRecorded({"author": {"limit": 0, "used": 0}}),
        DispatchRequested("call1"),
        RunFindingRecorded(_RUN_FINDING),
        Finished(
            "budget_exhausted",
            (_RUN_FINDING,),
            {"stage": None, "attempt_index": None, "reason": "budget_exhausted"},
        ),
    )


SEQUENCES: dict[str, Callable[[AuthoringJournal], None]] = {
    "transport_failure": _transport_failure,
    "author_correction": _author_correction,
    "reviews": _reviews,
    "no_attempts": _no_attempts,
}


def projected_bytes(name: str, tmp_path: Path) -> dict[str, bytes]:
    """Return the ledger, reviews, and sidecar bytes of one sequence, with tmp_path masked."""

    journal = AuthoringJournal("projection", tmp_path / "package")
    SEQUENCES[name](journal)
    sidecar = journal.flush().read_bytes()
    return {
        "ledger.json": (canonical_json(journal.ledger) + "\n").encode("utf-8"),
        "reviews.json": (canonical_json(journal.reviews) + "\n").encode("utf-8"),
        "failure-evidence.json": sidecar.replace(str(tmp_path).encode("utf-8"), b"<TMP>"),
    }


@pytest.mark.parametrize("name", sorted(SEQUENCES))
def test_projection_bytes_match_the_recorded_fixture(name: str, tmp_path: Path) -> None:
    for file_name, data in projected_bytes(name, tmp_path).items():
        assert data == (FIXTURES / name / file_name).read_bytes(), f"{name}/{file_name}"


def test_both_projections_keep_the_decoded_output_that_the_checks_rewrite(tmp_path: Path) -> None:
    journal = AuthoringJournal("projection", tmp_path / "package")
    _open(journal, "call2")
    decoded = {"stimulus": {"slots": ["owner_name"]}}
    journal.append(DecodedOutputRecorded(decoded, on_ledger=True))

    decoded["stimulus"]["slots"] = ["owner"]

    assert journal.ledger[-1]["decoded_output"]["stimulus"]["slots"] == ["owner"]
    assert journal.evidence["attempts"][-1]["decoded_output"]["stimulus"]["slots"] == ["owner"]


def test_every_event_but_the_request_has_one_handler() -> None:
    events = {
        value
        for value in vars(journal_module).values()
        if dataclasses.is_dataclass(value) and value.__module__ == journal_module.__name__
    }

    assert set(journal_module._HANDLERS) == events - {DispatchRequested}


def test_a_validation_rewrite_without_an_open_dispatch_writes_nothing(tmp_path: Path) -> None:
    journal = AuthoringJournal("projection", tmp_path / "package")
    before = canonical_json(journal.evidence)

    with pytest.raises(IndexError):
        journal.append(ValidationTransformed(({"t": 1},), ({"t": 1},)))

    assert canonical_json(journal.evidence) == before
    assert journal.ledger == []
    assert journal.events == []
