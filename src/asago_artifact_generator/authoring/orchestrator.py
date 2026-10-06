"""The authoring orchestrator: runs plan, artifact, review, and correction stages
within the policy's request allowance and writes the package or failure evidence.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from .context_budget import _enforce_prompt_size
from .core import (
    _REVIEW_STAGES,
    MAX_AUTHOR_CORRECTION_REQUESTS_PER_TASK,
    MAX_AUTHORING_REQUESTS,
    MAX_RENDERED_PROMPT_BYTES,
    MAX_REVIEW_REQUESTS_PER_TASK,
    REVIEW_REVISION_ALLOWANCE_PER_STAGE,
    ArtifactValidationError,
    AuthoringError,
    AuthoringTransport,
    BudgetExceeded,
    Call1FramingError,
    Call2FramingError,
    Finding,
    PromptOverflowError,
    PromptPacket,
    PromptPreflightError,
    ReviewResponseError,
    TransportResponse,
    _canonical_json,
    _prompt_overflow_finding,
    _prompt_preflight_finding,
    _safe_error,
    _safe_metadata,
    _sha256,
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
    ValidationPassed,
    ValidationTransformed,
)
from .package_assembly import _package_from_responses, _persist_blocked_plan
from .policy import (
    AuthoringBudget,
    AuthoringPolicy,
    AuthoringResult,
    _validate_nonnegative_integer,
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
    _decode_call2_json_response,
    _decode_v2_json_response,
    _response_parts,
)
from .review import (
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


def _finding_from_record(item: Any) -> Finding | None:
    """Rebuild a finding from its recorded form, or None when the record is malformed."""

    if not isinstance(item, dict):
        return None
    code = item.get("code")
    detail = item.get("detail")
    path = item.get("path", "")
    if not (isinstance(code, str) and isinstance(detail, str)):
        return None
    return Finding(
        code,
        detail,
        path if isinstance(path, str) else "",
        item.get("details", {}) if isinstance(item.get("details"), dict) else {},
    )


def _decode_stage_json(stage: str, raw: bytes) -> tuple[dict[str, Any], str | None]:
    if stage == "call2":
        return _decode_call2_json_response(raw)
    return _decode_v2_json_response(raw)


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
    return Finding("tool_call_condition_missing", text, "tool_call_condition_status", details)


def _command_attempt_condition_missing(view: InputView, claim_level: Any) -> Finding | None:
    """Return the missing-condition finding when a command-attempt claim has no bound condition."""

    return _tool_call_condition_missing(view) if claim_level == "command_attempt" else None


_FINDING_PATH_STAGES = {
    "tool_call_condition_status": "plan",
    "call1": "plan",
    "plan": "plan",
    "plan_review": "plan",
    "call2": "artifact",
    "artifact": "artifact",
    "artifact_review": "artifact",
}
_ATTEMPT_STAGES = {
    "call1": "plan",
    "plan_review": "plan",
    "call2": "artifact",
    "artifact_review": "artifact",
}


def _attempt_terminal_stage(attempt: dict[str, Any]) -> str | None:
    stage = attempt.get("stage")
    if stage in _ATTEMPT_STAGES:
        return _ATTEMPT_STAGES[stage]
    if stage == "correction":
        return "plan" if attempt.get("failed_stage") == "call1" else "artifact"
    return None


# Returned by ``_dispatch_correction`` when no correction response exists.
_CORRECTION_NOT_DISPATCHED = object()


@dataclass(frozen=True)
class _ArtifactRevision:
    """Semantic review findings that send the artifact back for a revision."""

    findings: tuple[Finding, ...]


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
        prior_author_correction_spend: int = 0,
        prior_review_spend: int = 0,
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
        if budget is None:
            budget = _default_budget(policy)
        _validate_nonnegative_integer(
            "prior_author_correction_spend",
            prior_author_correction_spend,
        )
        _validate_nonnegative_integer("prior_review_spend", prior_review_spend)
        if prior_author_correction_spend or prior_review_spend:
            budget.seed_prior_spend(
                task_id=task_id,
                prior_author_correction_spend=prior_author_correction_spend,
                prior_review_spend=prior_review_spend,
            )
        self.budget = budget
        self._journal = AuthoringJournal(task_id, self.package_dir)
        self._journal.append(BudgetRecorded(self.budget.snapshot(self.task_id)))
        self._findings: list[Finding] = []
        self._dispatch_recorded = True
        self._allowances: dict[str, int] | None = None
        self._review_revision_allowances: dict[str, int] | None = None
        self._review_status: dict[str, str] | None = None
        self._raw_responses: dict[str, bytes] = {}
        self._decoded_responses: dict[str, Any] = {}
        self._prompt_packets: dict[str, PromptPacket] = {}
        self._transformations: list[Any] = []

    def run(
        self,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> AuthoringResult:
        return self._run_v2_policy(view, inventory, runtime_contract)

    def _request_and_validate_v2(
        self,
        packet: PromptPacket,
        findings_collector: Any,
    ) -> tuple[dict[str, Any] | None, list[Finding], bytes]:
        """Dispatch and validate one v2 stage without changing response bytes."""

        stage = packet.stage
        self._prompt_packets[stage] = packet
        started = time.monotonic()
        try:
            response = self._dispatch(packet)
        except Exception as exc:
            return None, [self._record_v2_dispatch_failure(exc, stage, started)], b""
        raw = self._record_v2_response(stage, response)
        try:
            validation_value = self._decode_v2_stage(stage, raw)
        except (
            Call1FramingError,
            Call2FramingError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            return None, self._record_v2_decode_failure(exc, stage), raw
        rejection = self._v2_response_rejection(stage, validation_value)
        if rejection is not None:
            self._findings.append(rejection)
            self._record_failures([rejection])
            return None, [rejection], raw
        transformation_count = len(self._transformations)
        findings = findings_collector(validation_value)
        self._record_validation_transformations(transformation_count)
        if findings:
            self._findings.extend(findings)
            self._journal.append(LedgerFindingsRecorded("findings", tuple(findings)))
            self._record_failures(findings)
            return None, findings, raw
        self._journal.append(ValidationPassed())
        self._journal.flush()
        return validation_value, [], raw

    def _record_v2_dispatch_failure(self, exc: Exception, stage: str, started: float) -> Finding:
        """Record a v2 stage request that returned no response and return its finding."""

        if isinstance(exc, PromptOverflowError):
            finding = _prompt_overflow_finding(exc, stage)
            self._findings.append(finding)
            self._record_run_finding(finding)
            return finding
        if isinstance(exc, BudgetExceeded):
            # Budget stops happen before dispatch: no ledger record exists to
            # annotate, and no stage attempt was created for this request.
            finding = Finding("budget_exhausted", _safe_error(exc), stage)
            self._findings.append(finding)
            self._record_run_finding(finding)
            return finding
        finding = Finding("transport_failure", _safe_error(exc), stage)
        self._findings.append(finding)
        if self._dispatch_recorded:
            self._journal.append(DispatchErrored(_safe_error(exc)))
        self._record_unavailable_response(
            reason="provider_failure",
            detail=_safe_error(exc),
            finding=finding,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
        return finding

    def _record_v2_response(self, stage: str, response: TransportResponse | str | bytes) -> bytes:
        """Keep a v2 stage response under its stage name; return the raw bytes.

        ``_dispatch`` already journaled the response on its dispatch.
        """

        raw = _response_parts(response)[0]
        self._raw_responses[stage] = raw
        self._journal.flush()
        return raw

    def _decode_v2_stage(self, stage: str, raw: bytes) -> dict[str, Any]:
        """Decode one v2 stage response and record the decoded output."""

        decoded_json, transformation = _decode_stage_json(stage, raw)
        if transformation:
            self._record_transformation(transformation)
        self._decoded_responses[stage] = decoded_json
        self._journal.append(DecodedOutputRecorded(decoded_json, on_ledger=True))
        if isinstance(decoded_json, dict):
            self._record_candidate_digest(decoded_json)
        return decoded_json

    def _record_v2_decode_failure(self, exc: Exception, stage: str) -> list[Finding]:
        """Record a framing or parse failure for a v2 stage and return its findings."""

        checks_not_run = _correction_checks_not_run(stage)
        if isinstance(exc, (Call1FramingError, Call2FramingError)):
            self._findings.extend(exc.findings)
            self._journal.append(
                LedgerFindingsRecorded("framing_findings", tuple(exc.findings)),
                self._checks_skipped(checks_not_run),
            )
            self._record_failures(exc.findings)
            return exc.findings
        finding = Finding("response_parse_error", str(exc), stage)
        self._journal.append(ParseFailed(str(exc)), self._checks_skipped(checks_not_run))
        self._findings.append(finding)
        self._record_failures([finding])
        return [finding]

    @staticmethod
    def _v2_response_rejection(stage: str, validation_value: dict[str, Any]) -> Finding | None:
        """Return the finding that rejects a decoded value before its stage checks run."""

        try:
            assert_no_secrets(validation_value)
        except AuthoringError as exc:
            return Finding("secret_in_response", str(exc), stage)
        if stage == "call1" and not isinstance(validation_value, dict):
            return Finding("response_type_error", "plan must decode to an object", stage)
        return None

    def _dispatch(self, packet: PromptPacket) -> TransportResponse | str | bytes:
        # Reject an over-budget request before reserving an author/reviewer slot.
        self._dispatch_recorded = False
        context_preflight = getattr(self.transport, "preflight_context_budget", None)
        if callable(context_preflight):
            context_preflight(packet)
        # Reserve before creating any ledger or evidence record: a budget stop
        # happens before dispatch, so it leaves no dispatch event behind.
        role = "reviewer" if packet.stage in _REVIEW_STAGES else "author"
        try:
            self.budget.reserve(self.task_id, role=role)
        except BudgetExceeded:
            self._dispatch_recorded = False
            raise
        self._dispatch_recorded = True
        dispatch_index = len(self._journal.ledger) + 1
        self._journal.append(
            BudgetRecorded(self.budget.snapshot(self.task_id)),
            self._dispatch_opened(packet, dispatch_index, role),
        )
        self._journal.flush()
        try:
            response = self.transport.complete(packet)
        except Exception:
            self._record_transport_failure_controls()
            raise
        self._record_dispatch_response(dispatch_index, response)
        self._journal.flush()
        return response

    def _dispatch_attempt_indices(self, packet: PromptPacket) -> tuple[int, int]:
        """Return the stage attempt index and the per-failed-stage correction index."""

        stage_attempt_index = (
            sum(1 for prior in self._journal.ledger if prior.get("stage") == packet.stage) + 1
        )
        if packet.stage != "correction":
            return stage_attempt_index, 0
        failed_stage = (
            packet.payload.get("failed_stage") if isinstance(packet.payload, dict) else None
        )
        correction_index = (
            sum(
                1
                for prior in self._journal.ledger
                if prior.get("stage") == "correction" and prior.get("failed_stage") == failed_stage
            )
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
        *,
        failed_stage: str,
        failed_packet: PromptPacket,
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

        packet = self._correction_packet(
            failed_stage=failed_stage,
            failed_packet=failed_packet,
            failed_response=failed_response,
            findings=findings,
            view=view,
            inventory=inventory,
            runtime_contract=runtime_contract,
        )
        if packet is None:
            return None
        self._prompt_packets["correction"] = packet
        response = self._dispatch_correction(packet, failed_stage)
        if response is _CORRECTION_NOT_DISPATCHED:
            return None
        raw = self._record_correction_response(
            response,
            failed_stage=failed_stage,
            failed_response=failed_response,
            allowance_kind=allowance_kind,
        )
        validated = self._validate_correction_response(
            raw, failed_stage, view, inventory, runtime_contract
        )
        if validated is None:
            return None
        validation_value, decoded, replacement_findings = validated
        if replacement_findings:
            return validation_value, replacement_findings, raw
        self._journal.append(ValidationPassed())
        self._journal.flush()
        replacement_key = "call1" if failed_stage == "call1" else "call2"
        self._raw_responses[replacement_key] = raw
        self._decoded_responses[replacement_key] = decoded
        self._prompt_packets[replacement_key] = (
            build_call1_packet_v2(view, inventory, runtime_contract)
            if failed_stage == "call1"
            else build_call2_packet_v2(
                view,
                self._decoded_responses["call1"],
                inventory,
                runtime_contract,
            )
        )
        return validation_value, [], raw

    def _correction_packet(
        self,
        *,
        failed_stage: str,
        failed_packet: PromptPacket,
        failed_response: bytes,
        findings: list[Finding],
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> PromptPacket | None:
        """Render the correction prompt; record a preflight failure and return None."""

        original_context = (
            build_plan_author_context(view, inventory, runtime_contract)
            if failed_stage == "call1"
            else build_artifact_author_context(
                view,
                self._decoded_responses["call1"],
                inventory,
                runtime_contract,
            )
        )
        correction_payload = build_correction_context(
            failed_stage=failed_stage,
            original_context=original_context,
            current_output=failed_response,
            findings=findings,
        )
        # Preserve the generic compatibility members consumed by historical
        # offline evidence readers.  They are not rendered into the new
        # sectioned user context, so the candidate is still shown once.
        correction_payload.update(
            {
                "original_request": {
                    "system": failed_packet.system,
                    "payload": failed_packet.payload,
                },
                "failed_response": correction_payload["current_output"],
                "failed_response_encoding": correction_payload["current_output_encoding"],
            }
        )
        packet = _render_correction_packet(
            correction_payload,
            correction_repair_inputs(
                view, inventory, runtime_contract, plan=failed_stage == "call1"
            ),
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
            finding = _prompt_preflight_finding(exc, "correction")
            self._findings.append(finding)
            self._record_run_finding(finding)
            return None
        return packet

    def _dispatch_correction(self, packet: PromptPacket, failed_stage: str) -> Any:
        """Dispatch the correction; record a refused or failed request and return the sentinel."""

        started = time.monotonic()
        try:
            return self._dispatch(packet)
        except PromptOverflowError as exc:
            finding = _prompt_overflow_finding(exc, packet.stage)
            self._findings.append(finding)
            self._record_run_finding(finding)
            return _CORRECTION_NOT_DISPATCHED
        except BudgetExceeded as exc:
            # Budget stops happen before dispatch: no ledger record or stage
            # attempt exists for the refused correction request.
            finding = Finding("budget_exhausted", _safe_error(exc), failed_stage)
            self._findings.append(finding)
            self._record_run_finding(finding)
            return _CORRECTION_NOT_DISPATCHED
        except Exception as exc:
            finding = Finding("correction_dispatch_failed", _safe_error(exc), failed_stage)
            if self._dispatch_recorded:
                self._journal.append(DispatchErrored(_safe_error(exc)))
            self._findings.append(finding)
            self._record_unavailable_response(
                reason="provider_failure",
                detail=_safe_error(exc),
                finding=finding,
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
            return _CORRECTION_NOT_DISPATCHED

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

    def _decode_correction_response(self, raw: bytes, failed_stage: str) -> dict[str, Any]:
        """Return the decoded output of a correction response."""

        decoded, transformation = _decode_stage_json(failed_stage, raw)
        if transformation:
            self._record_transformation(transformation)
        return decoded

    def _validate_correction_response(
        self,
        raw: bytes,
        failed_stage: str,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> tuple[dict[str, Any], Any, list[Finding]] | None:
        """Decode and validate a correction; record a failure and return None.

        Returns the value to validate, the decoded output, and the recorded
        validation findings.
        """

        checks_not_run = _correction_checks_not_run(failed_stage)
        try:
            decoded = self._decode_correction_response(raw, failed_stage)
            assert_no_secrets(decoded)
            self._decoded_responses[f"correction-{failed_stage}"] = decoded
            self._journal.append(DecodedOutputRecorded(decoded, on_ledger=False))
            self._record_candidate_digest(decoded)
            self._journal.flush()
            if not isinstance(decoded, dict):
                raise ValueError("correction response must decode to an object")
            transformation_count = len(self._transformations)
            replacement_findings = self._correction_findings(
                failed_stage, decoded, view, inventory, runtime_contract
            )
            self._record_validation_transformations(transformation_count)
            if replacement_findings:
                self._journal.append(
                    LedgerFindingsRecorded("findings", tuple(replacement_findings))
                )
                self._findings.extend(replacement_findings)
                self._record_failures(replacement_findings)
        except (Call1FramingError, Call2FramingError) as exc:
            self._journal.append(
                LedgerFindingsRecorded("framing_findings", tuple(exc.findings)),
                self._checks_skipped(checks_not_run),
            )
            self._findings.extend(exc.findings)
            self._record_failures(exc.findings)
            return None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, AuthoringError) as exc:
            self._record_correction_failure(exc, failed_stage, checks_not_run)
            return None
        return decoded, decoded, replacement_findings

    def _correction_findings(
        self,
        failed_stage: str,
        decoded: dict[str, Any],
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> list[Finding]:
        """Run the checks of the stage a correction replaces."""

        if failed_stage == "call1":
            return collect_plan_findings_v2(
                decoded,
                inventory,
                runtime_contract,
                provenance_ids=scenario_provenance_ids(view),
                condition=view.payload.get("discriminating_condition"),
                transformations=self._transformations,
            )
        return collect_artifact_findings_v2(
            decoded,
            self._decoded_responses["call1"],
            inventory,
            runtime_contract,
            transformations=self._transformations,
        )

    def _record_correction_failure(
        self, exc: Exception, failed_stage: str, checks_not_run: list[str]
    ) -> None:
        finding = Finding("correction_failed", str(exc), failed_stage)
        self._journal.append(LedgerFindingsRecorded("findings", (finding,)))
        if isinstance(exc, (UnicodeDecodeError, json.JSONDecodeError)):
            self._journal.append(ParseFailed(str(exc)), self._checks_skipped(checks_not_run))
        self._findings.append(finding)
        self._record_failures([finding])

    def _run_v2_policy(
        self,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> AuthoringResult:
        """Run the stage-local correction and review state machine.

        Each stage owns its correction allowance; deterministic checks precede
        every semantic review; and review decisions
        route corrections without shared state or hidden retries.
        """

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
        missing = _command_attempt_condition_missing(view, _handoff_claim_level(view))
        if missing is not None:
            return self._policy_result("failed", None, [missing])
        plan = self._plan_stage_policy(view, inventory, runtime_contract)
        if isinstance(plan, _StageStop):
            return self._policy_result(plan.status, None, plan.findings)
        missing = _command_attempt_condition_missing(view, _plan_claim_level(plan))
        if missing is not None:
            self._record_failures([missing])
            return self._policy_result("failed", plan, [missing])
        artifact = self._artifact_stage_policy(view, plan, inventory, runtime_contract)
        if isinstance(artifact, _StageStop):
            return self._policy_result(artifact.status, plan, artifact.findings)
        metadata, artifact_definition = artifact
        return self._accept_package(
            view, plan, metadata, artifact_definition, inventory, runtime_contract
        )

    def _accept_package(
        self,
        view: InputView,
        plan: dict[str, Any],
        metadata: dict[str, Any],
        artifact_definition: dict[str, Any],
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> AuthoringResult:
        """Assemble and write the accepted package, or fail with the finding."""

        try:
            package = _package_from_responses(
                view=view,
                plan=plan,
                artifact=artifact_definition,
                task_id=self.task_id,
                ledger=self._journal.ledger,
                raw_responses=self._raw_responses,
                decoded_responses=self._decoded_responses,
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
            return self._policy_result(
                "failed",
                plan,
                [Finding("assembly_validation", exc.message, exc.path)],
            )
        try:
            path = write_package(self.package_dir, package)
        except Exception as exc:
            return self._policy_result("failed", plan, [Finding("package_write_failed", str(exc))])
        self._journal.append(ReviewStatusRecorded(dict(self._review_status)))
        self._record_allowances()
        self._finish("accepted", [])
        return AuthoringResult(
            status="accepted",
            task_id=self.task_id,
            plan=plan,
            artifact=metadata,
            package=package,
            package_path=path,
            findings=[],
            ledger=list(self._journal.ledger),
            transformations=list(self._transformations),
            raw_responses=dict(self._raw_responses),
            decoded_responses=dict(self._decoded_responses),
            prompts=dict(self._prompt_packets),
            review_status=dict(self._review_status),
            allowances=dict(self._allowances),
            review_revision_allowances=dict(self._review_revision_allowances or {}),
            failure_evidence_path=None,
            budget=self.budget.snapshot(self.task_id),
        )

    def _plan_stage_policy(
        self,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> dict[str, Any] | _StageStop:
        """Author, check, correct, and review the plan within its allowance."""

        try:
            packet = build_call1_packet_v2(view, inventory, runtime_contract)
        except PromptPreflightError as exc:
            return _preflight_stop(exc, "call1")
        collector = self._plan_findings_collector(view, inventory, runtime_contract)
        candidate: dict[str, Any] | None = None
        pending: Sequence[Finding] | None = None
        review_driven = False
        raw = b""
        while True:
            if candidate is None:
                if pending is None:
                    candidate, pending, raw = self._first_plan_candidate(packet, collector)
                if candidate is None:
                    corrected = self._plan_correction_round(
                        packet,
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
                    if pending is not None:
                        continue
            assert candidate is not None
            reviewed = self._review_plan_candidate(view, candidate, inventory, runtime_contract)
            if not isinstance(reviewed, tuple):
                return reviewed
            # Semantic revise findings join the stage correction path but spend
            # the stage's separate review-revision allowance.
            pending = reviewed
            review_driven = True
            candidate = None

    def _first_plan_candidate(
        self, packet: PromptPacket, collector: Any
    ) -> tuple[dict[str, Any] | None, Sequence[Finding], bytes]:
        decoded, pending, raw = self._request_and_validate_v2(packet, collector)
        return (decoded if isinstance(decoded, dict) else None), pending, raw

    def _plan_findings_collector(
        self,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> Any:
        """Return the plan checks bound to this scenario, inventory, and contract."""

        provenance_ids = scenario_provenance_ids(view)

        def collector(decoded: Any) -> list[Finding]:
            return collect_plan_findings_v2(
                decoded,
                inventory,
                runtime_contract,
                provenance_ids=provenance_ids,
                condition=view.payload.get("discriminating_condition"),
                transformations=self._transformations,
            )

        return collector

    def _plan_correction_round(
        self,
        packet: PromptPacket,
        pending: Sequence[Finding] | None,
        raw: bytes,
        *,
        review_driven: bool,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[Finding] | None, bytes] | _StageStop:
        """Spend one plan correction on the pending findings, or return the stage stop.

        The returned findings are ``None`` when the correction passed its checks;
        otherwise the candidate is ``None`` and the findings drive the next round.
        """

        pending = pending or [Finding("call1_failed", "Call 1 did not return a plan", "call1")]
        blocked = self._decoded_responses.get("call1")
        if _is_blocked_plan(blocked):
            _persist_blocked_plan(self.package_dir, blocked)
            return _StageStop("blocked")
        stop = self._stop_for_author_findings(pending)
        if stop is not None:
            return stop
        if not self._consume_allowance("plan", review_revision=review_driven):
            return _StageStop(
                "unresolved",
                (
                    *pending,
                    self._allowance_exhausted_finding("plan", review_revision=review_driven),
                ),
            )
        self._record_allowances()
        corrected = self._correction_v2(
            failed_stage="call1",
            failed_packet=packet,
            failed_response=raw,
            findings=list(pending),
            view=view,
            inventory=inventory,
            runtime_contract=runtime_contract,
            allowance_kind="review_revision" if review_driven else "correction",
        )
        if corrected is None:
            stop = self._stop_for_author_findings(list(self._findings))
            if stop is not None:
                return stop
            return _StageStop("unresolved", tuple(self._findings or pending))
        candidate, correction_findings, raw = corrected
        if correction_findings:
            return None, list(correction_findings), raw
        return candidate, None, raw

    def _review_plan_candidate(
        self,
        view: InputView,
        candidate: dict[str, Any],
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> dict[str, Any] | _StageStop | tuple[Finding, ...]:
        """Review a checked plan.

        Return the accepted plan, the stage stop, or the revise findings that
        send the plan back through correction.
        """

        if _is_blocked_plan(candidate):
            _persist_blocked_plan(self.package_dir, candidate)
            return _StageStop("blocked")
        if not self.policy.review_plan:
            self._review_status["plan"] = "not_requested"
            return candidate
        try:
            review_packet = build_plan_review_packet(view, candidate, inventory, runtime_contract)
        except PromptPreflightError as exc:
            return _preflight_stop(exc, "plan_review")
        outcome = self._semantic_review("plan", review_packet)
        if outcome.stop is not None:
            return outcome.stop
        if outcome.decision == "accept":
            self._review_status["plan"] = "accepted"
            return candidate
        if outcome.decision == "blocked":
            self._review_status["plan"] = "blocked"
            return _StageStop("blocked", self._semantic_finding_objects(outcome, "plan"))
        self._review_status["plan"] = "revise"
        return self._semantic_finding_objects(outcome, "plan")

    def _artifact_stage_policy(
        self,
        view: InputView,
        plan: dict[str, Any],
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]] | _StageStop:
        """Author, check, correct, and review the artifact stage."""

        try:
            packet = build_call2_packet_v2(view, plan, inventory, runtime_contract)
        except PromptPreflightError as exc:
            return _preflight_stop(exc, "call2")

        def collector(decoded: Any) -> list[Finding]:
            return collect_artifact_findings_v2(
                decoded,
                plan,
                inventory,
                runtime_contract,
                transformations=self._transformations,
            )

        parsed: dict[str, Any] | None = None
        pending: Sequence[Finding] | None = None
        review_driven = False
        raw = b""
        while True:
            if parsed is None:
                if pending is None:
                    parsed, pending, raw = self._request_and_validate_v2(packet, collector)
                if parsed is None:
                    step = self._correct_artifact_candidate(
                        view,
                        packet,
                        plan,
                        inventory,
                        runtime_contract,
                        pending=pending,
                        raw=raw,
                        review_driven=review_driven,
                    )
                    if isinstance(step, _StageStop):
                        return step
                    review_driven = False
                    parsed, pending, raw = step
                    if parsed is None:
                        continue
            review = self._review_artifact_candidate(
                view, plan, parsed, inventory, runtime_contract
            )
            if not isinstance(review, _ArtifactRevision):
                return review
            pending = review.findings
            review_driven = True
            parsed = None

    def _author_stop_or_unresolved(self, fallback: Sequence[Finding]) -> _StageStop:
        """Stop for a transport or budget finding, else report the recorded findings."""

        stop = self._stop_for_author_findings(list(self._findings))
        if stop is not None:
            return stop
        return _StageStop("unresolved", tuple(self._findings or fallback))

    def _correct_artifact_candidate(
        self,
        view: InputView,
        packet: PromptPacket,
        plan: dict[str, Any],
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
        *,
        pending: Sequence[Finding] | None,
        raw: bytes,
        review_driven: bool,
    ) -> tuple[dict[str, Any] | None, list[Finding], bytes] | _StageStop:
        """Spend one artifact allowance on a correction of the pending findings.

        The returned candidate is None while findings remain.
        """

        pending = pending or [
            Finding("call2_failed", "Call 2 did not return a valid artifact", "call2")
        ]
        stop = self._stop_for_author_findings(pending)
        if stop is not None:
            return stop
        if not self._consume_allowance("artifact", review_revision=review_driven):
            return _StageStop(
                "unresolved",
                (
                    *pending,
                    self._allowance_exhausted_finding("artifact", review_revision=review_driven),
                ),
            )
        self._record_allowances()
        allowance_kind = "review_revision" if review_driven else "correction"
        correction = self._correction_v2(
            failed_stage="call2",
            failed_packet=packet,
            failed_response=raw,
            findings=list(pending),
            view=view,
            inventory=inventory,
            runtime_contract=runtime_contract,
            allowance_kind=allowance_kind,
        )
        if correction is None:
            return self._author_stop_or_unresolved(pending)
        corrected_candidate, correction_findings, raw = correction
        if not isinstance(corrected_candidate, dict):
            return self._author_stop_or_unresolved(correction_findings)
        if correction_findings:
            return None, list(correction_findings), raw
        return corrected_candidate, [], raw

    def _review_artifact_candidate(
        self,
        view: InputView,
        plan: dict[str, Any],
        parsed: dict[str, Any],
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]] | _StageStop | _ArtifactRevision:
        """Accept a clean candidate, or review it and stop, accept, or ask for a revision."""

        if not self.policy.review_artifact:
            self._review_status["artifact"] = "not_requested"
            return self._artifact_parts(parsed, plan)
        try:
            review_packet = build_artifact_review_packet(
                view,
                plan,
                parsed,
                inventory,
                runtime_contract,
            )
        except PromptPreflightError as exc:
            finding = _prompt_preflight_finding(exc, "artifact_review")
            status = "prompt_overflow" if finding.code == "prompt_overflow" else "failed"
            return _StageStop(status, (finding,))
        outcome = self._semantic_review("artifact", review_packet)
        if outcome.stop is not None:
            return outcome.stop
        if outcome.decision == "accept":
            self._review_status["artifact"] = "accepted"
            return self._artifact_parts(parsed, plan)
        if outcome.decision == "blocked":
            # The accepted plan itself must change; never recurse back
            # into plan authoring.
            self._review_status["artifact"] = "blocked"
            return _StageStop(
                "needs_plan_revision",
                self._semantic_finding_objects(outcome, "artifact"),
            )
        # Semantic revise findings join the stage correction path but spend
        # the stage's separate review-revision allowance.
        self._review_status["artifact"] = "revise"
        return _ArtifactRevision(self._semantic_finding_objects(outcome, "artifact"))

    @staticmethod
    def _artifact_parts(
        metadata: dict[str, Any],
        plan: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Join the validated candidate with the accepted plan-owned fields."""

        artifact = {
            **metadata,
            # These values are copied from the accepted plan.  Call 2 and its
            # corrections never rewrite them.
            "setup_recipe": plan["setup_recipe"],
            "runtime_bindings": plan["runtime_bindings"],
            "prerequisites": plan["prerequisites"],
            "required_observations": plan["required_observations"],
        }
        return metadata, artifact

    def _semantic_review(self, review_key: str, packet: PromptPacket) -> _ReviewOutcome:
        """Dispatch one semantic review and classify its closed outcome."""

        assert self._review_status is not None
        started = time.monotonic()
        try:
            response = self._dispatch(packet)
        except Exception as exc:
            stop = self._review_dispatch_stop(exc, packet, started)
            self._review_status[review_key] = (
                "prompt_overflow" if stop.status == "prompt_overflow" else "unavailable"
            )
            return _ReviewOutcome(decision="", stop=stop)
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
        if review.transformation:
            self._record_transformation(review.transformation)
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
        self._decoded_responses[packet.stage] = self._journal.ledger[-1]["review"]
        self._journal.flush()
        return _ReviewOutcome(
            decision=decision_after_scope_filter,
            findings=in_scope_findings,
            raw=raw,
        )

    def _review_dispatch_stop(
        self, exc: Exception, packet: PromptPacket, started: float
    ) -> _StageStop:
        """Record a review request that returned no response and return its stop."""

        if isinstance(exc, PromptOverflowError):
            finding = _prompt_overflow_finding(exc, packet.stage)
            self._findings.append(finding)
            self._record_run_finding(finding)
            return _StageStop("prompt_overflow", (finding,))
        if isinstance(exc, BudgetExceeded):
            # Budget stops happen before dispatch: no ledger record or stage
            # attempt exists for the refused review request.
            finding = Finding("budget_exhausted", _safe_error(exc), packet.stage)
            self._findings.append(finding)
            self._record_run_finding(finding)
            return _StageStop("budget_exhausted", (finding,))
        finding = Finding("transport_failure", _safe_error(exc), packet.stage)
        self._findings.append(finding)
        if self._dispatch_recorded:
            self._record_unavailable_review_dispatch(exc, packet)
        self._record_unavailable_response(
            reason="provider_failure",
            detail=_safe_error(exc),
            finding=finding,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
        return _StageStop("review_unavailable", (finding,))

    def _record_unavailable_review_dispatch(self, exc: Exception, packet: PromptPacket) -> None:
        """Annotate a dispatched review that raised with its error and controls."""

        effective_controls = self._review_controls(self._journal.ledger[-1].get("controls"))
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

        finding = Finding("review_unavailable", _safe_error(exc), packet.stage)
        self._findings.append(finding)
        self._journal.append(LedgerFindingsRecorded("review_error", tuple(exc.findings)))
        self._set_review_evidence(
            status="unavailable",
            effective_controls=effective_controls,
            packet=packet,
        )
        self._record_failures(list(exc.findings))
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
        return Finding(
            "correction_limit_exhausted",
            f"{stage} {kind} allowance is exhausted",
            stage,
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
        """Return the terminal run stop for transport or budget failures."""

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

        policy = self.policy
        record = {
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

        effective = {
            "review_model_profile": self.review_model_profile,
            "max_retries": 0,
        }
        if getattr(self.transport, "sampling_controls", True):
            effective["temperature"] = 0
        else:
            effective["sampling_controls"] = False
        model = getattr(self.transport, "model", None)
        if isinstance(model, str) and model.strip():
            effective["model"] = model
        effective.update(_safe_metadata(controls))
        effective["max_retries"] = 0
        return effective

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
        raw_key = self._journal.ledger[-1].get("raw_response_key")
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
                raw_response_key=raw_key,
                raw=self._raw_responses.get(raw_key) if isinstance(raw_key, str) else None,
                review=review,
            )
        )

    def _policy_result(
        self,
        status: str,
        plan: dict[str, Any] | None,
        findings: tuple[Finding, ...] | list[Finding],
    ) -> AuthoringResult:
        if self._review_status is not None:
            self._journal.append(ReviewStatusRecorded(dict(self._review_status)))
        self._record_allowances()
        result = self._result(status, plan, list(findings))
        result.review_status = dict(self._review_status) if self._review_status else {}
        result.allowances = dict(self._allowances) if self._allowances else {}
        result.review_revision_allowances = (
            dict(self._review_revision_allowances) if self._review_revision_allowances else {}
        )
        result.budget = self.budget.snapshot(self.task_id)
        return result

    def _result(
        self,
        status: str,
        plan: dict[str, Any] | None,
        findings: list[Finding],
    ) -> AuthoringResult:
        if any(finding.code == "prompt_overflow" for finding in findings):
            status = "prompt_overflow"
        failure_evidence_path = self._finish(status, findings)
        return AuthoringResult(
            status=status,
            task_id=self.task_id,
            plan=plan,
            findings=list(findings),
            ledger=list(self._journal.ledger),
            transformations=list(self._transformations),
            raw_responses=dict(self._raw_responses),
            decoded_responses=dict(self._decoded_responses),
            prompts=dict(self._prompt_packets),
            failure_evidence_path=failure_evidence_path,
            budget=self.budget.snapshot(self.task_id),
        )

    def _latest_attempt_findings(self, fallback: list[Finding]) -> list[Finding]:
        """Return only the findings from the response that just terminated."""

        attempts = self._journal.evidence.get("attempts")
        latest = attempts[-1].get("findings") if isinstance(attempts, list) and attempts else None
        if not isinstance(latest, list) or not latest:
            return list(fallback)
        findings = (_finding_from_record(item) for item in latest)
        return [finding for finding in findings if finding is not None] or list(fallback)

    def _record_candidate_digest(self, candidate: dict[str, Any]) -> None:
        """Pin the normalized candidate bytes to the current dispatch event."""

        self._journal.append(
            CandidateDigested(_sha256(_canonical_json(candidate).encode("utf-8")))
        )

    def _record_transformation(self, transformation: Any) -> None:
        self._transformations.append(transformation)
        self._journal.append(TransformationRecorded(transformation, tuple(self._transformations)))

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

        attempts = self._journal.evidence["attempts"]
        if not attempts and not findings:
            return None
        terminal_findings = [] if status == "accepted" else self._latest_attempt_findings(findings)
        self._journal.append(
            Finished(
                status,
                tuple(terminal_findings),
                {
                    "stage": self._terminal_stage(terminal_findings),
                    "attempt_index": len(attempts) - 1 if attempts else None,
                    "reason": terminal_findings[-1].code if terminal_findings else status,
                },
            )
        )
        return self._journal.flush()

    def _terminal_stage(self, findings: list[Finding]) -> str | None:
        """Return the logical stage that produced the terminal outcome."""

        for finding in reversed(findings):
            stage = _FINDING_PATH_STAGES.get(finding.path)
            if stage is not None:
                return stage
        attempts = self._journal.evidence.get("attempts")
        if isinstance(attempts, list) and attempts:
            return _attempt_terminal_stage(attempts[-1])
        return None
