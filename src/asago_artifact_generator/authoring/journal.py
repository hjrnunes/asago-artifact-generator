"""The authoring journal: one append-only list of immutable dispatch events.

The orchestrator records what happened as events. The ledger (returned with
the result and packaged on acceptance), the failure-evidence sidecar, and the
preserved review records are one projection of those events: a table maps
each event class to the one handler that applies it to all three, in order,
and ``flush`` writes the failure-evidence document to its sidecar.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..failure_evidence import (
    failure_evidence_path,
    metadata_record,
    new_failure_evidence,
    raw_response_record,
    write_failure_evidence,
)
from .core import Finding, PromptPacket, _safe_metadata, _set_record_usage, _sha256

_REVIEW_EVIDENCE_KEYS = (
    "decision",
    "original_decision",
    "decision_after_scope_filter",
    "summary",
    "findings",
    "out_of_scope_findings",
    "question_ids",
)


@dataclass(frozen=True)
class BudgetRecorded:
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class PolicyRecorded:
    policy: dict[str, Any]


@dataclass(frozen=True)
class ReviewStatusRecorded:
    review_status: dict[str, str]


@dataclass(frozen=True)
class AllowancesRecorded:
    allowances: dict[str, int] | None
    review_revision_allowances: dict[str, int] | None


@dataclass(frozen=True)
class RunFindingRecorded:
    """A finding raised where no dispatch was opened for the request."""

    finding: Finding


@dataclass(frozen=True)
class DispatchRequested:
    """A dispatch began; it opens only if preflight and the budget let it through."""

    stage: str


@dataclass(frozen=True)
class DispatchOpened:
    dispatch_index: int
    attempt_index: int
    correction_index: int
    role: str
    task_id: str
    packet: PromptPacket
    policy: dict[str, Any]
    raw_response_path: str
    model_identity: dict[str, Any]
    review: dict[str, Any] | None = None


@dataclass(frozen=True)
class FailureControlsRecorded:
    """The controls a transport reported for a request that raised."""

    controls: dict[str, Any]


@dataclass(frozen=True)
class TransportRetried:
    """The transport retried the open dispatch's request once after a transport error.

    ``retry_of`` holds the error class and HTTP status (``None`` for a
    connection error) of the attempt that failed.  The retry's budget
    reservation is a separate ``BudgetRecorded`` event.
    """

    attempt: int
    retry_of: dict[str, Any]


@dataclass(frozen=True)
class ResponseReturned:
    raw_response_key: str
    raw: bytes
    usage: dict[str, Any] | None
    controls: dict[str, Any] | None
    response_capture: dict[str, Any] | None
    provider_model: str | None


@dataclass(frozen=True)
class CorrectionRecorded:
    failed_stage: str
    allowance: str
    failed_response: bytes


@dataclass(frozen=True)
class ReviewControlsRecorded:
    effective_controls: dict[str, Any]


@dataclass(frozen=True)
class TransformationRecorded:
    transformation: Any
    transformations: tuple[Any, ...]


@dataclass(frozen=True)
class DecodedOutputRecorded:
    output: Any
    on_ledger: bool


@dataclass(frozen=True)
class CandidateDigested:
    sha256: str


@dataclass(frozen=True)
class ValidationTransformed:
    changes: tuple[Any, ...]
    transformations: tuple[Any, ...]


@dataclass(frozen=True)
class LedgerFindingsRecorded:
    """Findings that only the ledger record keeps, under ``field``."""

    field: str
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class ParseFailed:
    error: str


@dataclass(frozen=True)
class ChecksSkipped:
    checks: tuple[str, ...]


@dataclass(frozen=True)
class ValidationPassed:
    pass


@dataclass(frozen=True)
class AttemptFailed:
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class ResponseUnavailable:
    reason: str
    detail: str
    elapsed_ms: float | None


@dataclass(frozen=True)
class DispatchErrored:
    error: str


@dataclass(frozen=True)
class ReviewDecided:
    review: dict[str, Any]


@dataclass(frozen=True)
class ReviewEvidenceRecorded:
    review_stage: str
    status: str
    effective_controls: dict[str, Any]
    prompt_version: str
    prompt_sha256: str
    input_sha256: str
    candidate_sha256: str
    contract_sha256: str
    configuration_sha256: str
    raw_response_key: str | None = None
    raw: bytes | None = None
    review: dict[str, Any] | None = None


@dataclass(frozen=True)
class ReviewFailed:
    failure: dict[str, Any]


@dataclass(frozen=True)
class Finished:
    status: str
    terminal_findings: tuple[Finding, ...]
    terminal: dict[str, Any]


def _evidence_finding(finding: Finding) -> dict[str, Any]:
    """Return the failure-evidence record of a finding: its fields and the stage that raised it."""

    record = finding.to_dict()
    if finding.stage is not None:
        record["stage"] = finding.stage
    return record


def _retry_record(event: TransportRetried) -> dict[str, Any]:
    """Return the record of one transport retry: its attempt number and the failure it retries."""

    return {"attempt": event.attempt, "retry_of": dict(event.retry_of)}


def _record_controls(event: Any) -> dict[str, Any] | None:
    """Return the controls an event sets on its dispatch record, or None."""

    if isinstance(event, FailureControlsRecorded):
        return _safe_metadata(event.controls)
    if isinstance(event, ResponseReturned):
        return _safe_metadata(event.controls or {"max_retries": 0})
    if isinstance(event, ReviewControlsRecorded):
        return event.effective_controls
    return None


def _review_evidence_update(
    evidence: dict[str, Any], pinned: dict[str, Any], event: ReviewEvidenceRecorded
) -> None:
    """Apply one review-evidence event to a review record, in the recorded key order."""

    evidence.update(
        {
            "status": event.status,
            "prompt_version": event.prompt_version,
            "prompt_sha256": event.prompt_sha256,
            "reviewed_input_sha256": pinned.get("reviewed_input_sha256", event.input_sha256),
            "reviewed_candidate_sha256": pinned.get(
                "reviewed_candidate_sha256", event.candidate_sha256
            ),
            "candidate_bytes_sha256": pinned.get("candidate_bytes_sha256", event.candidate_sha256),
            "contract_sha256": event.contract_sha256,
            "configuration_sha256": event.configuration_sha256,
            "effective_controls": dict(event.effective_controls),
        }
    )
    if event.raw is not None:
        evidence["raw_response_key"] = event.raw_response_key
        evidence["raw_response_sha256"] = _sha256(event.raw)
        evidence["raw_response_bytes"] = len(event.raw)
    if event.review is not None:
        evidence.update(
            {
                key: deepcopy(event.review[key])
                for key in _REVIEW_EVIDENCE_KEYS
                if key in event.review
            }
        )


def _dispatch_fields(event: DispatchOpened) -> dict[str, Any]:
    """Return the fields a dispatch's ledger record and evidence attempt share."""

    return {
        "dispatch_index": event.dispatch_index,
        "attempt_index": event.attempt_index,
        "stage_attempt_index": event.attempt_index,
        "correction_index": event.correction_index,
        "role": event.role,
        "stage": event.packet.stage,
        "task_id": event.task_id,
        "policy": deepcopy(event.policy),
        "model_identity": deepcopy(event.model_identity),
        "terminal_status": "in_progress",
    }


class _Projections:
    """The ledger records, the preserved reviews, and the failure-evidence document.

    Every dispatch opens one ledger record and one evidence attempt; the two
    views differ in shape, so each handler writes both explicitly.
    """

    def __init__(self, task_id: str, package_dir: Path) -> None:
        self.records: list[dict[str, Any]] = []
        self.reviews: dict[str, dict[str, Any]] = {}
        self.document = new_failure_evidence(task_id, package_dir)
        # The pinned review digests of the latest dispatch, as its ledger record holds them.
        self.pinned: dict[str, Any] = {}

    @property
    def attempt(self) -> dict[str, Any]:
        return self.document["attempts"][-1]

    def current(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return the latest dispatch's ledger record and evidence attempt."""

        return self.records[-1], self.attempt

    def apply(self, event: Any) -> None:
        handler = _HANDLERS.get(type(event))
        if handler is not None:
            handler(self, event)

    def _on_dispatch_opened(self, event: DispatchOpened) -> None:
        packet = event.packet
        record = {
            **_dispatch_fields(event),
            "prompt_version": packet.version,
            "prompt_sha256": packet.sha256,
            "prompt_hash": packet.sha256,
            "prompt_system": packet.system,
            "prompt_user": packet.user,
            "controls": {"max_retries": 0},
            "raw_response": event.raw_response_path,
        }
        attempt = {
            **_dispatch_fields(event),
            "prompt": {
                "version": packet.version,
                "sha256": packet.sha256,
                "hash": packet.sha256,
                "system": packet.system,
                "user": packet.user,
            },
            "controls": metadata_record(
                {"max_retries": 0}, unavailable_reason="controls_not_recorded"
            ),
            "raw_response": raw_response_record(b"", reason="not_returned"),
            "usage": metadata_record(None, unavailable_reason="not_returned"),
            "findings": [],
        }
        self.pinned = deepcopy(event.review) if event.review is not None else {}
        if event.review is not None:
            record.update(deepcopy(event.review))
            attempt["review"] = deepcopy(event.review["review"])
            attempt["reviewed_input_sha256"] = event.review["reviewed_input_sha256"]
            attempt["reviewed_candidate_sha256"] = event.review["reviewed_candidate_sha256"]
        self.records.append(record)
        self.document["attempts"].append(attempt)

    def _on_failure_controls(self, event: FailureControlsRecorded) -> None:
        record, attempt = self.current()
        record["controls"] = _record_controls(event)
        attempt["controls"] = metadata_record(
            event.controls, unavailable_reason="provider_did_not_return_response"
        )

    def _on_transport_retried(self, event: TransportRetried) -> None:
        for target in self.current():
            target.setdefault("transport_retries", []).append(_retry_record(event))

    def _on_response_returned(self, event: ResponseReturned) -> None:
        record, attempt = self.current()
        for target in (record, attempt):
            target["model_identity"]["returned_model"] = metadata_record(
                event.provider_model, unavailable_reason="provider_did_not_report_model"
            )
            if event.response_capture is not None:
                target["response_capture"] = deepcopy(event.response_capture)
        record["raw_response_key"] = event.raw_response_key
        _set_record_usage(record, event.usage)
        record["controls"] = _record_controls(event)
        attempt["raw_response"] = raw_response_record(event.raw)
        attempt["usage"] = metadata_record(
            event.usage if event.usage else None,
            unavailable_reason="provider_did_not_report_usage",
        )
        attempt["controls"] = metadata_record(
            event.controls or {"max_retries": 0}, unavailable_reason="controls_not_recorded"
        )

    def _on_correction(self, event: CorrectionRecorded) -> None:
        record, attempt = self.current()
        for target in (record, attempt):
            target["failed_stage"] = event.failed_stage
            target["allowance"] = event.allowance
        attempt["failed_response"] = raw_response_record(
            event.failed_response, reason="not_returned" if not event.failed_response else None
        )

    def _on_review_controls(self, event: ReviewControlsRecorded) -> None:
        record, attempt = self.current()
        record["controls"] = _record_controls(event)
        attempt["controls"] = metadata_record(
            event.effective_controls, unavailable_reason="controls_not_recorded"
        )

    def _on_transformation(self, event: TransformationRecorded) -> None:
        for target in self.current():
            target["transformation"] = event.transformation
        self.document["transformations"] = list(event.transformations)

    def _on_decoded_output(self, event: DecodedOutputRecorded) -> None:
        # Both views keep the decoded dict itself: the stage checks rewrite it in
        # place afterwards, and the recorded output is the rewritten candidate.
        record, attempt = self.current()
        if event.on_ledger:
            record["decoded_output"] = event.output
        attempt["decoded_output"] = event.output

    def _on_candidate_digested(self, event: CandidateDigested) -> None:
        for target in self.current():
            target["candidate_sha256"] = event.sha256

    def _on_validation_transformed(self, event: ValidationTransformed) -> None:
        if self.records:
            self.records[-1]["transformations"] = deepcopy(list(event.changes))
        self.document["transformations"] = list(event.transformations)
        self.attempt["transformations"] = deepcopy(list(event.changes))

    def _on_ledger_findings(self, event: LedgerFindingsRecorded) -> None:
        self.records[-1][event.field] = [finding.to_dict() for finding in event.findings]

    def _on_parse_failed(self, event: ParseFailed) -> None:
        self.records[-1]["parse_error"] = event.error

    def _on_checks_skipped(self, event: ChecksSkipped) -> None:
        for target in self.current():
            values = list(event.checks)
            target["checks_not_run"] = values
            target["checks"] = {"status": "not_run", "not_run": values}

    def _on_validation_passed(self, event: ValidationPassed) -> None:
        self.records[-1]["validation"] = "passed"

    def _on_dispatch_errored(self, event: DispatchErrored) -> None:
        self.records[-1]["error"] = event.error

    def _on_attempt_failed(self, event: AttemptFailed) -> None:
        attempt = self.attempt
        for finding in event.findings:
            attempt["findings"].append(_evidence_finding(finding))
            self.document["findings"].append(_evidence_finding(finding))
        if event.findings:
            failure = attempt.setdefault("failure", {})
            failure.update(
                {
                    "phase": failure.get("phase", "post_response"),
                    "code": event.findings[0].code,
                    "detail": event.findings[0].detail,
                }
            )

    def _on_response_unavailable(self, event: ResponseUnavailable) -> None:
        attempt = self.attempt
        attempt["raw_response"] = raw_response_record(b"", reason=event.reason)
        attempt["usage"] = metadata_record(None, unavailable_reason=event.reason)
        attempt["failure"] = {"detail": event.detail, "phase": "invocation"}
        if event.elapsed_ms is not None:
            attempt["failure"]["elapsed_ms"] = round(event.elapsed_ms, 3)

    def _on_review_decided(self, event: ReviewDecided) -> None:
        for target in self.current():
            target["review"] = deepcopy(event.review)

    def _on_review_evidence(self, event: ReviewEvidenceRecorded) -> None:
        record, attempt = self.current()
        evidence = record.setdefault("review", {})
        _review_evidence_update(evidence, record, event)
        self.reviews[event.review_stage] = deepcopy(evidence)
        _review_evidence_update(attempt.setdefault("review", {}), self.pinned, event)

    def _on_review_failed(self, event: ReviewFailed) -> None:
        record, attempt = self.current()
        record["failure"] = dict(event.failure)
        attempt["failure"] = event.failure

    def _on_finished(self, event: Finished) -> None:
        for target in [*self.records, *self.document["attempts"]]:
            target["terminal_status"] = event.status
            target["stage_status"] = event.status
        self.document["status"] = event.status
        self.document["findings"] = [_evidence_finding(item) for item in event.terminal_findings]
        self.document["terminal"] = event.terminal

    def _on_budget(self, event: BudgetRecorded) -> None:
        self.document["budget"] = event.snapshot

    def _on_policy(self, event: PolicyRecorded) -> None:
        self.document["policy"] = event.policy

    def _on_review_status(self, event: ReviewStatusRecorded) -> None:
        self.document["review_status"] = dict(event.review_status)

    def _on_allowances(self, event: AllowancesRecorded) -> None:
        if event.allowances is not None:
            self.document["allowances"] = dict(event.allowances)
        if event.review_revision_allowances is not None:
            self.document["review_revision_allowances"] = dict(event.review_revision_allowances)

    def _on_run_finding(self, event: RunFindingRecorded) -> None:
        self.document["findings"].append(_evidence_finding(event.finding))


# One handler per event. ``DispatchRequested`` has none: only
# ``AuthoringJournal.open_dispatch`` reads it.
_HANDLERS: dict[type, Callable[[_Projections, Any], None]] = {
    BudgetRecorded: _Projections._on_budget,
    PolicyRecorded: _Projections._on_policy,
    ReviewStatusRecorded: _Projections._on_review_status,
    AllowancesRecorded: _Projections._on_allowances,
    RunFindingRecorded: _Projections._on_run_finding,
    DispatchOpened: _Projections._on_dispatch_opened,
    FailureControlsRecorded: _Projections._on_failure_controls,
    TransportRetried: _Projections._on_transport_retried,
    ResponseReturned: _Projections._on_response_returned,
    CorrectionRecorded: _Projections._on_correction,
    ReviewControlsRecorded: _Projections._on_review_controls,
    TransformationRecorded: _Projections._on_transformation,
    DecodedOutputRecorded: _Projections._on_decoded_output,
    CandidateDigested: _Projections._on_candidate_digested,
    ValidationTransformed: _Projections._on_validation_transformed,
    LedgerFindingsRecorded: _Projections._on_ledger_findings,
    ParseFailed: _Projections._on_parse_failed,
    ChecksSkipped: _Projections._on_checks_skipped,
    ValidationPassed: _Projections._on_validation_passed,
    AttemptFailed: _Projections._on_attempt_failed,
    ResponseUnavailable: _Projections._on_response_unavailable,
    DispatchErrored: _Projections._on_dispatch_errored,
    ReviewDecided: _Projections._on_review_decided,
    ReviewEvidenceRecorded: _Projections._on_review_evidence,
    ReviewFailed: _Projections._on_review_failed,
    Finished: _Projections._on_finished,
}


class AuthoringJournal:
    """The append-only event list of one authoring task and its projections."""

    def __init__(self, task_id: str, package_dir: Path) -> None:
        self.events: list[Any] = []
        self._view = _Projections(task_id, package_dir)
        self._path = failure_evidence_path(package_dir)

    def append(self, *events: Any) -> None:
        for event in events:
            if isinstance(event, ReviewEvidenceRecorded) and not self._review_dispatch_open():
                # Review evidence belongs to an open review dispatch. Without
                # one it would land on whichever record came before.
                continue
            self._view.apply(event)
            self.events.append(event)

    def flush(self) -> Path:
        """Write the failure-evidence projection to its sidecar."""

        return write_failure_evidence(self._path, self._view.document)

    def dispatches(self) -> list[DispatchOpened]:
        return [event for event in self.events if isinstance(event, DispatchOpened)]

    def corrections(self) -> list[CorrectionRecorded]:
        return [event for event in self.events if isinstance(event, CorrectionRecorded)]

    def open_dispatch(self) -> DispatchOpened | None:
        """Return the latest dispatch if the latest request opened it."""

        for event in reversed(self.events):
            if isinstance(event, DispatchOpened):
                return event
            if isinstance(event, DispatchRequested):
                return None
        return None

    def _review_dispatch_open(self) -> bool:
        opened = self.open_dispatch()
        return opened is not None and opened.role == "reviewer"

    def latest_response(self) -> ResponseReturned | None:
        """Return the response of the open dispatch, once it returned."""

        if self.open_dispatch() is None:
            return None
        for event in self._since_latest_dispatch():
            if isinstance(event, ResponseReturned):
                return event
        return None

    def _since_latest_dispatch(self) -> list[Any]:
        for index in range(len(self.events) - 1, -1, -1):
            if isinstance(self.events[index], DispatchOpened):
                return self.events[index + 1 :]
        return []

    def latest_attempt_findings(self) -> list[Finding]:
        """Return the findings recorded against the latest dispatch's attempt."""

        return [
            finding
            for event in self._since_latest_dispatch()
            if isinstance(event, AttemptFailed)
            for finding in event.findings
        ]

    def dispatch_controls(self) -> dict[str, Any]:
        """Return the controls recorded for the latest dispatch."""

        controls: dict[str, Any] = {"max_retries": 0}
        for event in self._since_latest_dispatch():
            recorded = _record_controls(event)
            if recorded is not None:
                controls = recorded
        return controls

    def latest_failed_stage(self) -> str | None:
        """Return the stage the latest dispatch corrects, once its response named it."""

        for event in self._since_latest_dispatch():
            if isinstance(event, CorrectionRecorded):
                return event.failed_stage
        return None

    @property
    def ledger(self) -> list[dict[str, Any]]:
        return self._view.records

    @property
    def reviews(self) -> dict[str, dict[str, Any]]:
        """The latest review record of each stage, for the package."""

        return self._view.reviews

    @property
    def evidence(self) -> dict[str, Any]:
        return self._view.document
