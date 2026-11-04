"""The stage loop's decisions, exercised without a dispatch or an evidence writer."""

from __future__ import annotations

import inspect
from pathlib import Path

from asago_artifact_generator.authoring import orchestrator, stage_loop
from asago_artifact_generator.authoring.core import Finding
from asago_artifact_generator.authoring.review import (
    PriorReviewRound,
    RejectedReviewResponse,
)
from asago_artifact_generator.authoring.stage_loop import (
    Author,
    Authored,
    AuthorStopped,
    Correct,
    Corrected,
    CorrectionStopped,
    Halt,
    Review,
    ReviewAnswered,
    ReviewSkipped,
    ReviewStopped,
    StageLoopState,
    Started,
    _ReviewOutcome,
    _StageStop,
    step,
)

_DECISION_MODULE = Path(inspect.getfile(stage_loop))


def _finding(code: str = "plan_validation", path: str = "plan") -> Finding:
    return Finding(code, "the checks reported it", path)


def test_starting_the_loop_authors_the_stage() -> None:
    state, action = step(StageLoopState(), Started())

    assert action == Author()
    assert state == StageLoopState()


def test_an_author_preflight_stop_halts_the_stage() -> None:
    stop = _StageStop("prompt_overflow", (_finding("prompt_overflow", "call1"),))

    _, action = step(StageLoopState(), AuthorStopped(stop))

    assert action == Halt(stop)


def test_a_prompt_preflight_finding_stops_the_stage_failed() -> None:
    finding = _finding("prompt_preflight", "call1")

    _, action = step(StageLoopState(), Authored(None, (finding,), b""))

    assert action == Halt(_StageStop("failed", (finding,)))


def test_an_authored_candidate_goes_to_review() -> None:
    candidate = {"interpretation": "candidate"}

    state, action = step(StageLoopState(), Authored(candidate, (), b"raw"))

    assert action == Review(candidate, prior_round=None, rejected=None, rejected_dispatch=None)
    assert state.candidate == candidate
    assert state.pending == ()
    assert state.raw == b"raw"


def test_a_skipped_review_accepts_its_candidate() -> None:
    candidate = {"artifact": "candidate"}

    _, action = step(StageLoopState(), ReviewSkipped(candidate))

    assert action == Halt(candidate, review_status="not_requested")


def test_a_missing_candidate_goes_to_correction_with_its_findings() -> None:
    finding = _finding()

    state, action = step(StageLoopState(), Authored(None, (finding,), b"failed"))

    assert action == Correct((finding,), b"failed", review_driven=False)
    assert state.candidate is None
    assert state.pending == (finding,)
    assert state.raw == b"failed"


def test_a_corrected_candidate_returns_to_review_with_the_prior_round() -> None:
    candidate = {"interpretation": "corrected"}
    prior = PriorReviewRound(({"question": "plan-1"},), {"interpretation": "reviewed"})
    state = StageLoopState(
        pending=(_finding("semantic_review"),),
        raw=b"reviewed",
        review_driven=True,
        prior_round=prior,
    )

    next_state, action = step(state, Corrected(candidate, (), b"corrected"))

    assert action == Review(candidate, prior_round=prior, rejected=None, rejected_dispatch=None)
    assert next_state.candidate == candidate
    assert next_state.raw == b"corrected"
    assert next_state.review_driven is False


def test_a_correction_that_still_finds_problems_asks_again() -> None:
    finding = _finding("artifact_validation", "artifact")

    state, action = step(
        StageLoopState(review_driven=True), Corrected(None, (finding,), b"corrected")
    )

    assert action == Correct((finding,), b"corrected", review_driven=False)
    assert state.review_driven is False


def test_a_correction_stop_halts_the_stage() -> None:
    stop = _StageStop("unresolved", (_finding("correction_limit_exhausted"),))

    _, action = step(StageLoopState(), CorrectionStopped(stop))

    assert action == Halt(stop)


def test_a_review_accept_halts_with_the_candidate() -> None:
    candidate = {"interpretation": "accepted"}

    _, action = step(StageLoopState(), ReviewAnswered(candidate, _ReviewOutcome("accept"), ()))

    assert action == Halt(candidate, review_status="accepted")


def test_a_blocked_review_stops_with_the_stage_status() -> None:
    finding = _finding("semantic_review", "artifact")
    state = StageLoopState(review_blocked_status="needs_plan_revision")
    outcome = _ReviewOutcome("blocked")

    _, action = step(state, ReviewAnswered({"artifact": "candidate"}, outcome, (finding,)))

    assert action == Halt(_StageStop("needs_plan_revision", (finding,)), review_status="blocked")


def test_a_revise_asks_a_correction_and_spends_the_review_revision() -> None:
    candidate = {"interpretation": "reviewed"}
    record = {"question": "plan-1", "problem": "unsupported claim"}
    finding = _finding("semantic_review")
    state = StageLoopState(candidate=candidate, raw=b"reviewed")
    outcome = _ReviewOutcome("revise", findings=(record,))

    next_state, action = step(state, ReviewAnswered(candidate, outcome, (finding,)))

    assert action == Correct((finding,), b"reviewed", review_driven=True, review_status="revise")
    assert next_state.candidate is None
    assert next_state.pending == (finding,)
    assert next_state.prior_round == PriorReviewRound((record,), candidate)
    assert next_state.review_driven is True


def test_a_rejected_review_asks_again_with_the_rejected_response() -> None:
    candidate = {"artifact": "candidate"}
    rejected = RejectedReviewResponse(b"not json", (_finding("review_schema"),))
    outcome = _ReviewOutcome("", rejected=rejected, rejected_dispatch=4)
    prior = PriorReviewRound((), {"artifact": "reviewed"})
    state = StageLoopState(candidate=candidate, prior_round=prior)

    next_state, action = step(state, ReviewAnswered(candidate, outcome, ()))

    assert action == Review(candidate, prior_round=prior, rejected=rejected, rejected_dispatch=4)
    assert next_state.rejected == rejected
    assert next_state.rejected_dispatch == 4


def test_a_review_stop_halts_the_stage() -> None:
    stop = _StageStop("review_unavailable", (_finding("review_unavailable", "plan_review"),))

    _, action = step(StageLoopState(), ReviewStopped(stop))

    assert action == Halt(stop)


def test_the_decision_module_calls_no_evidence_writer() -> None:
    source = _DECISION_MODULE.read_text()

    assert "_record_" not in source
    assert "_reject_" not in source


def test_the_orchestrator_stage_loop_is_driven_by_step() -> None:
    assert orchestrator.step is step
    assert "step(" in inspect.getsource(orchestrator.AuthoringOrchestrator._run_stage)
