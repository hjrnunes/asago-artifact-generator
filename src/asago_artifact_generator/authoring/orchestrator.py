"""The authoring orchestrator: runs plan, artifact, review, and correction stages
within the policy's request allowance and writes the package or failure evidence.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..contract_kit import ClaimLevel
from ..failure_evidence import metadata_record
from ..input_adapter import InputView
from ..package_io import write_package
from .binding_repair import correction_repair_inputs
from .checks import (
    _is_blocked_plan,
    _plan_claim_level,
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
)
from .condition_types import operand_type_findings
from .context_budget import _enforce_prompt_size
from .core import (
    _REVIEW_STAGES,
    FINDING_STAGE_KEYS,
    MAX_AUTHOR_CORRECTION_REQUESTS_PER_TASK,
    MAX_AUTHORING_REQUESTS,
    MAX_RENDERED_PROMPT_BYTES,
    MAX_REVIEW_REQUESTS_PER_TASK,
    REVIEW_REVISION_ALLOWANCE_PER_STAGE,
    ArtifactValidationError,
    AuthoringError,
    AuthoringTransport,
    BudgetExceeded,
    Finding,
    FramingError,
    PromptOverflowError,
    PromptPacket,
    PromptPreflightError,
    ReviewResponse,
    ReviewResponseError,
    TransportResponse,
    _canonical_json,
    _prompt_overflow_finding,
    _prompt_preflight_finding,
    _safe_error,
    _safe_metadata,
    _sha256,
    staged_findings,
)
from .correction import _render_correction_packet, build_correction_context
from .journal import (
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
from .oracle_self_test import DEFECT_CODE, oracle_defect_findings
from .package_assembly import _package_from_responses, _persist_blocked_plan
from .policy import (
    AuthoringBudget,
    AuthoringPolicy,
    AuthoringResult,
    policy_max_dispatches,
    policy_role_limits,
)
from .prompt_context import (
    build_artifact_author_context,
    build_plan_author_context,
    scenario_provenance_ids,
)
from .prompt_packets import build_call1_packet_v2, build_call2_packet_v2
from .prompt_safety import assert_no_secrets, prompt_data_urls
from .response_decode import (
    _decode_stage_response,
    _response_parts,
)
from .review import (
    REVIEW_FINDINGS_OMITTED,
    PriorReviewRound,
    _review_configuration_digest,
    _review_contract_digest,
    _review_finding_to_finding,
    _review_packet_digests,
    _review_question_ids,
    _scope_review_response,
    build_artifact_review_packet,
    build_plan_review_packet,
    parse_review_response,
)
from .sequential_turns import (
    multi_turn_payload,
    sequential_artifact_findings,
    sequential_plan_findings,
)
from .shape_gate import shape_refusal_findings

CRASH_FINDING_CODE = "authoring_crashed"
_CRASH_MESSAGE_LIMIT = 200


@dataclass(frozen=True)
class _StageStop:
    """One terminal stop of the stage-local orchestration state machine."""

    status: str
    findings: tuple[Finding, ...] = ()


def _preflight_stop(exc: PromptPreflightError, stage: str) -> _StageStop:
    """Return the stage stop for a prompt that failed its pre-dispatch checks."""

    finding = _prompt_preflight_finding(exc, stage)
    status = "prompt_overflow" if finding.code == "prompt_overflow" else "failed"
    return _StageStop(status, (finding,))


def _validate_orchestrator_arguments(
    transport: AuthoringTransport,
    discovery_provenance: dict[str, Any] | None,
    policy: AuthoringPolicy,
) -> None:
    if getattr(transport, "max_retries", None) != 0:
        raise ValueError("authoring transport must set max_retries=0")
    if discovery_provenance is not None and not isinstance(discovery_provenance, dict):
        raise ValueError("discovery_provenance must be a mapping")
    if not isinstance(policy, AuthoringPolicy):
        raise ValueError("policy must be an AuthoringPolicy instance")


def _default_budget(policy: AuthoringPolicy) -> AuthoringBudget:
    """Cover the policy's own closed worst case.

    An explicitly supplied budget is honored as an earlier stop and never
    raised to the policy maximum.
    """

    role_limits = policy_role_limits(policy)
    return AuthoringBudget(
        aggregate_limit=MAX_AUTHORING_REQUESTS,
        task_limit=policy_max_dispatches(policy),
        author_limit=max(MAX_AUTHOR_CORRECTION_REQUESTS_PER_TASK, role_limits["author"]),
        review_limit=max(MAX_REVIEW_REQUESTS_PER_TASK, role_limits["reviewer"]),
    )


def _correction_checks_not_run(failed_stage: str) -> list[str]:
    if failed_stage == "call1":
        return ["plan_validation"]
    return ["artifact_validation"]


def _secret_in(decoded: dict[str, Any]) -> str | None:
    """Return why a decoded response is rejected for carrying a secret, if it is."""

    try:
        assert_no_secrets(decoded)
    except AuthoringError as exc:
        return str(exc)
    return None


def _staged(finding: Finding) -> Finding:
    """Mark a finding located at a stage key with that key's logical stage."""

    return replace(finding, stage=FINDING_STAGE_KEYS.get(finding.path))


def _handoff_claim_level(view: InputView) -> Any:
    outcome = view.payload.get("safe_observable_outcome")
    return outcome.get("claim_level") if isinstance(outcome, dict) else None


def _tool_call_condition_missing(view: InputView) -> Finding | None:
    """Return the terminal finding when a command-attempt package has no bound condition.

    A command_attempt package is scored by a tool-call condition over captured
    arguments; without a bound condition it has nothing to execute.
    """

    status = view.tool_call_condition_status
    if status.get("status") == "bound":
        return None
    reason = status.get("reason")
    detail = status.get("detail")
    text = f"the handoff tool-call condition is {status.get('status')} ({reason})"
    if detail:
        text = f"{text}: {detail}"
    details = {"status": status.get("status"), "reason": reason}
    if detail:
        details["detail"] = detail
    return _staged(
        Finding("tool_call_condition_missing", text, "tool_call_condition_status", details)
    )


def _command_attempt_condition_findings(
    view: InputView, claim_level: Any, inventory: dict[str, Any]
) -> list[Finding]:
    """Return the findings that leave a command-attempt claim without a usable condition.

    A condition that is missing, or whose value comparisons compare operand
    types that cannot match, decides nothing; the model cannot repair either.
    """

    if claim_level != ClaimLevel.COMMAND_ATTEMPT:
        return []
    missing = _tool_call_condition_missing(view)
    if missing is not None:
        return [missing]
    return operand_type_findings(view.tool_call_condition, inventory)


def _attempt_terminal_stage(stage: str, failed_stage: str | None) -> str | None:
    if stage in FINDING_STAGE_KEYS:
        return FINDING_STAGE_KEYS[stage]
    if stage == "correction":
        return "plan" if failed_stage == "call1" else "artifact"
    return None


@dataclass(frozen=True)
class _Stage:
    """What differs between the plan and the artifact stage; the loop is shared."""

    key: str
    author: str
    author_packet: Callable[[], PromptPacket]
    checks: Callable[[Any], list[Finding]]
    # Stands in for the findings when the author request left none.
    missing: Finding
    # Builds the review packet for a checked candidate, the earlier review round
    # it answers (if any), and its check findings; None when review is off.
    review_packet: (
        Callable[[dict[str, Any], PriorReviewRound | None, Sequence[Finding]], PromptPacket] | None
    )
    # The run status when the review decides the candidate is blocked.
    review_blocked_status: str
    # Only a plan may decline the experiment; a blocked plan stops its stage.
    may_block: bool = False

    @property
    def review_stage(self) -> str:
        return f"{self.key}_review"


@dataclass(frozen=True)
class _Revision:
    """Semantic review findings that send the candidate back for a revision."""

    findings: tuple[Finding, ...]
    # The in-scope findings as the reviewer wrote them.
    records: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class _ReviewOutcome:
    """One completed semantic-review dispatch and its closed classification."""

    decision: str
    findings: tuple[dict[str, Any], ...] = ()
    stop: _StageStop | None = None
    raw: bytes = b""


class AuthoringOrchestrator:
    """Run target-free authoring under a stage-local correction policy.

    Each stage keeps its own correction allowance, deterministic checks
    precede every semantic review, and review decisions
    route corrections without shared state.
    """

    def __init__(
        self,
        *,
        transport: AuthoringTransport,
        package_dir: str | Path,
        task_id: str,
        budget: AuthoringBudget | None = None,
        policy: AuthoringPolicy,
        discovery_provenance: dict[str, Any] | None = None,
    ) -> None:
        _validate_orchestrator_arguments(transport, discovery_provenance, policy)
        self.transport = transport
        self.package_dir = Path(package_dir)
        self.task_id = task_id
        self.discovery_provenance = deepcopy(discovery_provenance or {})
        self.policy = policy
        self.review_model_profile = policy.review_model_profile
        # Policy and transport controls are fixed for the run.
        self._policy_record = self._build_policy_record()
        self._base_review_controls = self._build_base_review_controls()
        if budget is None:
            budget = _default_budget(policy)
        self.budget = budget
        self._journal = AuthoringJournal(task_id, self.package_dir)
        self._journal.append(BudgetRecorded(self.budget.snapshot(self.task_id)))
        self._findings: list[Finding] = []
        self._allowances: dict[str, int] | None = None
        self._review_revision_allowances: dict[str, int] | None = None
        self._review_status: dict[str, str] | None = None
        self._raw_responses: dict[str, bytes] = {}
        self._decoded_responses: dict[str, Any] = {}
        self._prompt_packets: dict[str, PromptPacket] = {}
        self._transformations: list[Any] = []
        # The stage a crash is charged to; None until the first stage starts.
        self._active_stage: str | None = None

    def _record_dispatch_failure(
        self,
        exc: Exception,
        packet: PromptPacket,
        started: float,
        *,
        path: str | None = None,
        failure_code: str = "transport_failure",
    ) -> Finding:
        """Record a request that returned no response and return its finding.

        ``path`` (default: the packet stage) locates a budget or provider
        failure; a provider failure gets ``failure_code``.
        """

        path = packet.stage if path is None else path
        if isinstance(exc, PromptOverflowError):
            finding = replace(
                _prompt_overflow_finding(exc, packet.stage), stage=FINDING_STAGE_KEYS.get(path)
            )
        elif isinstance(exc, BudgetExceeded):
            finding = _staged(Finding("budget_exhausted", _safe_error(exc), path))
        elif isinstance(exc, PromptPreflightError) and not self._journal.dispatches():
            # The first request of the run has no earlier attempt to carry the
            # failure, so it ends like a packet-build preflight failure.
            finding = _prompt_preflight_finding(exc, packet.stage)
        else:
            finding = _staged(Finding(failure_code, _safe_error(exc), path))
        self._findings.append(finding)
        if finding.code in {"prompt_overflow", "budget_exhausted", "prompt_preflight"}:
            # These stops happen before dispatch: no ledger record or stage
            # attempt exists to annotate.
            self._record_run_finding(finding)
            return finding
        if self._journal.open_dispatch() is not None:
            if packet.stage in _REVIEW_STAGES:
                self._record_unavailable_review_dispatch(exc, packet)
            else:
                self._journal.append(DispatchErrored(_safe_error(exc)))
        self._record_unavailable_response(
            reason="provider_failure",
            detail=_safe_error(exc),
            finding=finding,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
        return finding

    def _author(
        self, packet: PromptPacket, checks: Callable[[Any], list[Finding]]
    ) -> tuple[dict[str, Any] | None, list[Finding], bytes]:
        """Dispatch one stage author request and check its response.

        The returned candidate is None while findings remain.
        """

        stage = packet.stage
        self._prompt_packets[stage] = packet
        started = time.monotonic()
        try:
            response = self._dispatch(packet)
        except Exception as exc:
            return None, [self._record_dispatch_failure(exc, packet, started)], b""
        # ``_dispatch`` already journaled the response on its dispatch.
        raw = _response_parts(response)[0]
        self._raw_responses[stage] = raw
        self._journal.flush()
        checked = self._decode_and_check(raw, stage, checks, correction=False)
        assert checked is not None
        return (*checked, raw)

    def _decode_and_check(
        self,
        raw: bytes,
        author_stage: str,
        checks: Callable[[Any], list[Finding]],
        *,
        correction: bool,
    ) -> tuple[dict[str, Any] | None, list[Finding]] | None:
        """Decode one author or correction response of ``author_stage`` and run its checks.

        Return the candidate (None while findings remain) and the recorded
        findings.  A correction that fails to decode or carries a secret
        returns None: its stage stops.  The two paths differ in finding codes
        and journal events; both are part of the failure evidence.
        """

        try:
            decoded, transformation = _decode_stage_response(author_stage, raw)
        except (FramingError, UnicodeDecodeError) as exc:
            findings = self._reject_undecodable(exc, author_stage, correction=correction)
            return None if correction else (None, findings)
        if transformation:
            self._record_transformation(transformation)
        secret = self._record_decoded(decoded, author_stage, correction=correction)
        if secret is not None:
            return None if correction else (None, [secret])
        transformation_count = len(self._transformations)
        findings = checks(decoded)
        self._record_validation_transformations(transformation_count)
        if findings:
            self._reject(findings, LedgerFindingsRecorded("findings", tuple(findings)))
            return None, findings
        self._journal.append(ValidationPassed())
        self._journal.flush()
        return decoded, []

    def _reject_undecodable(
        self, exc: FramingError | UnicodeDecodeError, author_stage: str, *, correction: bool
    ) -> list[Finding]:
        """Record a response that did not decode and return its findings."""

        checks_not_run = self._checks_skipped(_correction_checks_not_run(author_stage))
        if isinstance(exc, FramingError):
            findings = [_staged(finding) for finding in exc.findings]
            self._reject(
                findings,
                LedgerFindingsRecorded("framing_findings", tuple(findings)),
                checks_not_run,
            )
            return findings
        if correction:
            finding = _staged(Finding("correction_failed", str(exc), author_stage))
            self._reject(
                [finding],
                LedgerFindingsRecorded("findings", (finding,)),
                ParseFailed(str(exc)),
                checks_not_run,
            )
            return [finding]
        finding = _staged(Finding("response_parse_error", str(exc), author_stage))
        self._reject([finding], ParseFailed(str(exc)), checks_not_run)
        return [finding]

    def _record_decoded(
        self, decoded: dict[str, Any], author_stage: str, *, correction: bool
    ) -> Finding | None:
        """Record a decoded response; return the finding that rejects it for a secret.

        A correction is rejected before its decoded output is recorded; an
        author response after.
        """

        secret = _secret_in(decoded)
        if correction:
            if secret is not None:
                finding = _staged(Finding("correction_failed", secret, author_stage))
                self._reject([finding], LedgerFindingsRecorded("findings", (finding,)))
                return finding
            self._decoded_responses[f"correction-{author_stage}"] = decoded
            self._journal.append(DecodedOutputRecorded(decoded, on_ledger=False))
            self._record_candidate_digest(decoded)
            self._journal.flush()
            return None
        self._decoded_responses[author_stage] = decoded
        self._journal.append(DecodedOutputRecorded(decoded, on_ledger=True))
        self._record_candidate_digest(decoded)
        if secret is None:
            return None
        finding = _staged(Finding("secret_in_response", secret, author_stage))
        self._reject([finding])
        return finding

    def _reject(self, findings: list[Finding], *events: Any) -> None:
        """Record the findings that reject a response, after the events that explain them."""

        self._findings.extend(findings)
        self._journal.append(*events)
        self._record_failures(findings)

    def _dispatch(self, packet: PromptPacket) -> TransportResponse | str | bytes:
        self._journal.append(DispatchRequested(packet.stage))
        # Reject an over-budget request before reserving an author/reviewer slot.
        context_preflight = getattr(self.transport, "preflight_context_budget", None)
        if callable(context_preflight):
            context_preflight(packet)
        # Reserve before creating any ledger or evidence record: a budget stop
        # happens before dispatch, so it leaves no dispatch event behind.
        role = "reviewer" if packet.stage in _REVIEW_STAGES else "author"
        self.budget.reserve(self.task_id, role=role)
        dispatch_index = len(self._journal.dispatches()) + 1
        self._journal.append(
            BudgetRecorded(self.budget.snapshot(self.task_id)),
            self._dispatch_opened(packet, dispatch_index, role),
        )
        self._journal.flush()
        try:
            response = self._complete_with_retry_accounting(packet, role)
        except Exception:
            self._record_transport_failure_controls()
            raise
        self._record_dispatch_response(dispatch_index, response)
        self._journal.flush()
        return response

    def _complete_with_retry_accounting(
        self, packet: PromptPacket, role: str
    ) -> TransportResponse | str | bytes:
        """Complete a request; a transport retry reserves budget and lands on the dispatch.

        A transport that makes the one retry after a transport error exposes
        ``retry_gate`` and ``last_retries``.  The retry is another request:
        it reserves the budget the first request reserved, and without budget
        it does not happen.  A transport without those attributes never retries.
        """

        has_gate = hasattr(self.transport, "retry_gate")
        if has_gate:
            self.transport.retry_gate = lambda: self._reserve_transport_retry(role)
        try:
            return self.transport.complete(packet)
        finally:
            if has_gate:
                self.transport.retry_gate = None
            for retry in getattr(self.transport, "last_retries", None) or []:
                self._journal.append(TransportRetried(retry["attempt"], retry["retry_of"]))

    def _reserve_transport_retry(self, role: str) -> bool:
        """Reserve one request for a transport retry; report whether the budget allowed it."""

        try:
            self.budget.reserve(self.task_id, role=role)
        except BudgetExceeded:
            return False
        self._journal.append(BudgetRecorded(self.budget.snapshot(self.task_id)))
        return True

    def _dispatch_attempt_indices(self, packet: PromptPacket) -> tuple[int, int]:
        """Return the stage attempt index and the per-failed-stage correction index."""

        stage_attempt_index = (
            sum(1 for prior in self._journal.dispatches() if prior.packet.stage == packet.stage)
            + 1
        )
        if packet.stage != "correction":
            return stage_attempt_index, 0
        failed_stage = (
            packet.payload.get("failed_stage") if isinstance(packet.payload, dict) else None
        )
        correction_index = (
            sum(1 for prior in self._journal.corrections() if prior.failed_stage == failed_stage)
            + 1
        )
        return stage_attempt_index, correction_index

    def _dispatch_model_identity(self) -> dict[str, Any]:
        """Return the requested model identity before the provider reports its own."""

        profile_alias = getattr(self.transport, "profile_name", None)
        if not isinstance(profile_alias, str) or not profile_alias.strip():
            profile_alias = None
        requested_model = getattr(self.transport, "model", None)
        if not isinstance(requested_model, str) or not requested_model.strip():
            requested_model = None
        return {
            "profile_alias": profile_alias,
            "requested_model": requested_model,
            "returned_model": metadata_record(None, unavailable_reason="not_returned"),
        }

    def _dispatch_opened(
        self, packet: PromptPacket, dispatch_index: int, role: str
    ) -> DispatchOpened:
        """Return the event that opens the ledger record and attempt of a dispatch."""

        stage_attempt_index, correction_index = self._dispatch_attempt_indices(packet)
        return DispatchOpened(
            dispatch_index=dispatch_index,
            attempt_index=stage_attempt_index,
            correction_index=correction_index,
            role=role,
            task_id=self.task_id,
            packet=packet,
            policy=self._effective_policy_record(),
            raw_response_path=f"authoring/{dispatch_index:02d}-{packet.stage}.raw",
            model_identity=self._dispatch_model_identity(),
            review=(
                self._review_dispatch_fields(packet) if packet.stage in _REVIEW_STAGES else None
            ),
        )

    def _review_dispatch_fields(self, packet: PromptPacket) -> dict[str, Any]:
        """Return the pending review fields that a review dispatch adds to its record."""

        input_digest, candidate_digest = _review_packet_digests(packet)
        effective_controls = self._review_controls(None)
        return {
            "reviewed_input_sha256": input_digest,
            "reviewed_candidate_sha256": candidate_digest,
            "candidate_bytes_sha256": candidate_digest,
            "review": {
                "status": "pending",
                "prompt_version": packet.version,
                "prompt_sha256": packet.sha256,
                "reviewed_input_sha256": input_digest,
                "reviewed_candidate_sha256": candidate_digest,
                "candidate_bytes_sha256": candidate_digest,
                "contract_sha256": _review_contract_digest(packet),
                "configuration_sha256": _review_configuration_digest(
                    effective_controls,
                    self._effective_policy_record(),
                ),
                "effective_controls": effective_controls,
            },
        }

    def _record_transport_failure_controls(self) -> None:
        """Keep the controls a transport reports for a request that raised."""

        last_controls = getattr(self.transport, "last_controls", None)
        if isinstance(last_controls, dict):
            self._journal.append(FailureControlsRecorded(last_controls))

    def _record_dispatch_response(
        self,
        dispatch_index: int,
        response: TransportResponse | str | bytes,
    ) -> None:
        """Record the returned model, raw bytes, usage, and controls of a response."""

        provider_model = (
            response.provider_model if isinstance(response, TransportResponse) else None
        )
        raw, usage, controls, response_capture = _response_parts(response)
        raw_key = f"dispatch:{dispatch_index}"
        self._raw_responses[raw_key] = raw
        self._journal.append(
            ResponseReturned(
                raw_response_key=raw_key,
                raw=raw,
                usage=usage,
                controls=controls,
                response_capture=response_capture,
                provider_model=provider_model,
            )
        )

    def _correction_v2(
        self,
        stage: _Stage,
        *,
        failed_response: bytes,
        findings: list[Finding],
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
        allowance_kind: str,
    ) -> tuple[dict[str, Any] | None, list[Finding], bytes] | None:
        """Replace one failed v2 response in its original stage format.

        The caller owns the stage-local allowance decision. A syntactically
        valid candidate is returned with its validation findings.
        ``allowance_kind`` records which stage allowance the caller spent on
        the dispatched correction.
        """

        failed_stage = stage.author
        packet = self._correction_packet(
            failed_stage=failed_stage,
            failed_response=failed_response,
            findings=findings,
            view=view,
            inventory=inventory,
            runtime_contract=runtime_contract,
        )
        if packet is None:
            return None
        self._prompt_packets["correction"] = packet
        started = time.monotonic()
        try:
            response = self._dispatch(packet)
        except Exception as exc:
            self._record_dispatch_failure(
                exc,
                packet,
                started,
                path=failed_stage,
                failure_code="correction_dispatch_failed",
            )
            return None
        raw = self._record_correction_response(
            response,
            failed_stage=failed_stage,
            failed_response=failed_response,
            allowance_kind=allowance_kind,
        )
        checked = self._decode_and_check(raw, failed_stage, stage.checks, correction=True)
        if checked is None:
            return None
        candidate, findings = checked
        if candidate is not None:
            self._raw_responses[failed_stage] = raw
            self._decoded_responses[failed_stage] = candidate
        return candidate, findings, raw

    def _correction_packet(
        self,
        *,
        failed_stage: str,
        failed_response: bytes,
        findings: list[Finding],
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> PromptPacket | None:
        """Render the correction prompt; record a preflight failure and return None."""

        stage_context = (
            build_plan_author_context(view, inventory, runtime_contract)
            if failed_stage == "call1"
            else build_artifact_author_context(
                view,
                self._decoded_responses["call1"],
                inventory,
                runtime_contract,
            )
        )
        # The plan correction reads the turn count and delivery from this block; the artifact
        # correction shows it to the model, as the first Call 2 request does.
        original_context = {**stage_context, **multi_turn_payload(view, runtime_contract)}
        correction_payload = build_correction_context(
            failed_stage=failed_stage,
            original_context=original_context,
            current_output=failed_response,
            findings=findings,
        )
        packet = _render_correction_packet(
            correction_payload,
            correction_repair_inputs(view, inventory, runtime_contract),
        )
        try:
            _enforce_prompt_size(
                packet,
                MAX_RENDERED_PROMPT_BYTES,
                allowed_urls=prompt_data_urls(
                    view,
                    failed_response,
                    self._decoded_responses.get("call1"),
                    findings,
                ),
            )
        except PromptPreflightError as exc:
            finding = replace(
                _prompt_preflight_finding(exc, "correction"),
                stage=FINDING_STAGE_KEYS.get(failed_stage),
            )
            self._findings.append(finding)
            self._record_run_finding(finding)
            return None
        return packet

    def _record_correction_response(
        self,
        response: Any,
        *,
        failed_stage: str,
        failed_response: bytes,
        allowance_kind: str,
    ) -> bytes:
        """Record what the correction replaces and which allowance it spent; return its bytes."""

        raw = _response_parts(response)[0]
        self._raw_responses["correction"] = raw
        self._journal.append(CorrectionRecorded(failed_stage, allowance_kind, failed_response))
        self._journal.flush()
        return raw

    def run(
        self,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> AuthoringResult:
        """Run the stage-local correction and review state machine.

        Each stage owns its correction allowance; deterministic checks precede
        every semantic review; and review decisions
        route corrections without shared state or hidden retries. An exception
        the machine does not handle ends the sidecar as ``failed`` with an
        ``authoring_crashed`` finding, then propagates unchanged.
        """

        try:
            return self._run(view, inventory, runtime_contract)
        except Exception as exc:
            self._record_crash(exc)
            raise

    def _record_crash(self, exc: Exception) -> None:
        """Close the journal for an unhandled exception without masking it."""

        detail = f"{type(exc).__name__}: {_safe_error(exc)[:_CRASH_MESSAGE_LIMIT]}"
        finding = Finding(CRASH_FINDING_CODE, detail, "run", stage=self._active_stage)
        try:
            self._finish("failed", [finding])
        except Exception:  # noqa: BLE001 - the original exception is the one to surface
            pass

    def _run(
        self,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> AuthoringResult:
        policy = self.policy
        self._allowances = {
            "plan": policy.plan_max_corrections,
            "artifact": policy.artifact_max_corrections,
        }
        self._review_revision_allowances = {
            "plan": REVIEW_REVISION_ALLOWANCE_PER_STAGE if policy.review_plan else 0,
            "artifact": REVIEW_REVISION_ALLOWANCE_PER_STAGE if policy.review_artifact else 0,
        }
        self._record_allowances()
        self._review_status = {"plan": "not_requested", "artifact": "not_requested"}
        self._journal.append(
            PolicyRecorded(self._effective_policy_record()),
            ReviewStatusRecorded(dict(self._review_status)),
        )
        self._journal.flush()
        unusable = shape_refusal_findings(view, runtime_contract, inventory) or (
            _command_attempt_condition_findings(view, _handoff_claim_level(view), inventory)
        )
        if unusable:
            return self._result("failed", None, unusable)
        plan = self._run_stage(
            self._plan_stage(view, inventory, runtime_contract), view, inventory, runtime_contract
        )
        if isinstance(plan, _StageStop):
            return self._result(plan.status, None, plan.findings)
        unusable = _command_attempt_condition_findings(view, _plan_claim_level(plan), inventory)
        if unusable:
            self._record_failures(unusable)
            return self._result("failed", plan, unusable)
        artifact = self._run_stage(
            self._artifact_stage(plan, view, inventory, runtime_contract),
            view,
            inventory,
            runtime_contract,
        )
        if isinstance(artifact, _StageStop):
            return self._result(artifact.status, plan, artifact.findings)
        return self._accept_package(view, plan, artifact, inventory, runtime_contract)

    def _accept_package(
        self,
        view: InputView,
        plan: dict[str, Any],
        artifact: dict[str, Any],
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> AuthoringResult:
        """Assemble and write the accepted package, or fail with the finding."""

        try:
            package = _package_from_responses(
                view=view,
                plan=plan,
                artifact={
                    **artifact,
                    # Copied from the accepted plan; Call 2 and its corrections
                    # never rewrite them.
                    "setup_recipe": plan["setup_recipe"],
                    "runtime_bindings": plan["runtime_bindings"],
                    "prerequisites": plan["prerequisites"],
                    "required_observations": plan["required_observations"],
                },
                task_id=self.task_id,
                ledger=self._journal.ledger,
                raw_responses=self._raw_responses,
                prompt_packets=self._prompt_packets,
                transformations=self._transformations,
                inventory=inventory,
                runtime_contract=runtime_contract,
                discovery_provenance=self.discovery_provenance,
                policy=self._effective_policy_record(),
                review_status=dict(self._review_status),
                preserved_reviews=self._journal.reviews,
                terminal_status="accepted",
                budget=self.budget.snapshot(self.task_id),
            )
        except ArtifactValidationError as exc:
            return self._result(
                "failed",
                plan,
                [Finding("assembly_validation", exc.message, exc.path, stage="artifact")],
            )
        try:
            path = write_package(self.package_dir, package)
        except Exception as exc:
            finding = Finding("package_write_failed", str(exc), stage="artifact")
            return self._result("failed", plan, [finding])
        return self._result(
            "accepted", plan, [], artifact=artifact, package=package, package_path=path
        )

    def _plan_stage(
        self, view: InputView, inventory: dict[str, Any], runtime_contract: dict[str, Any]
    ) -> _Stage:
        def checks(decoded: Any) -> list[Finding]:
            return collect_plan_findings_v2(
                decoded,
                inventory,
                runtime_contract,
                provenance_ids=scenario_provenance_ids(view),
                condition=view.payload.get("discriminating_condition"),
                transformations=self._transformations,
            ) + sequential_plan_findings(view, decoded)

        return _Stage(
            key="plan",
            author="call1",
            author_packet=lambda: build_call1_packet_v2(view, inventory, runtime_contract),
            checks=checks,
            missing=_staged(Finding("call1_failed", "Call 1 did not return a plan", "call1")),
            review_packet=(
                (
                    lambda candidate, prior, findings: build_plan_review_packet(
                        view,
                        candidate,
                        inventory,
                        runtime_contract,
                        prior_round=prior,
                        check_findings=findings,
                    )
                )
                if self.policy.review_plan
                else None
            ),
            review_blocked_status="blocked",
            may_block=True,
        )

    def _artifact_stage(
        self,
        plan: dict[str, Any],
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> _Stage:
        def checks(decoded: Any) -> list[Finding]:
            findings = collect_artifact_findings_v2(
                decoded,
                plan,
                inventory,
                runtime_contract,
                transformations=self._transformations,
                condition=view.tool_call_condition,
            ) + sequential_artifact_findings(view, decoded)
            return findings + oracle_defect_findings(findings, decoded, view.tool_call_condition)

        return _Stage(
            key="artifact",
            author="call2",
            author_packet=lambda: build_call2_packet_v2(view, plan, inventory, runtime_contract),
            checks=checks,
            missing=_staged(
                Finding("call2_failed", "Call 2 did not return a valid artifact", "call2")
            ),
            review_packet=(
                (
                    lambda candidate, _prior, findings: build_artifact_review_packet(
                        view, plan, candidate, inventory, runtime_contract, check_findings=findings
                    )
                )
                if self.policy.review_artifact
                else None
            ),
            # The accepted plan itself must change; never recurse back into
            # plan authoring.
            review_blocked_status="needs_plan_revision",
        )

    def _run_stage(
        self,
        stage: _Stage,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> dict[str, Any] | _StageStop:
        """Author, check, correct, and review one stage within its allowances."""

        self._active_stage = stage.key
        try:
            packet = stage.author_packet()
        except PromptPreflightError as exc:
            return _preflight_stop(exc, stage.author)
        candidate, pending, raw = self._author(packet, stage.checks)
        if any(finding.code == "prompt_preflight" for finding in pending):
            return _StageStop("failed", tuple(pending))
        review_driven = False
        prior_round: PriorReviewRound | None = None
        while True:
            while candidate is None:
                corrected = self._correction_round(
                    stage,
                    pending,
                    raw,
                    review_driven=review_driven,
                    view=view,
                    inventory=inventory,
                    runtime_contract=runtime_contract,
                )
                if isinstance(corrected, _StageStop):
                    return corrected
                review_driven = False
                candidate, pending, raw = corrected
            reviewed = self._review_candidate(stage, candidate, pending, prior_round)
            if not isinstance(reviewed, _Revision):
                return reviewed
            # Semantic revise findings join the stage correction path but spend
            # the stage's separate review-revision allowance.
            pending = list(reviewed.findings)
            prior_round = PriorReviewRound(reviewed.records, candidate)
            review_driven = True
            candidate = None

    def _correction_round(
        self,
        stage: _Stage,
        pending: Sequence[Finding],
        raw: bytes,
        *,
        review_driven: bool,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[Finding], bytes] | _StageStop:
        """Spend one stage allowance on a correction of the pending findings.

        The returned candidate is None while findings remain.
        """

        pending = pending or [stage.missing]
        if stage.may_block:
            blocked = self._decoded_responses.get(stage.author)
            if _is_blocked_plan(blocked):
                _persist_blocked_plan(self.package_dir, blocked)
                return _StageStop("blocked")
        stop = self._stop_for_author_findings(list(pending))
        if stop is not None:
            return stop
        if not self._consume_allowance(stage.key, review_revision=review_driven):
            return _StageStop(
                "unresolved",
                (
                    *pending,
                    self._allowance_exhausted_finding(stage.key, review_revision=review_driven),
                ),
            )
        self._record_allowances()
        corrected = self._correction_v2(
            stage,
            failed_response=raw,
            findings=list(pending),
            view=view,
            inventory=inventory,
            runtime_contract=runtime_contract,
            allowance_kind="review_revision" if review_driven else "correction",
        )
        if corrected is None:
            return self._failed_correction_stop(pending)
        return corrected

    def _failed_correction_stop(self, pending: Sequence[Finding]) -> _StageStop:
        """Stop the stage after a correction that returned no candidate."""

        stop = self._stop_for_author_findings(list(self._findings))
        if stop is not None:
            return stop
        return _StageStop("unresolved", tuple(self._findings or pending))

    def _review_candidate(
        self,
        stage: _Stage,
        candidate: dict[str, Any],
        check_findings: Sequence[Finding],
        prior: PriorReviewRound | None = None,
    ) -> dict[str, Any] | _StageStop | _Revision:
        """Review a checked candidate.

        Return the accepted candidate, the stage stop, or the revise findings
        that send the candidate back through correction.
        """

        if stage.may_block and _is_blocked_plan(candidate):
            _persist_blocked_plan(self.package_dir, candidate)
            return _StageStop("blocked")
        if stage.review_packet is None:
            self._review_status[stage.key] = "not_requested"
            return candidate
        try:
            review_packet = stage.review_packet(candidate, prior, check_findings)
        except PromptPreflightError as exc:
            return _preflight_stop(exc, stage.review_stage)
        outcome = self._semantic_review(stage.key, review_packet)
        if outcome.stop is not None:
            return outcome.stop
        if outcome.decision == "accept":
            self._review_status[stage.key] = "accepted"
            return candidate
        findings = self._semantic_finding_objects(outcome, stage.key)
        if outcome.decision == "blocked":
            self._review_status[stage.key] = "blocked"
            return _StageStop(stage.review_blocked_status, findings)
        self._review_status[stage.key] = "revise"
        return _Revision(findings, outcome.findings)

    def _semantic_review(self, review_key: str, packet: PromptPacket) -> _ReviewOutcome:
        """Dispatch one semantic review and classify its closed outcome."""

        assert self._review_status is not None
        started = time.monotonic()
        try:
            response = self._dispatch(packet)
        except Exception as exc:
            finding = self._record_dispatch_failure(exc, packet, started)
            status = (
                finding.code
                if finding.code in {"prompt_overflow", "budget_exhausted"}
                else "review_unavailable"
            )
            self._review_status[review_key] = (
                "prompt_overflow" if status == "prompt_overflow" else "unavailable"
            )
            return _ReviewOutcome(decision="", stop=_StageStop(status, (finding,)))
        raw, effective_controls = self._record_review_response(packet, response)
        try:
            review = parse_review_response(raw)
        except ReviewResponseError as exc:
            stop = self._review_parse_stop(exc, packet, effective_controls)
            self._review_status[review_key] = "unavailable"
            return _ReviewOutcome(decision="", stop=stop)
        decision_after_scope_filter, in_scope_findings, out_of_scope_findings = (
            _scope_review_response(review, stage=packet.stage)
        )
        review_record = {
            "decision": review.decision,
            "original_decision": review.decision,
            "decision_after_scope_filter": decision_after_scope_filter,
            "summary": review.summary,
            "findings": [dict(item) for item in in_scope_findings],
            "out_of_scope_findings": [dict(item) for item in out_of_scope_findings],
            "question_ids": list(_review_question_ids(packet.stage)),
        }
        self._record_review_transformations(review, packet.stage)
        self._journal.append(ReviewDecided(review_record))
        self._set_review_evidence(
            status={
                "accept": "accepted",
                "revise": "revise",
                "blocked": "blocked",
            }[decision_after_scope_filter],
            effective_controls=effective_controls,
            packet=packet,
            review=review_record,
        )
        self._decoded_responses[packet.stage] = self._journal.reviews[review_key]
        self._journal.flush()
        return _ReviewOutcome(
            decision=decision_after_scope_filter,
            findings=in_scope_findings,
            raw=raw,
        )

    def _record_unavailable_review_dispatch(self, exc: Exception, packet: PromptPacket) -> None:
        """Annotate a dispatched review that raised with its error and controls."""

        effective_controls = self._review_controls(self._journal.dispatch_controls())
        self._journal.append(
            DispatchErrored(_safe_error(exc)), ReviewControlsRecorded(effective_controls)
        )
        self._set_review_evidence(
            status="unavailable",
            effective_controls=effective_controls,
            packet=packet,
        )

    def _record_review_response(
        self, packet: PromptPacket, response: TransportResponse | str | bytes
    ) -> tuple[bytes, dict[str, Any]]:
        """Store a review response as pending evidence.

        Return the raw bytes and the effective controls.
        """

        raw, _usage, controls, _response_capture = _response_parts(response)
        self._raw_responses[packet.stage] = raw
        effective_controls = self._review_controls(controls)
        self._journal.append(ReviewControlsRecorded(effective_controls))
        self._set_review_evidence(
            status="pending",
            effective_controls=effective_controls,
            packet=packet,
        )
        self._journal.flush()
        return raw, effective_controls

    def _review_parse_stop(
        self,
        exc: ReviewResponseError,
        packet: PromptPacket,
        effective_controls: dict[str, Any],
    ) -> _StageStop:
        """Record a review response that failed to parse and return its stop."""

        finding = _staged(Finding("review_unavailable", _safe_error(exc), packet.stage))
        self._findings.append(finding)
        self._journal.append(LedgerFindingsRecorded("review_error", tuple(exc.findings)))
        self._set_review_evidence(
            status="unavailable",
            effective_controls=effective_controls,
            packet=packet,
        )
        self._record_failures(staged_findings(exc.findings, FINDING_STAGE_KEYS[packet.stage]))
        failure = {
            "phase": "post_response",
            "code": finding.code,
            "detail": finding.detail,
        }
        self._journal.append(ReviewFailed(failure), RunFindingRecorded(finding))
        self._journal.flush()
        return _StageStop("review_unavailable", (finding,))

    def _consume_allowance(self, stage: str, *, review_revision: bool = False) -> bool:
        """Spend one correction from the named stage's independent allowance.

        A revision requested by a semantic review spends the stage's separate
        review-revision allowance; every other correction spends the stage
        correction allowance.
        """

        allowances = self._review_revision_allowances if review_revision else self._allowances
        assert allowances is not None
        if allowances[stage] <= 0:
            return False
        allowances[stage] -= 1
        return True

    def _allowance_exhausted_finding(self, stage: str, *, review_revision: bool) -> Finding:
        kind = "review revision" if review_revision else "correction"
        return _staged(
            Finding("correction_limit_exhausted", f"{stage} {kind} allowance is exhausted", stage)
        )

    def _record_allowances(self) -> None:
        self._journal.append(
            AllowancesRecorded(
                dict(self._allowances) if self._allowances is not None else None,
                (
                    dict(self._review_revision_allowances)
                    if self._review_revision_allowances is not None
                    else None
                ),
            )
        )

    @staticmethod
    def _stop_for_author_findings(findings: list[Finding]) -> _StageStop | None:
        """Return the terminal run stop for transport, budget, or oracle failures."""

        if any(finding.code == DEFECT_CODE for finding in findings):
            # A correction keeps the plan and the producer's condition fixed, so
            # no new example can change what the condition decides.
            return _StageStop("failed", tuple(findings))
        stop_findings = [
            finding
            for finding in findings
            if finding.code
            in {
                "transport_failure",
                "budget_exhausted",
                "correction_dispatch_failed",
                "prompt_overflow",
            }
        ]
        if not stop_findings:
            return None
        for code in ("prompt_overflow", "budget_exhausted"):
            matching = tuple(finding for finding in stop_findings if finding.code == code)
            if matching:
                return _StageStop(code, matching)
        return _StageStop("transport_failure", tuple(stop_findings))

    @staticmethod
    def _semantic_finding_objects(
        outcome: _ReviewOutcome,
        stage: str,
    ) -> tuple[Finding, ...]:
        """Convert complete reviewer findings into typed stage findings."""

        return tuple(_review_finding_to_finding(item, stage) for item in outcome.findings)

    def _effective_policy_record(self) -> dict[str, Any]:
        """Return the effective stage policy and reviewer controls record."""

        return dict(self._policy_record)

    def _build_policy_record(self) -> dict[str, Any]:
        policy = self.policy
        record: dict[str, Any] = {
            "plan_max_corrections": policy.plan_max_corrections,
            "artifact_max_corrections": policy.artifact_max_corrections,
            "plan_max_review_revisions": (
                REVIEW_REVISION_ALLOWANCE_PER_STAGE if policy.review_plan else 0
            ),
            "artifact_max_review_revisions": (
                REVIEW_REVISION_ALLOWANCE_PER_STAGE if policy.review_artifact else 0
            ),
            "review_plan": policy.review_plan,
            "review_artifact": policy.review_artifact,
            "review_model_profile": self.review_model_profile,
            "max_retries": 0,
        }
        if getattr(self.transport, "sampling_controls", True):
            record["review_temperature"] = 0
        else:
            record["sampling_controls"] = False
        return record

    def _review_controls(self, controls: Any) -> dict[str, Any]:
        """Return redacted effective reviewer controls for durable evidence."""

        return {**self._base_review_controls, **_safe_metadata(controls), "max_retries": 0}

    def _build_base_review_controls(self) -> dict[str, Any]:
        controls: dict[str, Any] = {
            "review_model_profile": self.review_model_profile,
            "max_retries": 0,
        }
        if getattr(self.transport, "sampling_controls", True):
            controls["temperature"] = 0
        else:
            controls["sampling_controls"] = False
        model = getattr(self.transport, "model", None)
        if isinstance(model, str) and model.strip():
            controls["model"] = model
        return controls

    def _set_review_evidence(
        self,
        *,
        status: str,
        effective_controls: dict[str, Any],
        packet: PromptPacket,
        review: dict[str, Any] | None = None,
    ) -> None:
        """Record the durable review evidence of the open review dispatch."""

        input_digest, candidate_digest = _review_packet_digests(packet)
        response = self._journal.latest_response()
        self._journal.append(
            ReviewEvidenceRecorded(
                review_stage="plan" if packet.stage == "plan_review" else "artifact",
                status=status,
                effective_controls=effective_controls,
                prompt_version=packet.version,
                prompt_sha256=packet.sha256,
                input_sha256=input_digest,
                candidate_sha256=candidate_digest,
                contract_sha256=_review_contract_digest(packet),
                configuration_sha256=_review_configuration_digest(
                    effective_controls,
                    self._effective_policy_record(),
                ),
                raw_response_key=response.raw_response_key if response else None,
                raw=response.raw if response else None,
                review=review,
            )
        )

    def _result(
        self,
        status: str,
        plan: dict[str, Any] | None,
        findings: Sequence[Finding],
        *,
        artifact: dict[str, Any] | None = None,
        package: Any = None,
        package_path: Path | None = None,
    ) -> AuthoringResult:
        """Close the run with ``status`` and return its result."""

        assert self._review_status is not None
        assert self._allowances is not None
        assert self._review_revision_allowances is not None
        self._journal.append(ReviewStatusRecorded(dict(self._review_status)))
        self._record_allowances()
        if any(finding.code == "prompt_overflow" for finding in findings):
            status = "prompt_overflow"
        failure_evidence_path = self._finish(status, list(findings))
        return AuthoringResult(
            status=status,
            task_id=self.task_id,
            plan=plan,
            artifact=artifact,
            package=package,
            package_path=package_path,
            findings=list(findings),
            ledger=list(self._journal.ledger),
            transformations=list(self._transformations),
            raw_responses=dict(self._raw_responses),
            failure_evidence_path=None if status == "accepted" else failure_evidence_path,
            review_status=dict(self._review_status),
            allowances=dict(self._allowances),
            review_revision_allowances=dict(self._review_revision_allowances),
            budget=self.budget.snapshot(self.task_id),
        )

    def _latest_attempt_findings(self, fallback: list[Finding]) -> list[Finding]:
        """Return only the findings from the response that just terminated."""

        return self._journal.latest_attempt_findings() or list(fallback)

    def _record_candidate_digest(self, candidate: dict[str, Any]) -> None:
        """Pin the normalized candidate bytes to the current dispatch event."""

        self._journal.append(
            CandidateDigested(_sha256(_canonical_json(candidate).encode("utf-8")))
        )

    def _record_transformation(self, transformation: Any) -> None:
        self._transformations.append(transformation)
        self._journal.append(TransformationRecorded(transformation, tuple(self._transformations)))

    def _record_review_transformations(self, review: ReviewResponse, stage: str) -> None:
        """Record the fence removal and any defaulted ``findings`` of one parsed review.

        The fence name takes the attempt's single ``transformation`` slot; the
        defaulted field is a detailed record, so it goes to the attempt's
        ``transformations`` list beside the other deterministic rewrites.
        """

        if review.transformation:
            self._record_transformation(review.transformation)
        if review.findings_omitted:
            start = len(self._transformations)
            self._transformations.append(
                {
                    "transformation": REVIEW_FINDINGS_OMITTED,
                    "stage": stage,
                    "field": "findings",
                    "default": [],
                }
            )
            self._record_validation_transformations(start)

    def _record_validation_transformations(self, start: int) -> None:
        """Persist deterministic binding rewrites made during validation."""

        changes = self._transformations[start:]
        if not changes:
            return
        self._journal.append(ValidationTransformed(tuple(changes), tuple(self._transformations)))
        self._journal.flush()

    def _record_unavailable_response(
        self,
        *,
        reason: str,
        detail: str,
        finding: Finding,
        elapsed_ms: float | None = None,
    ) -> None:
        self._journal.append(ResponseUnavailable(reason, _safe_error(detail), elapsed_ms))
        self._record_failures([finding])

    def _record_run_finding(self, finding: Finding) -> None:
        """Record a finding of a request that opened no dispatch."""

        self._journal.append(RunFindingRecorded(finding))
        self._journal.flush()

    def _record_failures(self, findings: Sequence[Finding]) -> None:
        self._journal.append(AttemptFailed(tuple(findings)))
        self._journal.flush()

    @staticmethod
    def _checks_skipped(checks: list[str]) -> ChecksSkipped:
        """Return the event for downstream checks skipped after response framing failed."""

        return ChecksSkipped(tuple(dict.fromkeys(checks)))

    def _finish(self, status: str, findings: list[Finding]) -> Path | None:
        """Close the journal with the terminal status; return the sidecar path if written."""

        attempts = len(self._journal.dispatches())
        if not attempts and not findings:
            return None
        terminal_findings = self._terminal_findings(status, findings)
        self._journal.append(
            Finished(
                status,
                tuple(terminal_findings),
                {
                    "stage": self._terminal_stage(terminal_findings),
                    "attempt_index": attempts - 1 if attempts else None,
                    "reason": terminal_findings[-1].code if terminal_findings else status,
                },
            )
        )
        return self._journal.flush()

    def _terminal_findings(self, status: str, findings: list[Finding]) -> list[Finding]:
        """Return the findings the terminal record carries for ``status``."""

        if status == "accepted":
            return []
        crashed = [finding for finding in findings if finding.code == CRASH_FINDING_CODE]
        if crashed:
            return crashed
        if status == "prompt_overflow":
            # A correction overflow stops a request that never dispatched, so the
            # latest attempt's findings belong to the response before it.
            overflow = [finding for finding in findings if finding.code == "prompt_overflow"]
            return overflow or list(findings)
        return self._latest_attempt_findings(findings)

    def _terminal_stage(self, findings: list[Finding]) -> str | None:
        """Return the logical stage that produced the terminal outcome."""

        for finding in reversed(findings):
            if finding.stage is not None:
                return finding.stage
        dispatches = self._journal.dispatches()
        if dispatches:
            return _attempt_terminal_stage(
                dispatches[-1].packet.stage, self._journal.latest_failed_stage()
            )
        return None
