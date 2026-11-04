"""The stage loop's decisions, kept apart from the evidence they act on.

``step`` is the whole state machine of one stage: it receives the loop state
and the event the orchestrator just handled, and returns the next state and
the action to perform. It never dispatches a request, writes the journal, or
calls an evidence writer. The orchestrator performs each action and reports
what happened as the next event. The plan and artifact stages share this loop;
only their packets, checks and review rules differ.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .core import Finding
from .review import PriorReviewRound, RejectedReviewResponse


@dataclass(frozen=True)
class _StageStop:
    """One terminal stop of the stage-local orchestration state machine."""

    status: str
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True)
class _ReviewOutcome:
    """One completed semantic-review dispatch and its closed classification."""

    decision: str
    findings: tuple[dict[str, Any], ...] = ()
    stop: _StageStop | None = None
    raw: bytes = b""
    # A response that failed its checks while the stage's review-retry allowance
    # had room, and the dispatch that returned it; the review asks again.
    rejected: RejectedReviewResponse | None = None
    rejected_dispatch: int | None = None


@dataclass(frozen=True)
class StageLoopState:
    """Where one stage loop stands between actions.

    ``candidate`` is None while findings remain to correct. ``raw`` is the
    response the next correction replaces, and ``prior_round`` is what the last
    review said before the current revision. ``rejected`` names the review
    response the next review asks again after.
    """

    candidate: dict[str, Any] | None = None
    pending: tuple[Finding, ...] = ()
    raw: bytes = b""
    review_driven: bool = False
    prior_round: PriorReviewRound | None = None
    rejected: RejectedReviewResponse | None = None
    rejected_dispatch: int | None = None
    review_blocked_status: str = "blocked"


@dataclass(frozen=True)
class Started:
    """The stage begins; its author packet has not been dispatched."""


@dataclass(frozen=True)
class AuthorStopped:
    """The author packet failed its pre-dispatch guard."""

    stop: _StageStop


@dataclass(frozen=True)
class Authored:
    """The author dispatch returned, decoded, normalized and checked."""

    candidate: dict[str, Any] | None
    findings: tuple[Finding, ...]
    raw: bytes


@dataclass(frozen=True)
class CorrectionStopped:
    """The correction path decided to stop the stage."""

    stop: _StageStop


@dataclass(frozen=True)
class Corrected:
    """The correction dispatch returned, decoded, normalized and checked."""

    candidate: dict[str, Any] | None
    findings: tuple[Finding, ...]
    raw: bytes


@dataclass(frozen=True)
class ReviewStopped:
    """The review path stopped before it could decide."""

    stop: _StageStop


@dataclass(frozen=True)
class ReviewSkipped:
    """The stage has no semantic review; its checked candidate is accepted."""

    candidate: dict[str, Any]


@dataclass(frozen=True)
class ReviewAnswered:
    """One review dispatch produced its closed outcome.

    ``findings`` are the in-scope reviewer findings as typed stage findings.
    """

    candidate: dict[str, Any]
    outcome: _ReviewOutcome
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class Author:
    """Dispatch the stage's author packet."""


@dataclass(frozen=True)
class Correct:
    """Spend one allowance on a correction of ``findings``."""

    findings: tuple[Finding, ...]
    raw: bytes
    review_driven: bool
    review_status: str | None = None


@dataclass(frozen=True)
class Review:
    """Build and dispatch the stage's review packet for ``candidate``."""

    candidate: dict[str, Any]
    prior_round: PriorReviewRound | None
    rejected: RejectedReviewResponse | None
    rejected_dispatch: int | None


@dataclass(frozen=True)
class Halt:
    """End the stage with the accepted candidate or the stage stop."""

    result: dict[str, Any] | _StageStop
    review_status: str | None = None


StageEvent = (
    Started
    | AuthorStopped
    | Authored
    | CorrectionStopped
    | Corrected
    | ReviewStopped
    | ReviewSkipped
    | ReviewAnswered
)
StageAction = Author | Correct | Review | Halt


def step(state: StageLoopState, event: StageEvent) -> tuple[StageLoopState, StageAction]:
    """Return the next loop state and the action the orchestrator must perform."""

    if isinstance(event, Started):
        return state, Author()
    if isinstance(event, (AuthorStopped, CorrectionStopped, ReviewStopped)):
        return state, Halt(event.stop)
    if isinstance(event, Authored):
        if any(finding.code == "prompt_preflight" for finding in event.findings):
            return state, Halt(_StageStop("failed", event.findings))
        return _response_step(state, event.candidate, event.findings, event.raw)
    if isinstance(event, Corrected):
        return _response_step(
            replace(state, review_driven=False), event.candidate, event.findings, event.raw
        )
    if isinstance(event, ReviewSkipped):
        return state, Halt(event.candidate, review_status="not_requested")
    assert isinstance(event, ReviewAnswered), f"unsupported stage event: {type(event).__name__}"
    return _review_step(state, event)


def _response_step(
    state: StageLoopState,
    candidate: dict[str, Any] | None,
    findings: tuple[Finding, ...],
    raw: bytes,
) -> tuple[StageLoopState, StageAction]:
    """Route a checked author or correction response to correction or review."""

    state = replace(
        state,
        candidate=candidate,
        pending=findings,
        raw=raw,
        rejected=None,
        rejected_dispatch=None,
    )
    if candidate is None:
        return replace(state, review_driven=False), Correct(findings, raw, review_driven=False)
    return state, Review(candidate, state.prior_round, None, None)


def _review_step(
    state: StageLoopState, event: ReviewAnswered
) -> tuple[StageLoopState, StageAction]:
    """Route one closed review outcome to a retry, a revision, or the stage end."""

    outcome = event.outcome
    if outcome.rejected is not None:
        state = replace(
            state, rejected=outcome.rejected, rejected_dispatch=outcome.rejected_dispatch
        )
        return state, Review(
            event.candidate, state.prior_round, outcome.rejected, outcome.rejected_dispatch
        )
    if outcome.stop is not None:
        return state, Halt(outcome.stop)
    if outcome.decision == "accept":
        return state, Halt(event.candidate, review_status="accepted")
    if outcome.decision == "blocked":
        return state, Halt(
            _StageStop(state.review_blocked_status, event.findings), review_status="blocked"
        )
    # Semantic revise findings join the stage correction path but spend the
    # stage's separate review-revision allowance.
    state = replace(
        state,
        candidate=None,
        pending=event.findings,
        prior_round=PriorReviewRound(outcome.findings, event.candidate),
        review_driven=True,
        rejected=None,
        rejected_dispatch=None,
    )
    return state, Correct(event.findings, state.raw, review_driven=True, review_status="revise")
