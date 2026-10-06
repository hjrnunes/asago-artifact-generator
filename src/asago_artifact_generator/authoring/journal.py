"""The authoring journal: one append-only list of immutable dispatch events.

The orchestrator records what happened as events. The ledger (returned with
the result and packaged on acceptance), the failure-evidence sidecar, and the
preserved review records are projections of those events: each projection
applies every event in order, and ``flush`` writes the failure-evidence
projection to its sidecar.
"""

from __future__ import annotations

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


class _LedgerProjection:
    """One record per dispatch, plus the latest review record of each stage."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.reviews: dict[str, dict[str, Any]] = {}

    def apply(self, event: Any) -> None:
        handler = getattr(self, f"_on_{type(event).__name__}", None)
        if handler is not None:
            handler(event)

    def _on_DispatchOpened(self, event: DispatchOpened) -> None:
        packet = event.packet
        record = {
            "dispatch_index": event.dispatch_index,
            "attempt_index": event.attempt_index,
            "stage_attempt_index": event.attempt_index,
            "correction_index": event.correction_index,
            "role": event.role,
            "stage": packet.stage,
            "task_id": event.task_id,
            "prompt_version": packet.version,
            "prompt_sha256": packet.sha256,
            "prompt_hash": packet.sha256,
            "prompt_system": packet.system,
            "prompt_user": packet.user,
            "controls": {"max_retries": 0},
            "policy": deepcopy(event.policy),
            "raw_response": event.raw_response_path,
            "model_identity": deepcopy(event.model_identity),
            "terminal_status": "in_progress",
        }
        if event.review is not None:
            record.update(deepcopy(event.review))
        self.records.append(record)

    def _on_FailureControlsRecorded(self, event: FailureControlsRecorded) -> None:
        self.records[-1]["controls"] = _safe_metadata(event.controls)

    def _on_ResponseReturned(self, event: ResponseReturned) -> None:
        record = self.records[-1]
        record["model_identity"]["returned_model"] = metadata_record(
            event.provider_model,
            unavailable_reason="provider_did_not_report_model",
        )
        record["raw_response_key"] = event.raw_response_key
        _set_record_usage(record, event.usage)
        record["controls"] = _safe_metadata(event.controls or {"max_retries": 0})
        if event.response_capture is not None:
            record["response_capture"] = deepcopy(event.response_capture)

    def _on_CorrectionRecorded(self, event: CorrectionRecorded) -> None:
        self.records[-1]["failed_stage"] = event.failed_stage
        self.records[-1]["allowance"] = event.allowance

    def _on_ReviewControlsRecorded(self, event: ReviewControlsRecorded) -> None:
        self.records[-1]["controls"] = event.effective_controls

    def _on_TransformationRecorded(self, event: TransformationRecorded) -> None:
        self.records[-1]["transformation"] = event.transformation

    def _on_DecodedOutputRecorded(self, event: DecodedOutputRecorded) -> None:
        if event.on_ledger:
            self.records[-1]["decoded_output"] = event.output

    def _on_CandidateDigested(self, event: CandidateDigested) -> None:
        self.records[-1]["candidate_sha256"] = event.sha256

    def _on_ValidationTransformed(self, event: ValidationTransformed) -> None:
        if self.records:
            self.records[-1]["transformations"] = deepcopy(list(event.changes))

    def _on_LedgerFindingsRecorded(self, event: LedgerFindingsRecorded) -> None:
        self.records[-1][event.field] = [finding.to_dict() for finding in event.findings]

    def _on_ParseFailed(self, event: ParseFailed) -> None:
        self.records[-1]["parse_error"] = event.error

    def _on_ChecksSkipped(self, event: ChecksSkipped) -> None:
        values = list(event.checks)
        self.records[-1]["checks_not_run"] = values
        self.records[-1]["checks"] = {"status": "not_run", "not_run": values}

    def _on_ValidationPassed(self, event: ValidationPassed) -> None:
        self.records[-1]["validation"] = "passed"

    def _on_DispatchErrored(self, event: DispatchErrored) -> None:
        self.records[-1]["error"] = event.error

    def _on_ReviewDecided(self, event: ReviewDecided) -> None:
        self.records[-1]["review"] = deepcopy(event.review)

    def _on_ReviewEvidenceRecorded(self, event: ReviewEvidenceRecorded) -> None:
        record = self.records[-1]
        evidence = record.setdefault("review", {})
        _review_evidence_update(evidence, record, event)
        self.reviews[event.review_stage] = deepcopy(evidence)

    def _on_ReviewFailed(self, event: ReviewFailed) -> None:
        self.records[-1]["failure"] = dict(event.failure)

    def _on_Finished(self, event: Finished) -> None:
        for record in self.records:
            record["terminal_status"] = event.status
            record["stage_status"] = event.status


class _EvidenceProjection:
    """The failure-evidence document: run fields plus one attempt per dispatch."""

    def __init__(self, task_id: str, package_dir: Path) -> None:
        self.document = new_failure_evidence(task_id, package_dir)
        # The pinned review digests of the latest dispatch, as its ledger record holds them.
        self._pinned: dict[str, Any] = {}

    def _attempt(self) -> dict[str, Any]:
        return self.document["attempts"][-1]

    def apply(self, event: Any) -> None:
        handler = getattr(self, f"_on_{type(event).__name__}", None)
        if handler is not None:
            handler(event)

    def _on_BudgetRecorded(self, event: BudgetRecorded) -> None:
        self.document["budget"] = event.snapshot

    def _on_PolicyRecorded(self, event: PolicyRecorded) -> None:
        self.document["policy"] = event.policy

    def _on_ReviewStatusRecorded(self, event: ReviewStatusRecorded) -> None:
        self.document["review_status"] = dict(event.review_status)

    def _on_AllowancesRecorded(self, event: AllowancesRecorded) -> None:
        if event.allowances is not None:
            self.document["allowances"] = dict(event.allowances)
        if event.review_revision_allowances is not None:
            self.document["review_revision_allowances"] = dict(event.review_revision_allowances)

    def _on_RunFindingRecorded(self, event: RunFindingRecorded) -> None:
        self.document["findings"].append(event.finding.to_dict())

    def _on_DispatchOpened(self, event: DispatchOpened) -> None:
        packet = event.packet
        attempt = {
            "dispatch_index": event.dispatch_index,
            "attempt_index": event.attempt_index,
            "stage_attempt_index": event.attempt_index,
            "correction_index": event.correction_index,
            "role": event.role,
            "stage": packet.stage,
            "task_id": event.task_id,
            "policy": deepcopy(event.policy),
            "prompt": {
                "version": packet.version,
                "sha256": packet.sha256,
                "hash": packet.sha256,
                "system": packet.system,
                "user": packet.user,
            },
            "controls": metadata_record(
                {"max_retries": 0},
                unavailable_reason="controls_not_recorded",
            ),
            "model_identity": deepcopy(event.model_identity),
            "raw_response": raw_response_record(b"", reason="not_returned"),
            "usage": metadata_record(None, unavailable_reason="not_returned"),
            "findings": [],
            "terminal_status": "in_progress",
        }
        self._pinned = deepcopy(event.review) if event.review is not None else {}
        if event.review is not None:
            attempt["review"] = deepcopy(event.review["review"])
            attempt["reviewed_input_sha256"] = event.review["reviewed_input_sha256"]
            attempt["reviewed_candidate_sha256"] = event.review["reviewed_candidate_sha256"]
        self.document["attempts"].append(attempt)

    def _on_FailureControlsRecorded(self, event: FailureControlsRecorded) -> None:
        self._attempt()["controls"] = metadata_record(
            event.controls,
            unavailable_reason="provider_did_not_return_response",
        )

    def _on_ResponseReturned(self, event: ResponseReturned) -> None:
        attempt = self._attempt()
        attempt["model_identity"]["returned_model"] = metadata_record(
            event.provider_model,
            unavailable_reason="provider_did_not_report_model",
        )
        attempt["raw_response"] = raw_response_record(event.raw)
        attempt["usage"] = metadata_record(
            event.usage if event.usage else None,
            unavailable_reason="provider_did_not_report_usage",
        )
        attempt["controls"] = metadata_record(
            event.controls or {"max_retries": 0},
            unavailable_reason="controls_not_recorded",
        )
        if event.response_capture is not None:
            attempt["response_capture"] = deepcopy(event.response_capture)

    def _on_CorrectionRecorded(self, event: CorrectionRecorded) -> None:
        attempt = self._attempt()
        attempt["failed_stage"] = event.failed_stage
        attempt["allowance"] = event.allowance
        attempt["failed_response"] = raw_response_record(
            event.failed_response,
            reason="not_returned" if not event.failed_response else None,
        )

    def _on_ReviewControlsRecorded(self, event: ReviewControlsRecorded) -> None:
        self._attempt()["controls"] = metadata_record(
            event.effective_controls,
            unavailable_reason="controls_not_recorded",
        )

    def _on_TransformationRecorded(self, event: TransformationRecorded) -> None:
        self._attempt()["transformation"] = event.transformation
        self.document["transformations"] = list(event.transformations)

    def _on_DecodedOutputRecorded(self, event: DecodedOutputRecorded) -> None:
        self._attempt()["decoded_output"] = event.output

    def _on_CandidateDigested(self, event: CandidateDigested) -> None:
        self._attempt()["candidate_sha256"] = event.sha256

    def _on_ValidationTransformed(self, event: ValidationTransformed) -> None:
        self.document["transformations"] = list(event.transformations)
        self._attempt()["transformations"] = deepcopy(list(event.changes))

    def _on_ChecksSkipped(self, event: ChecksSkipped) -> None:
        values = list(event.checks)
        self._attempt()["checks_not_run"] = values
        self._attempt()["checks"] = {"status": "not_run", "not_run": values}

    def _on_AttemptFailed(self, event: AttemptFailed) -> None:
        attempt = self._attempt()
        for finding in event.findings:
            attempt["findings"].append(finding.to_dict())
            self.document["findings"].append(finding.to_dict())
        if event.findings:
            failure = attempt.setdefault("failure", {})
            failure.update(
                {
                    "phase": failure.get("phase", "post_response"),
                    "code": event.findings[0].code,
                    "detail": event.findings[0].detail,
                }
            )

    def _on_ResponseUnavailable(self, event: ResponseUnavailable) -> None:
        attempt = self._attempt()
        attempt["raw_response"] = raw_response_record(b"", reason=event.reason)
        attempt["usage"] = metadata_record(None, unavailable_reason=event.reason)
        attempt["failure"] = {"detail": event.detail, "phase": "invocation"}
        if event.elapsed_ms is not None:
            attempt["failure"]["elapsed_ms"] = round(event.elapsed_ms, 3)

    def _on_ReviewDecided(self, event: ReviewDecided) -> None:
        self._attempt()["review"] = deepcopy(event.review)

    def _on_ReviewEvidenceRecorded(self, event: ReviewEvidenceRecorded) -> None:
        attempt = self._attempt()
        evidence = attempt.setdefault("review", {})
        _review_evidence_update(evidence, self._pinned, event)

    def _on_ReviewFailed(self, event: ReviewFailed) -> None:
        self._attempt()["failure"] = event.failure

    def _on_Finished(self, event: Finished) -> None:
        self.document["status"] = event.status
        self.document["findings"] = [finding.to_dict() for finding in event.terminal_findings]
        self.document["terminal"] = event.terminal
        for attempt in self.document["attempts"]:
            attempt["terminal_status"] = event.status
            attempt["stage_status"] = event.status


class AuthoringJournal:
    """The append-only event list of one authoring task and its projections."""

    def __init__(self, task_id: str, package_dir: Path) -> None:
        self.events: list[Any] = []
        self._ledger = _LedgerProjection()
        self._evidence = _EvidenceProjection(task_id, package_dir)
        self._path = failure_evidence_path(package_dir)
        self._written: Path | None = None

    def append(self, *events: Any) -> None:
        for event in events:
            if isinstance(event, ReviewEvidenceRecorded) and not self._review_dispatch_open():
                # Review evidence belongs to an open review dispatch. Without
                # one it would land on whichever record came before.
                continue
            self._ledger.apply(event)
            self._evidence.apply(event)
            self.events.append(event)

    def flush(self) -> Path:
        """Write the failure-evidence projection to its sidecar."""

        self._written = write_failure_evidence(self._path, self._evidence.document)
        return self._written

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

    def latest_failed_stage(self) -> str | None:
        """Return the stage the latest dispatch corrects, once its response named it."""

        for event in self._since_latest_dispatch():
            if isinstance(event, CorrectionRecorded):
                return event.failed_stage
        return None

    @property
    def ledger(self) -> list[dict[str, Any]]:
        return self._ledger.records

    @property
    def reviews(self) -> dict[str, dict[str, Any]]:
        """The latest review record of each stage, for the package."""

        return self._ledger.reviews

    @property
    def evidence(self) -> dict[str, Any]:
        return self._evidence.document

    @property
    def written(self) -> Path | None:
        return self._written
