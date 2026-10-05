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

from ..detector_controls import (
    ControlCase,
    DetectorControlFeedback,
    build_control_cases_for_runtime_contract,
    build_control_skips_for_runtime_contract,
    build_detector_feedback,
    run_detector_controls,
)
from ..failure_evidence import (
    failure_evidence_path,
    metadata_record,
    new_failure_evidence,
    raw_response_record,
    write_failure_evidence,
)
from ..input_adapter import InputView
from ..package_io import write_package
from .checks import (
    _is_blocked_plan,
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
    parse_call2_response,
)
from .context_budget import _enforce_prompt_size
from .controls import (
    SuppliedControlCases,
    _deduplicate_control_cases,
    _mark_control_origins,
    _validate_supplied_control_cases,
)
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
    ParsedCall2Response,
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
    _set_record_usage,
    _sha256,
)
from .correction import _render_correction_packet, build_correction_context
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
    _plan_semantic_judge_needed,
    build_artifact_author_context,
    build_plan_author_context,
    scenario_provenance_ids,
)
from .prompt_packets import build_call1_packet_v2, build_call2_packet_v2
from .prompt_safety import assert_no_secrets, prompt_data_urls
from .response_decode import _decode_v2_json_response, _response_parts
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
    supplied_control_cases: SuppliedControlCases | None,
    discovery_provenance: dict[str, Any] | None,
    policy: AuthoringPolicy,
) -> None:
    if getattr(transport, "max_retries", None) != 0:
        raise ValueError("authoring transport must set max_retries=0")
    if supplied_control_cases is not None:
        _validate_supplied_control_cases(supplied_control_cases)
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


def _record_control_evidence(
    record: dict[str, Any],
    controls: list[dict[str, Any]],
    deduplication: Any,
    skips: list[dict[str, Any]],
) -> None:
    record["detector_controls"] = controls
    record["control_deduplication"] = deepcopy(deduplication)
    if skips:
        record["detector_control_skips"] = deepcopy(skips)


def _correction_checks_not_run(failed_stage: str) -> list[str]:
    if failed_stage == "call1":
        return ["plan_validation"]
    return ["artifact_validation", "detector_controls"]


def _secret_scan_target(validation_value: dict[str, Any] | ParsedCall2Response) -> Any:
    if isinstance(validation_value, ParsedCall2Response):
        return validation_value.metadata
    return validation_value


_REVIEW_EVIDENCE_KEYS = (
    "decision",
    "original_decision",
    "decision_after_scope_filter",
    "summary",
    "findings",
    "out_of_scope_findings",
    "question_ids",
)


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


_FINDING_PATH_STAGES = {
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

    Each stage keeps its own correction allowance, deterministic checks and
    detector controls precede every semantic review, and review decisions
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
        supplied_control_cases: SuppliedControlCases | None = None,
        discovery_provenance: dict[str, Any] | None = None,
    ) -> None:
        _validate_orchestrator_arguments(
            transport, supplied_control_cases, discovery_provenance, policy
        )
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
        self._ledger: list[dict[str, Any]] = []
        self._findings: list[Finding] = []
        self._dispatch_count = 0
        self._dispatch_recorded = True
        self._allowances: dict[str, int] | None = None
        self._review_revision_allowances: dict[str, int] | None = None
        self._review_status: dict[str, str] | None = None
        self._review_evidence: dict[str, dict[str, Any]] = {}
        self._saved_plan_review_packet: PromptPacket | None = None
        self._last_controls: list[dict[str, Any]] | None = None
        self._control_condition: dict[str, Any] | None = None
        self._last_detector_feedback: tuple[DetectorControlFeedback, ...] = ()
        self._supplied_control_cases = supplied_control_cases
        self._raw_responses: dict[str, bytes] = {}
        self._decoded_responses: dict[str, Any] = {}
        self._prompt_packets: dict[str, PromptPacket] = {}
        self._transformations: list[Any] = []
        self._failure_evidence = new_failure_evidence(self.task_id, self.package_dir)
        self._failure_evidence["budget"] = self.budget.snapshot(self.task_id)
        self._failure_evidence_file: Path | None = None

    def run(
        self,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> AuthoringResult:
        condition = view.payload.get("discriminating_condition")
        self._control_condition = deepcopy(condition) if isinstance(condition, dict) else None
        return self._run_v2_policy(view, inventory, runtime_contract)

    def _run_detector_controls(
        self,
        parsed: ParsedCall2Response,
        plan: dict[str, Any],
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
        findings: list[Finding],
    ) -> list[Finding]:
        """Run finite controls before correction or package publication."""

        normal_cases = build_control_cases_for_runtime_contract(
            runtime_contract,
            plan,
            parsed.metadata,
            inventory,
            condition=self._control_condition,
        )
        skips = [
            skip.as_dict()
            for skip in build_control_skips_for_runtime_contract(
                runtime_contract,
                plan,
                parsed.metadata,
                inventory,
                condition=self._control_condition,
            )
        ]
        supplied_cases = self._resolve_supplied_control_cases(plan, parsed.metadata)
        cases, deduplication = _deduplicate_control_cases(normal_cases, supplied_cases)
        raw_findings, controls = run_detector_controls(
            parsed.python_bytes,
            cases=cases,
            plan=plan,
            metadata=parsed.metadata,
            inventory=inventory,
            runtime_contract=runtime_contract,
            condition=self._control_condition,
        )
        if self._supplied_control_cases is not None:
            _mark_control_origins(controls, len(normal_cases))
        self._last_controls = controls
        self._last_detector_feedback = build_detector_feedback(
            cases,
            controls,
            judge_enabled=(
                _plan_semantic_judge_needed(plan)
                or parsed.metadata.get("semantic_judge_spec") is not None
            ),
        )
        if self._ledger:
            _record_control_evidence(self._ledger[-1], controls, deduplication, skips)
        if self._failure_evidence.get("attempts"):
            _record_control_evidence(self._failure_attempt(), controls, deduplication, skips)
        control_findings = [
            Finding(item["code"], item["detail"], item.get("path", "")) for item in raw_findings
        ]
        if control_findings:
            self._findings.extend(control_findings)
            if self._ledger:
                prior = self._ledger[-1].get("findings", [])
                self._ledger[-1]["findings"] = [
                    *prior,
                    *(finding.to_dict() for finding in control_findings),
                ]
            self._record_failures(control_findings)
            return [*findings, *control_findings]
        self._persist_failure_evidence()
        return findings

    def _resolve_supplied_control_cases(
        self,
        plan: dict[str, Any],
        metadata: dict[str, Any],
    ) -> tuple[ControlCase, ...]:
        """Resolve the caller's supplied-control hook for one candidate.

        A provider callable receives the current candidate plan and metadata so
        the caller can mechanically remap candidate-local names and dynamic
        record IDs into its case evidence.
        """

        hook = self._supplied_control_cases
        if hook is None:
            return ()
        resolved = hook(plan, metadata) if callable(hook) else hook
        if isinstance(resolved, (str, bytes)) or not isinstance(resolved, Sequence):
            raise ValueError("supplied_control_cases must resolve to ControlCase instances")
        cases = tuple(resolved)
        for case in cases:
            if not isinstance(case, ControlCase):
                raise ValueError("supplied_control_cases must resolve to ControlCase instances")
        return cases

    @staticmethod
    def _parse_candidate_for_controls(raw: bytes) -> ParsedCall2Response | None:
        """Recover a syntactically complete candidate without repairing it."""

        try:
            return parse_call2_response(raw)
        except (Call2FramingError, UnicodeDecodeError, ValueError):
            return None

    def _request_and_validate_v2(
        self,
        packet: PromptPacket,
        findings_collector: Any,
    ) -> tuple[dict[str, Any] | ParsedCall2Response | None, list[Finding], bytes]:
        """Dispatch and validate one v2 stage without changing response bytes."""

        stage = packet.stage
        self._prompt_packets[stage] = packet
        started = time.monotonic()
        try:
            response = self._dispatch(packet)
        except Exception as exc:
            return None, [self._record_v2_dispatch_failure(exc, stage, started)], b""
        raw, record = self._record_v2_response(stage, response)
        try:
            validation_value = self._decode_v2_stage(stage, raw, record)
        except (
            Call1FramingError,
            Call2FramingError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            return None, self._record_v2_decode_failure(exc, stage, record), raw
        rejection = self._v2_response_rejection(stage, validation_value)
        if rejection is not None:
            self._findings.append(rejection)
            self._record_failure(rejection)
            return None, [rejection], raw
        transformation_count = len(self._transformations)
        findings = findings_collector(validation_value)
        self._record_validation_transformations(transformation_count)
        if findings:
            self._findings.extend(findings)
            record["findings"] = [finding.to_dict() for finding in findings]
            self._record_failures(findings)
            return None, findings, raw
        record["validation"] = "passed"
        self._persist_failure_evidence()
        return validation_value, [], raw

    def _record_v2_dispatch_failure(self, exc: Exception, stage: str, started: float) -> Finding:
        """Record a v2 stage request that returned no response and return its finding."""

        if isinstance(exc, PromptOverflowError):
            finding = _prompt_overflow_finding(exc, stage)
            self._findings.append(finding)
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            return finding
        if isinstance(exc, BudgetExceeded):
            # Budget stops happen before dispatch: no ledger record exists to
            # annotate, and no stage attempt was created for this request.
            finding = Finding("budget_exhausted", _safe_error(exc), stage)
            self._findings.append(finding)
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            return finding
        finding = Finding("transport_failure", _safe_error(exc), stage)
        self._findings.append(finding)
        if self._dispatch_recorded:
            self._ledger[-1]["error"] = _safe_error(exc)
        self._record_unavailable_response(
            reason="provider_failure",
            detail=_safe_error(exc),
            finding=finding,
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
        return finding

    def _record_v2_response(
        self, stage: str, response: TransportResponse | str | bytes
    ) -> tuple[bytes, dict[str, Any]]:
        """Store a v2 stage response on its ledger record; return the raw bytes and record."""

        raw, usage, controls, response_capture = _response_parts(response)
        self._raw_responses[stage] = raw
        record = self._ledger[-1]
        raw_key = f"dispatch:{record['dispatch_index']}"
        self._raw_responses[raw_key] = raw
        record["raw_response_key"] = raw_key
        _set_record_usage(record, usage)
        record["controls"] = _safe_metadata(controls or {"max_retries": 0})
        if response_capture is not None:
            record["response_capture"] = deepcopy(response_capture)
        self._record_available_response(raw, usage, controls, response_capture)
        return raw, record

    def _decode_v2_stage(
        self, stage: str, raw: bytes, record: dict[str, Any]
    ) -> dict[str, Any] | ParsedCall2Response:
        """Decode one v2 stage response and record the decoded output."""

        if stage == "call2":
            decoded = parse_call2_response(raw)
            self._raw_responses["call2-python"] = decoded.python_bytes
            record["framing"] = "two-block-v2"
            record["decoded_output"] = decoded.metadata
            self._decoded_responses[stage] = decoded.metadata
            self._failure_attempt()["decoded_output"] = decoded.metadata
            self._record_candidate_digest(decoded)
            return decoded
        decoded_json, transformation = _decode_v2_json_response(raw)
        if transformation:
            self._transformations.append(transformation)
            record["transformation"] = transformation
            self._failure_attempt()["transformation"] = transformation
            self._failure_evidence["transformations"] = list(self._transformations)
        self._decoded_responses[stage] = decoded_json
        record["decoded_output"] = decoded_json
        self._failure_attempt()["decoded_output"] = decoded_json
        if isinstance(decoded_json, dict):
            self._record_candidate_digest(decoded_json)
        return decoded_json

    def _record_v2_decode_failure(
        self, exc: Exception, stage: str, record: dict[str, Any]
    ) -> list[Finding]:
        """Record a framing or parse failure for a v2 stage and return its findings."""

        checks_not_run = (
            ["plan_validation"]
            if stage == "call1"
            else ["artifact_validation", "detector_controls"]
        )
        if isinstance(exc, (Call1FramingError, Call2FramingError)):
            self._findings.extend(exc.findings)
            record["framing_findings"] = [finding.to_dict() for finding in exc.findings]
            self._record_checks_not_run(checks_not_run)
            self._record_failures(exc.findings)
            return exc.findings
        finding = Finding("response_parse_error", str(exc), stage)
        record["parse_error"] = str(exc)
        self._record_checks_not_run(checks_not_run)
        self._findings.append(finding)
        self._record_failure(finding)
        return [finding]

    @staticmethod
    def _v2_response_rejection(
        stage: str, validation_value: dict[str, Any] | ParsedCall2Response
    ) -> Finding | None:
        """Return the finding that rejects a decoded value before its stage checks run."""

        try:
            assert_no_secrets(
                validation_value.metadata
                if isinstance(validation_value, ParsedCall2Response)
                else validation_value
            )
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
        dispatch_index = self._dispatch_count + 1
        self._dispatch_count = dispatch_index
        self._failure_evidence["budget"] = self.budget.snapshot(self.task_id)
        record = self._open_dispatch_records(packet, dispatch_index, role)
        self._persist_failure_evidence()
        try:
            response = self.transport.complete(packet)
        except Exception:
            self._record_transport_failure_controls(record)
            raise
        self._record_dispatch_response(record, dispatch_index, response)
        self._persist_failure_evidence()
        return response

    def _dispatch_attempt_indices(self, packet: PromptPacket) -> tuple[int, int]:
        """Return the stage attempt index and the per-failed-stage correction index."""

        stage_attempt_index = (
            sum(1 for prior in self._ledger if prior.get("stage") == packet.stage) + 1
        )
        if packet.stage != "correction":
            return stage_attempt_index, 0
        failed_stage = (
            packet.payload.get("failed_stage") if isinstance(packet.payload, dict) else None
        )
        correction_index = (
            sum(
                1
                for prior in self._ledger
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

    def _open_dispatch_records(
        self, packet: PromptPacket, dispatch_index: int, role: str
    ) -> dict[str, Any]:
        """Append the in-progress ledger record and failure-evidence attempt for a dispatch."""

        stage_attempt_index, correction_index = self._dispatch_attempt_indices(packet)
        model_identity = self._dispatch_model_identity()
        policy_record = self._effective_policy_record()
        record = {
            "dispatch_index": dispatch_index,
            "attempt_index": stage_attempt_index,
            "stage_attempt_index": stage_attempt_index,
            "correction_index": correction_index,
            "role": role,
            "stage": packet.stage,
            "task_id": self.task_id,
            "prompt_version": packet.version,
            "prompt_sha256": packet.sha256,
            "prompt_hash": packet.sha256,
            "prompt_system": packet.system,
            "prompt_user": packet.user,
            "controls": {"max_retries": 0},
            "policy": deepcopy(policy_record),
            "raw_response": f"authoring/{len(self._ledger) + 1:02d}-{packet.stage}.raw",
            "model_identity": model_identity,
            "terminal_status": "in_progress",
        }
        if packet.stage in _REVIEW_STAGES:
            record.update(self._review_dispatch_fields(packet))
        self._ledger.append(record)
        self._failure_evidence["attempts"].append(
            {
                "dispatch_index": dispatch_index,
                "attempt_index": stage_attempt_index,
                "stage_attempt_index": stage_attempt_index,
                "correction_index": correction_index,
                "role": role,
                "stage": packet.stage,
                "task_id": self.task_id,
                "policy": deepcopy(policy_record),
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
                "model_identity": deepcopy(model_identity),
                "raw_response": raw_response_record(b"", reason="not_returned"),
                "usage": metadata_record(None, unavailable_reason="not_returned"),
                "findings": [],
                "terminal_status": "in_progress",
            }
        )
        if packet.stage in _REVIEW_STAGES:
            self._failure_attempt()["review"] = deepcopy(record["review"])
            self._failure_attempt()["reviewed_input_sha256"] = record["reviewed_input_sha256"]
            self._failure_attempt()["reviewed_candidate_sha256"] = record[
                "reviewed_candidate_sha256"
            ]
        return record

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

    def _record_transport_failure_controls(self, record: dict[str, Any]) -> None:
        """Keep the controls a transport reports for a request that raised."""

        last_controls = getattr(self.transport, "last_controls", None)
        if isinstance(last_controls, dict):
            record["controls"] = _safe_metadata(last_controls)
            self._failure_attempt()["controls"] = metadata_record(
                last_controls,
                unavailable_reason="provider_did_not_return_response",
            )

    def _record_dispatch_response(
        self,
        record: dict[str, Any],
        dispatch_index: int,
        response: TransportResponse | str | bytes,
    ) -> None:
        """Record the returned model, raw bytes, usage, and controls of a response."""

        provider_model = (
            response.provider_model if isinstance(response, TransportResponse) else None
        )
        returned_model = metadata_record(
            provider_model,
            unavailable_reason="provider_did_not_report_model",
        )
        record["model_identity"]["returned_model"] = returned_model
        self._failure_attempt()["model_identity"]["returned_model"] = deepcopy(returned_model)
        raw, usage, controls, response_capture = _response_parts(response)
        raw_key = f"dispatch:{dispatch_index}"
        self._raw_responses[raw_key] = raw
        record["raw_response_key"] = raw_key
        _set_record_usage(record, usage)
        record["controls"] = _safe_metadata(controls or {"max_retries": 0})
        if response_capture is not None:
            record["response_capture"] = deepcopy(response_capture)
        attempt = self._failure_attempt()
        attempt["raw_response"] = raw_response_record(raw)
        attempt["usage"] = metadata_record(
            usage if usage else None,
            unavailable_reason="provider_did_not_report_usage",
        )
        attempt["controls"] = metadata_record(
            controls or {"max_retries": 0},
            unavailable_reason="controls_not_recorded",
        )
        if response_capture is not None:
            attempt["response_capture"] = deepcopy(response_capture)

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
    ) -> tuple[dict[str, Any] | ParsedCall2Response | None, list[Finding], bytes] | None:
        """Replace one failed v2 response in its original stage format.

        The caller owns the stage-local allowance decision. A syntactically
        valid candidate is returned with its validation findings so the caller
        can run controls before deciding whether to correct again.
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
        self._ledger[-1]["validation"] = "passed"
        self._persist_failure_evidence()
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
            detector_feedback=(self._last_detector_feedback if failed_stage == "call2" else None),
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
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
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
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            return _CORRECTION_NOT_DISPATCHED
        except BudgetExceeded as exc:
            # Budget stops happen before dispatch: no ledger record or stage
            # attempt exists for the refused correction request.
            finding = Finding("budget_exhausted", _safe_error(exc), failed_stage)
            self._findings.append(finding)
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            return _CORRECTION_NOT_DISPATCHED
        except Exception as exc:
            finding = Finding("correction_dispatch_failed", _safe_error(exc), failed_stage)
            if self._dispatch_recorded:
                self._ledger[-1]["error"] = _safe_error(exc)
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
        """Record the correction response in the ledger and failure evidence; return its bytes."""

        raw, usage, controls, response_capture = _response_parts(response)
        raw_key = f"dispatch:{self._ledger[-1]['dispatch_index']}"
        self._raw_responses[raw_key] = raw
        self._raw_responses["correction"] = raw
        self._ledger[-1]["raw_response_key"] = raw_key
        _set_record_usage(self._ledger[-1], usage)
        self._ledger[-1]["controls"] = _safe_metadata(controls or {"max_retries": 0})
        if response_capture is not None:
            self._ledger[-1]["response_capture"] = deepcopy(response_capture)
        self._ledger[-1]["failed_stage"] = failed_stage
        self._failure_attempt()["failed_stage"] = failed_stage
        self._ledger[-1]["allowance"] = allowance_kind
        self._failure_attempt()["allowance"] = allowance_kind
        self._failure_attempt()["failed_response"] = raw_response_record(
            failed_response,
            reason="not_returned" if not failed_response else None,
        )
        self._record_available_response(raw, usage, controls, response_capture)
        return raw

    def _decode_correction_response(
        self, raw: bytes, failed_stage: str
    ) -> tuple[dict[str, Any] | ParsedCall2Response, Any]:
        """Return the value to validate and the decoded output of a correction response."""

        if failed_stage == "call2":
            parsed = parse_call2_response(raw)
            self._raw_responses["call2-python"] = parsed.python_bytes
            return parsed, parsed.metadata
        decoded, transformation = _decode_v2_json_response(raw)
        if transformation:
            self._transformations.append(transformation)
            self._failure_evidence["transformations"] = list(self._transformations)
            self._ledger[-1]["transformation"] = transformation
            self._failure_attempt()["transformation"] = transformation
        return decoded, decoded

    def _validate_correction_response(
        self,
        raw: bytes,
        failed_stage: str,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> tuple[dict[str, Any] | ParsedCall2Response, Any, list[Finding]] | None:
        """Decode and validate a correction; record a failure and return None.

        Returns the value to validate, the decoded output, and the recorded
        validation findings.
        """

        checks_not_run = _correction_checks_not_run(failed_stage)
        try:
            validation_value, decoded = self._decode_correction_response(raw, failed_stage)
            assert_no_secrets(_secret_scan_target(validation_value))
            self._decoded_responses[f"correction-{failed_stage}"] = decoded
            self._failure_attempt()["decoded_output"] = decoded
            self._record_candidate_digest(validation_value)
            self._persist_failure_evidence()
            if not isinstance(decoded, dict):
                raise ValueError("correction response must decode to an object")
            transformation_count = len(self._transformations)
            replacement_findings = self._correction_findings(
                failed_stage, decoded, validation_value, view, inventory, runtime_contract
            )
            self._record_validation_transformations(transformation_count)
            if replacement_findings:
                self._ledger[-1]["findings"] = [
                    finding.to_dict() for finding in replacement_findings
                ]
                self._findings.extend(replacement_findings)
                self._record_failures(replacement_findings)
        except (Call1FramingError, Call2FramingError) as exc:
            self._ledger[-1]["framing_findings"] = [finding.to_dict() for finding in exc.findings]
            self._record_checks_not_run(checks_not_run)
            self._findings.extend(exc.findings)
            self._record_failures(exc.findings)
            return None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, AuthoringError) as exc:
            self._record_correction_failure(exc, failed_stage, checks_not_run)
            return None
        return validation_value, decoded, replacement_findings

    def _correction_findings(
        self,
        failed_stage: str,
        decoded: dict[str, Any],
        validation_value: dict[str, Any] | ParsedCall2Response,
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
            validation_value,
            self._decoded_responses["call1"],
            inventory,
            runtime_contract,
            transformations=self._transformations,
        )

    def _record_correction_failure(
        self, exc: Exception, failed_stage: str, checks_not_run: list[str]
    ) -> None:
        finding = Finding("correction_failed", str(exc), failed_stage)
        self._ledger[-1]["findings"] = [finding.to_dict()]
        if isinstance(exc, (UnicodeDecodeError, json.JSONDecodeError)):
            self._ledger[-1]["parse_error"] = str(exc)
            self._record_checks_not_run(checks_not_run)
        self._findings.append(finding)
        self._record_failure(finding)

    def _run_v2_policy(
        self,
        view: InputView,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> AuthoringResult:
        """Run the stage-local correction and review state machine.

        Each stage owns its correction allowance; deterministic checks and
        detector controls precede every semantic review; and review decisions
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
        self._failure_evidence["policy"] = self._effective_policy_record()
        self._failure_evidence["review_status"] = dict(self._review_status)
        self._persist_failure_evidence()
        plan = self._plan_stage_policy(view, inventory, runtime_contract)
        if isinstance(plan, _StageStop):
            return self._policy_result(plan.status, None, plan.findings)
        artifact = self._artifact_stage_policy(view, plan, inventory, runtime_contract)
        if isinstance(artifact, _StageStop):
            return self._policy_result(artifact.status, plan, artifact.findings)
        parsed, metadata, artifact_definition = artifact
        try:
            package = _package_from_responses(
                view=view,
                plan=plan,
                artifact=artifact_definition,
                task_id=self.task_id,
                ledger=self._ledger,
                raw_responses=self._raw_responses,
                decoded_responses=self._decoded_responses,
                prompt_packets=self._prompt_packets,
                transformations=self._transformations,
                inventory=inventory,
                runtime_contract=runtime_contract,
                discovery_provenance=self.discovery_provenance,
                detector_bytes=parsed.python_bytes,
                policy=self._effective_policy_record(),
                review_status=dict(self._review_status),
                preserved_reviews=self._review_evidence,
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
        self._failure_evidence["review_status"] = dict(self._review_status)
        self._record_allowances()
        self._finish_failure_evidence("accepted", [])
        return AuthoringResult(
            status="accepted",
            task_id=self.task_id,
            plan=plan,
            artifact=metadata,
            package=package,
            package_path=path,
            findings=[],
            ledger=list(self._ledger),
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
    ) -> tuple[ParsedCall2Response, dict[str, Any], dict[str, Any]] | _StageStop:
        """Author, check, control, correct, and review the artifact stage."""

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

        parsed: ParsedCall2Response | None = None
        pending: Sequence[Finding] | None = None
        review_driven = False
        raw = b""
        while True:
            if parsed is None:
                if pending is None:
                    parsed, pending, raw = self._first_artifact_candidate(
                        packet, collector, plan, inventory, runtime_contract
                    )
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

    def _first_artifact_candidate(
        self,
        packet: PromptPacket,
        collector: Any,
        plan: dict[str, Any],
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> tuple[ParsedCall2Response | None, list[Finding], bytes]:
        """Request the artifact, run controls on any runnable candidate, and keep it if clean."""

        candidate, pending, raw = self._request_and_validate_v2(packet, collector)
        recover = (
            candidate
            if isinstance(candidate, ParsedCall2Response)
            else (self._parse_candidate_for_controls(raw) if raw else None)
        )
        if recover is not None:
            # A runnable candidate is exercised where the isolated
            # control interface safely supports it, even beside
            # structural findings; every obtainable defect is
            # collected before any correction decision.
            pending = self._run_detector_controls(
                recover,
                plan,
                inventory,
                runtime_contract,
                pending,
            )
        parsed = candidate if candidate is not None and not pending else None
        return parsed, pending, raw

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
    ) -> tuple[ParsedCall2Response | None, list[Finding], bytes] | _StageStop:
        """Spend one artifact allowance on a correction and run controls on its candidate.

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
        if not isinstance(corrected_candidate, ParsedCall2Response):
            return self._author_stop_or_unresolved(correction_findings)
        controlled = self._run_detector_controls(
            corrected_candidate,
            plan,
            inventory,
            runtime_contract,
            list(correction_findings),
        )
        if controlled:
            # A corrected candidate can fail deterministic checks,
            # controls, or both. Keep every finding and use the
            # latest candidate/control feedback for the next
            # correction while allowance remains.
            return None, controlled, raw
        return corrected_candidate, controlled, raw

    def _review_artifact_candidate(
        self,
        view: InputView,
        plan: dict[str, Any],
        parsed: ParsedCall2Response,
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> (
        tuple[ParsedCall2Response, dict[str, Any], dict[str, Any]] | _StageStop | _ArtifactRevision
    ):
        """Accept a clean candidate, or review it and stop, accept, or ask for a revision."""

        if not self.policy.review_artifact:
            self._review_status["artifact"] = "not_requested"
            return self._artifact_parts(parsed, plan)
        try:
            review_packet = build_artifact_review_packet(
                view,
                plan,
                parsed.metadata,
                parsed.python_bytes,
                self._last_controls,
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
        parsed: ParsedCall2Response,
        plan: dict[str, Any],
    ) -> tuple[ParsedCall2Response, dict[str, Any], dict[str, Any]]:
        """Join the validated candidate with the accepted plan-owned fields."""

        metadata = parsed.metadata
        artifact = {
            **metadata,
            # These values are copied from the accepted plan.  Call 2 and its
            # corrections never rewrite them.
            "setup_recipe": plan["setup_recipe"],
            "runtime_bindings": plan["runtime_bindings"],
            "prerequisites": plan["prerequisites"],
            "required_observations": plan["required_observations"],
        }
        return parsed, metadata, artifact

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
        raw, record, effective_controls = self._record_review_response(packet, response)
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
            record["transformation"] = review.transformation
            self._transformations.append(review.transformation)
            self._failure_attempt()["transformation"] = review.transformation
            self._failure_evidence["transformations"] = list(self._transformations)
        record["review"] = review_record
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
        self._decoded_responses[packet.stage] = review_record
        self._failure_attempt()["review"] = deepcopy(record["review"])
        self._persist_failure_evidence()
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
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            return _StageStop("prompt_overflow", (finding,))
        if isinstance(exc, BudgetExceeded):
            # Budget stops happen before dispatch: no ledger record or stage
            # attempt exists for the refused review request.
            finding = Finding("budget_exhausted", _safe_error(exc), packet.stage)
            self._findings.append(finding)
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
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
        """Annotate a dispatched review that raised with its error, controls, and digests."""

        self._ledger[-1]["error"] = _safe_error(exc)
        effective_controls = self._review_controls(self._ledger[-1].get("controls"))
        self._ledger[-1]["controls"] = effective_controls
        self._failure_attempt()["controls"] = metadata_record(
            effective_controls,
            unavailable_reason="controls_not_recorded",
        )
        input_digest, candidate_digest = _review_packet_digests(packet)
        self._ledger[-1]["reviewed_input_sha256"] = input_digest
        self._ledger[-1]["reviewed_candidate_sha256"] = candidate_digest
        self._failure_attempt()["reviewed_input_sha256"] = input_digest
        self._failure_attempt()["reviewed_candidate_sha256"] = candidate_digest
        self._set_review_evidence(
            status="unavailable",
            effective_controls=effective_controls,
            packet=packet,
        )

    def _record_review_response(
        self, packet: PromptPacket, response: TransportResponse | str | bytes
    ) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
        """Store a review response as pending evidence.

        Return the raw bytes, the ledger record, and the effective controls.
        """

        raw, usage, controls, response_capture = _response_parts(response)
        record = self._ledger[-1]
        raw_key = f"dispatch:{record['dispatch_index']}"
        self._raw_responses[raw_key] = raw
        self._raw_responses[packet.stage] = raw
        record["raw_response_key"] = raw_key
        _set_record_usage(record, usage)
        effective_controls = self._review_controls(controls)
        record["controls"] = effective_controls
        if response_capture is not None:
            record["response_capture"] = deepcopy(response_capture)
        input_digest, candidate_digest = _review_packet_digests(packet)
        record["reviewed_input_sha256"] = input_digest
        record["reviewed_candidate_sha256"] = candidate_digest
        record["candidate_bytes_sha256"] = candidate_digest
        self._failure_attempt()["reviewed_input_sha256"] = input_digest
        self._failure_attempt()["reviewed_candidate_sha256"] = candidate_digest
        if response_capture is not None:
            self._failure_attempt()["response_capture"] = deepcopy(response_capture)
        self._set_review_evidence(
            status="pending",
            effective_controls=effective_controls,
            packet=packet,
        )
        self._record_available_response(
            raw,
            usage,
            effective_controls,
            response_capture,
        )
        return raw, record, effective_controls

    def _review_parse_stop(
        self,
        exc: ReviewResponseError,
        packet: PromptPacket,
        effective_controls: dict[str, Any],
    ) -> _StageStop:
        """Record a review response that failed to parse and return its stop."""

        finding = Finding("review_unavailable", _safe_error(exc), packet.stage)
        self._findings.append(finding)
        if self._dispatch_recorded:
            self._ledger[-1]["review_error"] = [item.to_dict() for item in exc.findings]
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
        if self._dispatch_recorded:
            self._ledger[-1]["failure"] = dict(failure)
        self._failure_attempt()["failure"] = failure
        self._failure_evidence["findings"].append(finding.to_dict())
        self._persist_failure_evidence()
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
        if self._allowances is not None:
            self._failure_evidence["allowances"] = dict(self._allowances)
        if self._review_revision_allowances is not None:
            self._failure_evidence["review_revision_allowances"] = dict(
                self._review_revision_allowances
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
        """Update the durable review record shared by ledger and failure evidence."""

        if (
            not self._dispatch_recorded
            or not self._ledger
            or not self._failure_evidence.get("attempts")
        ):
            return
        record = self._ledger[-1]
        evidence = record.setdefault("review", {})
        input_digest, candidate_digest = _review_packet_digests(packet)
        evidence.update(
            {
                "status": status,
                "prompt_version": packet.version,
                "prompt_sha256": packet.sha256,
                "reviewed_input_sha256": record.get("reviewed_input_sha256", input_digest),
                "reviewed_candidate_sha256": record.get(
                    "reviewed_candidate_sha256", candidate_digest
                ),
                "candidate_bytes_sha256": record.get("candidate_bytes_sha256", candidate_digest),
                "contract_sha256": _review_contract_digest(packet),
                "configuration_sha256": _review_configuration_digest(
                    effective_controls,
                    self._effective_policy_record(),
                ),
                "effective_controls": dict(effective_controls),
            }
        )
        raw_key = record.get("raw_response_key")
        raw = self._raw_responses.get(raw_key) if isinstance(raw_key, str) else None
        if raw is not None:
            evidence["raw_response_key"] = raw_key
            evidence["raw_response_sha256"] = _sha256(raw)
            evidence["raw_response_bytes"] = len(raw)
        if review is not None:
            evidence.update(
                {key: deepcopy(review[key]) for key in _REVIEW_EVIDENCE_KEYS if key in review}
            )
        attempt = self._failure_attempt()
        attempt["review"] = deepcopy(evidence)
        self._review_evidence["plan" if packet.stage == "plan_review" else "artifact"] = deepcopy(
            evidence
        )

    def _policy_result(
        self,
        status: str,
        plan: dict[str, Any] | None,
        findings: tuple[Finding, ...] | list[Finding],
    ) -> AuthoringResult:
        if self._review_status is not None:
            self._failure_evidence["review_status"] = dict(self._review_status)
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
        return AuthoringResult(
            status=status,
            task_id=self.task_id,
            plan=plan,
            findings=list(findings),
            ledger=list(self._ledger),
            transformations=list(self._transformations),
            raw_responses=dict(self._raw_responses),
            decoded_responses=dict(self._decoded_responses),
            prompts=dict(self._prompt_packets),
            failure_evidence_path=self._finish_failure_evidence(status, findings),
            budget=self.budget.snapshot(self.task_id),
        )

    def _latest_attempt_findings(self, fallback: list[Finding]) -> list[Finding]:
        """Return only the findings from the response that just terminated."""

        attempts = self._failure_evidence.get("attempts")
        latest = attempts[-1].get("findings") if isinstance(attempts, list) and attempts else None
        if not isinstance(latest, list) or not latest:
            return list(fallback)
        findings = (_finding_from_record(item) for item in latest)
        return [finding for finding in findings if finding is not None] or list(fallback)

    def _failure_attempt(self) -> dict[str, Any]:
        return self._failure_evidence["attempts"][-1]

    def _record_candidate_digest(self, candidate: dict[str, Any] | ParsedCall2Response) -> None:
        """Pin the normalized candidate bytes to the current dispatch event."""

        if isinstance(candidate, ParsedCall2Response):
            digest = _sha256(
                _canonical_json(candidate.metadata).encode("utf-8")
                + b"\0"
                + candidate.python_bytes
            )
        else:
            digest = _sha256(_canonical_json(candidate).encode("utf-8"))
        self._ledger[-1]["candidate_sha256"] = digest
        self._failure_attempt()["candidate_sha256"] = digest

    def _record_validation_transformations(self, start: int) -> None:
        """Persist deterministic binding rewrites made during validation."""

        changes = self._transformations[start:]
        if not changes:
            return
        self._failure_evidence["transformations"] = list(self._transformations)
        attempt = self._failure_attempt()
        attempt["transformations"] = deepcopy(changes)
        if self._ledger:
            self._ledger[-1]["transformations"] = deepcopy(changes)
        self._persist_failure_evidence()

    def _record_available_response(
        self,
        raw: bytes,
        usage: dict[str, Any] | None,
        controls: dict[str, Any] | None,
        response_capture: dict[str, Any] | None = None,
    ) -> None:
        attempt = self._failure_attempt()
        attempt["raw_response"] = raw_response_record(raw)
        attempt["usage"] = metadata_record(
            usage if usage else None,
            unavailable_reason="provider_did_not_report_usage",
        )
        attempt["controls"] = metadata_record(
            controls or {"max_retries": 0},
            unavailable_reason="controls_not_recorded",
        )
        if response_capture is not None:
            attempt["response_capture"] = deepcopy(response_capture)
        self._persist_failure_evidence()

    def _record_unavailable_response(
        self,
        *,
        reason: str,
        detail: str,
        finding: Finding,
        elapsed_ms: float | None = None,
    ) -> None:
        attempt = self._failure_attempt()
        attempt["raw_response"] = raw_response_record(b"", reason=reason)
        attempt["usage"] = metadata_record(None, unavailable_reason=reason)
        attempt["failure"] = {"detail": _safe_error(detail), "phase": "invocation"}
        if elapsed_ms is not None:
            attempt["failure"]["elapsed_ms"] = round(elapsed_ms, 3)
        self._record_failure(finding)

    def _record_failure(self, finding: Finding) -> None:
        attempt = self._failure_attempt()
        attempt["findings"].append(finding.to_dict())
        if "failure" not in attempt:
            attempt["failure"] = {
                "phase": "post_response",
                "code": finding.code,
                "detail": finding.detail,
            }
        else:
            attempt["failure"]["code"] = finding.code
            attempt["failure"]["detail"] = finding.detail
        self._failure_evidence["findings"].append(finding.to_dict())
        self._persist_failure_evidence()

    def _record_checks_not_run(self, checks: list[str]) -> None:
        """Record downstream checks skipped after response framing failed."""

        if not checks:
            return
        values = list(dict.fromkeys(checks))
        for record in (self._ledger[-1], self._failure_attempt()):
            record["checks_not_run"] = values
            record["checks"] = {"status": "not_run", "not_run": values}

    def _record_failures(self, findings: list[Finding]) -> None:
        attempt = self._failure_attempt()
        for finding in findings:
            attempt["findings"].append(finding.to_dict())
            self._failure_evidence["findings"].append(finding.to_dict())
        if findings:
            failure = attempt.setdefault("failure", {})
            failure.update(
                {
                    "phase": failure.get("phase", "post_response"),
                    "code": findings[0].code,
                    "detail": findings[0].detail,
                }
            )
        self._persist_failure_evidence()

    def _persist_failure_evidence(self) -> None:
        self._failure_evidence_file = write_failure_evidence(
            failure_evidence_path(self.package_dir),
            self._failure_evidence,
        )

    def _finish_failure_evidence(
        self,
        status: str,
        findings: list[Finding],
    ) -> Path | None:
        if not self._failure_evidence["attempts"] and not findings:
            return None
        self._failure_evidence["status"] = status
        terminal_findings = (
            [] if status in {"accepted", "packaged"} else self._latest_attempt_findings(findings)
        )
        self._failure_evidence["findings"] = [finding.to_dict() for finding in terminal_findings]
        attempt_count = len(self._failure_evidence["attempts"])
        self._failure_evidence["terminal"] = {
            "stage": self._terminal_stage(terminal_findings),
            "attempt_index": attempt_count - 1 if attempt_count else None,
            "reason": terminal_findings[-1].code if terminal_findings else status,
        }
        for record in (*self._failure_evidence["attempts"], *self._ledger):
            record["terminal_status"] = status
            record["stage_status"] = status
        aggregate = self._failure_evidence.get("aggregate")
        if isinstance(aggregate, dict) and isinstance(aggregate.get("spent_before"), int):
            aggregate["spent_after"] = aggregate["spent_before"] + len(
                self._failure_evidence["attempts"]
            )
        self._persist_failure_evidence()
        return self._failure_evidence_file

    def _terminal_stage(self, findings: list[Finding]) -> str | None:
        """Return the logical stage that produced the terminal outcome."""

        for finding in reversed(findings):
            stage = _FINDING_PATH_STAGES.get(finding.path)
            if stage is not None:
                return stage
        attempts = self._failure_evidence.get("attempts")
        if isinstance(attempts, list) and attempts:
            return _attempt_terminal_stage(attempts[-1])
        return None
