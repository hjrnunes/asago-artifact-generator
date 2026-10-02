"""Target-free two-call authoring orchestration.

The model owns experiment meaning and detector source.  This module owns the
small mechanical surface around that response: deterministic prompt views,
closed structural checks, request accounting, and immutable package assembly.
It never contacts a target, setup transport, discovery service, or judge.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..bindings import CLOSED_TYPES, canonical_binding_paths, named_record_facts
from ..detector_controls import (
    ControlCase,
    DetectorControlFeedback,
    build_control_cases_for_runtime_contract,
    build_control_skips_for_runtime_contract,
    build_detector_feedback,
    build_detector_feedback_prompt_context,
    describe_input_shapes,
    run_detector_controls,
)
from ..failure_evidence import (
    failure_evidence_path,
    load_failure_evidence,
    metadata_record,
    new_failure_evidence,
    raw_response_record,
    write_failure_evidence,
)
from ..input_adapter import InputView
from ..package_io import ArtifactPackage, build_package, write_package
from .checks import (
    _binding_selector_type,
    _binding_source_schema,
    _binding_types_compatible,
    _is_blocked_plan,
    collect_artifact_findings_v2,
    collect_plan_findings,
    collect_plan_findings_v2,
    parse_call2_response,
)
from .context_budget import CONTEXT_GUARD_CALIBRATION as CONTEXT_GUARD_CALIBRATION
from .context_budget import _context_budget_estimate, _enforce_context_budget, _enforce_prompt_size
from .context_budget import _context_guard_ratio as _context_guard_ratio
from .contracts import _NEUTRAL_DETECTOR_SOURCE as _NEUTRAL_DETECTOR_SOURCE
from .contracts import (
    NEUTRAL_ESTABLISHED_OMISSION_OUTCOME_EXAMPLE,
    NEUTRAL_OMISSION_OUTCOME_EXAMPLE,
    NEUTRAL_PLAN_OUTCOME_EXAMPLE,
    PLAN_FIELD_MEANINGS,
    _call1_contract_v2,
    _call2_contract_v2,
    _render_evidence_packet_interface,
    evidence_packet_contract,
    neutral_artifact_plan,
    neutral_artifact_plan_v2,
    neutral_artifact_response_without_source,
    neutral_observation_cases,
)
from .contracts import _binding_contract as _binding_contract
from .contracts import _evidence_packet_contract as _evidence_packet_contract
from .core import (
    _CONTEXT_FRAMING_TOKEN_RESERVE,
    _REVIEW_STAGES,
    ARTIFACT_REVIEW_PROMPT_VERSION,
    ARTIFACT_REVIEW_PROMPT_VERSION_V16,
    AUTHORING_INTERFACE_VERSION_V2,
    AUTHORING_MAX_COMPLETION_TOKENS,
    CALL1_PROMPT_VERSION_V18,
    CALL2_PROMPT_VERSION_V21,
    CORRECTION_PROMPT_VERSION_V25,
    CORRECTION_PROMPT_VERSION_V27,
    MAX_AUTHOR_CORRECTION_REQUESTS_PER_TASK,
    MAX_AUTHORING_REQUESTS,
    MAX_RENDERED_PROMPT_BYTES,
    MAX_REQUESTS_PER_TASK,
    MAX_REVIEW_REQUESTS_PER_TASK,
    PLAN_REVIEW_PROMPT_VERSION,
    PLAN_REVIEW_PROMPT_VERSION_V17,
    REVIEW_REVISION_ALLOWANCE_PER_STAGE,
    ArtifactValidationError,
    AuthoringError,
    AuthoringTransport,
    BudgetExceeded,
    Call1FramingError,
    Call2FramingError,
    Finding,
    ParsedCall2Response,
    PlanValidationError,
    PromptOverflowError,
    PromptPacket,
    PromptPreflightError,
    ReviewResponse,
    ReviewResponseError,
    TransportResponse,
    _canonical_json,
    _json_bytes,
    _model_dump,
    _prompt_overflow_finding,
    _prompt_preflight_finding,
    _safe_error,
    _safe_metadata,
    _set_record_usage,
    _sha256,
)
from .core import AUTHORING_CONTEXT_WINDOW_TOKENS as AUTHORING_CONTEXT_WINDOW_TOKENS
from .core import AUTHORING_THINKING_EXTRA_BODY as AUTHORING_THINKING_EXTRA_BODY
from .core import REVIEW_THINKING_EXTRA_BODY as REVIEW_THINKING_EXTRA_BODY
from .inventory import (
    _expected_authoring_input_pins,
    _input_view_payload,
    _resolved_judge_spec,
    _source_input_payload,
)
from .prompt_context import _CURRENT_PLAN_AUTHOR_GUIDANCE as _CURRENT_PLAN_AUTHOR_GUIDANCE
from .prompt_context import (
    _OWNER_SCOPE_SECTION_TITLE,
    _context_has_not_called,
    _matching_runtime_observations,
    _plan_claim_level,
    _plan_semantic_judge_needed,
    _required_observation_keys,
    _scenario_design_prompt_view,
    _semantic_judge_fact_ref_guidance,
    artifact_observation_guide,
    build_artifact_author_context,
    build_plan_author_context,
    scenario_provenance_ids,
)
from .prompt_packets import (
    _artifact_response_contract_for_prompt,
    build_call1_packet_v2,
    build_call2_packet_v2,
)
from .prompt_safety import (
    _endpoint_identity,
    _endpoint_prompt_paths,
    assert_no_prompt_duplicates,
    assert_no_prompt_secrets,
    assert_no_secrets,
    prompt_data_urls,
    scan_for_prompt_secrets,
    scan_for_secrets,
    scan_prompt_duplicates,
)
from .response_decode import (
    _MISSING,
    _decode_v2_json_response,
    _provider_field,
    _provider_response_capture,
    _readable_response,
    _response_parts,
)
from .review import _PLAN_REVIEW_QUESTIONS as _PLAN_REVIEW_QUESTIONS
from .review import (
    ARTIFACT_REVIEW_QUESTION_IDS,
    PLAN_REVIEW_QUESTION_IDS,
    _review_configuration_digest,
    _review_contract_digest,
    _review_finding_to_finding,
    _review_packet_digests,
    _review_question_ids,
    _scope_review_response,
    build_artifact_review_packet,
    build_artifact_reviewer_context,
    build_plan_review_packet,
    build_plan_reviewer_context,
    parse_review_response,
)


@dataclass
class AuthoringBudget:
    """Shared aggregate and per-task request guard.

    Reservation happens before invoking the transport, so transport failures
    consume budget exactly like successful requests.
    """

    aggregate_limit: int = MAX_AUTHORING_REQUESTS
    task_limit: int = MAX_REQUESTS_PER_TASK
    author_limit: int = MAX_AUTHOR_CORRECTION_REQUESTS_PER_TASK
    review_limit: int = MAX_REVIEW_REQUESTS_PER_TASK
    total_dispatched: int = 0
    dispatched_by_task: dict[str, int] = field(default_factory=dict)
    dispatched_by_task_role: dict[str, dict[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "aggregate_limit",
            "task_limit",
            "author_limit",
            "review_limit",
            "total_dispatched",
        ):
            _validate_nonnegative_integer(name, getattr(self, name))
        if not isinstance(self.dispatched_by_task, dict):
            raise ValueError("dispatched_by_task must be a mapping")
        if not isinstance(self.dispatched_by_task_role, dict):
            raise ValueError("dispatched_by_task_role must be a mapping")
        for task_id, count in self.dispatched_by_task.items():
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("dispatched_by_task keys must be nonblank strings")
            _validate_nonnegative_integer(f"dispatched_by_task[{task_id!r}]", count)
        for task_id, roles in self.dispatched_by_task_role.items():
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("dispatched_by_task_role keys must be nonblank strings")
            if not isinstance(roles, dict):
                raise ValueError(f"dispatched_by_task_role[{task_id!r}] must be a mapping")
            for role, count in roles.items():
                if role not in {"author", "reviewer"}:
                    raise ValueError(
                        f"dispatched_by_task_role[{task_id!r}] has unsupported role {role!r}"
                    )
                _validate_nonnegative_integer(
                    f"dispatched_by_task_role[{task_id!r}][{role!r}]",
                    count,
                )

    @classmethod
    def from_prior_spend(
        cls,
        *,
        task_id: str,
        prior_author_correction_spend: int = 0,
        prior_review_spend: int = 0,
        aggregate_limit: int = MAX_AUTHORING_REQUESTS,
        task_limit: int = MAX_REQUESTS_PER_TASK,
        author_limit: int = MAX_AUTHOR_CORRECTION_REQUESTS_PER_TASK,
        review_limit: int = MAX_REVIEW_REQUESTS_PER_TASK,
        author_limit_increment: int = 0,
        review_limit_increment: int = 0,
    ) -> AuthoringBudget:
        """Create a guard seeded with caller-supplied spend for one task."""

        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a nonblank string")
        _validate_nonnegative_integer(
            "prior_author_correction_spend",
            prior_author_correction_spend,
        )
        _validate_nonnegative_integer("prior_review_spend", prior_review_spend)
        _validate_nonnegative_integer("author_limit_increment", author_limit_increment)
        _validate_nonnegative_integer("review_limit_increment", review_limit_increment)
        total = prior_author_correction_spend + prior_review_spend
        return cls(
            aggregate_limit=aggregate_limit,
            task_limit=task_limit,
            author_limit=author_limit + author_limit_increment,
            review_limit=review_limit + review_limit_increment,
            total_dispatched=total,
            dispatched_by_task={task_id: total},
            dispatched_by_task_role={
                task_id: {
                    "author": prior_author_correction_spend,
                    "reviewer": prior_review_spend,
                }
            },
        )

    def seed_prior_spend(
        self,
        *,
        task_id: str,
        prior_author_correction_spend: int = 0,
        prior_review_spend: int = 0,
        author_limit_increment: int = 0,
        review_limit_increment: int = 0,
    ) -> None:
        """Add caller-supplied prior spend before the first dispatch."""

        seeded = self.from_prior_spend(
            task_id=task_id,
            prior_author_correction_spend=prior_author_correction_spend,
            prior_review_spend=prior_review_spend,
            aggregate_limit=self.aggregate_limit,
            task_limit=self.task_limit,
            author_limit=self.author_limit,
            review_limit=self.review_limit,
            author_limit_increment=author_limit_increment,
            review_limit_increment=review_limit_increment,
        )
        self.total_dispatched += seeded.total_dispatched
        self.dispatched_by_task[task_id] = (
            self.dispatched_by_task.get(task_id, 0) + seeded.dispatched_by_task[task_id]
        )
        current_roles = self.dispatched_by_task_role.setdefault(task_id, {})
        for role, count in seeded.dispatched_by_task_role[task_id].items():
            current_roles[role] = current_roles.get(role, 0) + count
        self.author_limit = seeded.author_limit
        self.review_limit = seeded.review_limit

    def reserve(self, task_id: str, *, role: str = "author") -> int:
        if role not in {"author", "reviewer"}:
            raise ValueError(f"unsupported budget role: {role}")
        if self.total_dispatched >= self.aggregate_limit:
            raise BudgetExceeded(
                "aggregate authoring budget exhausted",
                scope="aggregate",
                task_id=task_id,
                role=role,
                used=self.total_dispatched,
                limit=self.aggregate_limit,
            )
        used = self.dispatched_by_task.get(task_id, 0)
        if used >= self.task_limit:
            raise BudgetExceeded(
                f"per-task authoring budget exhausted: {task_id}",
                scope="task",
                task_id=task_id,
                role=role,
                used=used,
                limit=self.task_limit,
            )
        roles = self.dispatched_by_task_role.get(task_id, {})
        role_used = roles.get(role, 0)
        role_limit = self.author_limit if role == "author" else self.review_limit
        if role_used >= role_limit:
            role_name = "author/correction" if role == "author" else "review"
            raise BudgetExceeded(
                f"per-task {role_name} budget exhausted: {task_id}",
                scope=role,
                task_id=task_id,
                role=role,
                used=role_used,
                limit=role_limit,
            )
        self.total_dispatched += 1
        self.dispatched_by_task[task_id] = used + 1
        self.dispatched_by_task_role.setdefault(task_id, {})[role] = role_used + 1
        return self.total_dispatched

    def snapshot(self, task_id: str) -> dict[str, Any]:
        """Return redacted accounting state for evidence and package metadata."""

        task_spent = self.dispatched_by_task.get(task_id, 0)
        roles = self.dispatched_by_task_role.get(task_id, {})
        author_spent = roles.get("author", 0)
        review_spent = roles.get("reviewer", 0)
        return {
            "aggregate_limit": self.aggregate_limit,
            "aggregate_spent": self.total_dispatched,
            "aggregate_remaining": max(self.aggregate_limit - self.total_dispatched, 0),
            "task_limit": self.task_limit,
            "task_spent": task_spent,
            "task_remaining": max(self.task_limit - task_spent, 0),
            "author_correction_limit": self.author_limit,
            "author_correction_spent": author_spent,
            "author_correction_remaining": max(self.author_limit - author_spent, 0),
            "review_limit": self.review_limit,
            "review_spent": review_spent,
            "review_remaining": max(self.review_limit - review_spent, 0),
            "task_id": task_id,
        }


_UNSET_CORRECTIONS = object()


def _validate_nonnegative_integer(name: str, value: Any) -> None:
    """Reject booleans and other nonnegative-integer budget inputs."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer, got {value!r}")


@dataclass(frozen=True)
class AuthoringPolicy:
    """Stage-local correction allowances and review switches for one task.

    ``plan_max_corrections`` and ``artifact_max_corrections`` each default to
    one and accept only nonnegative integers; booleans, negatives, and other
    types are rejected and values are never clamped.  ``review_plan`` and
    ``review_artifact`` default to enabled and are validated independently.
    Set a stage limit to zero to disable corrections for that stage.
    """

    plan_max_corrections: Any = _UNSET_CORRECTIONS
    artifact_max_corrections: Any = _UNSET_CORRECTIONS
    review_plan: bool = True
    review_artifact: bool = True
    review_model_profile: str | None = None

    def __post_init__(self) -> None:
        for name in ("review_plan", "review_artifact"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if self.review_model_profile is not None and (
            not isinstance(self.review_model_profile, str) or not self.review_model_profile.strip()
        ):
            raise ValueError("review_model_profile must be a nonblank string when provided")
        explicit: dict[str, int] = {}
        for name in ("plan_max_corrections", "artifact_max_corrections"):
            value = getattr(self, name)
            if value is _UNSET_CORRECTIONS:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer, got {value!r}")
            explicit[name] = value
        object.__setattr__(self, "plan_max_corrections", explicit.get("plan_max_corrections", 1))
        object.__setattr__(
            self, "artifact_max_corrections", explicit.get("artifact_max_corrections", 1)
        )

    @classmethod
    def from_cli(
        cls,
        *,
        plan_max_corrections: int | None = None,
        artifact_max_corrections: int | None = None,
        review_plan: bool = True,
        review_artifact: bool = True,
        review_model_profile: str | None = None,
    ) -> AuthoringPolicy:
        """Build one policy from optional CLI values; ``None`` keeps defaults."""

        kwargs: dict[str, Any] = {
            "review_plan": review_plan,
            "review_artifact": review_artifact,
            "review_model_profile": review_model_profile,
        }
        if plan_max_corrections is not None:
            kwargs["plan_max_corrections"] = plan_max_corrections
        if artifact_max_corrections is not None:
            kwargs["artifact_max_corrections"] = artifact_max_corrections
        return cls(**kwargs)


def _stage_author_dispatches(corrections: int, reviewed: bool) -> int:
    revisions = REVIEW_REVISION_ALLOWANCE_PER_STAGE if reviewed else 0
    return corrections + 1 + revisions


def policy_role_limits(policy: AuthoringPolicy) -> dict[str, int]:
    """Return the closed worst-case author and review dispatches for one policy.

    A stage's author responses are its initial attempt, its corrections, and,
    when the stage is reviewed, its review revisions.  Each author response
    can cost at most one review.
    """

    plan_author = _stage_author_dispatches(policy.plan_max_corrections, policy.review_plan)
    artifact_author = _stage_author_dispatches(
        policy.artifact_max_corrections, policy.review_artifact
    )
    return {
        "author": plan_author + artifact_author,
        "reviewer": (plan_author if policy.review_plan else 0)
        + (artifact_author if policy.review_artifact else 0),
    }


def policy_max_dispatches(policy: AuthoringPolicy) -> int:
    """Return the closed worst-case dispatch count one policy can spend.

    A reviewed stage costs its initial attempt, its corrections, and its
    review revisions, and each of those author responses can also cost one
    review; an unreviewed stage costs at most ``corrections + 1``.  These are
    upper bounds used for default budget guards, not spending targets.
    """

    limits = policy_role_limits(policy)
    return limits["author"] + limits["reviewer"]


@dataclass
class AuthoringResult:
    """Outcome and retained evidence from one bounded authoring task."""

    status: str
    task_id: str
    plan: dict[str, Any] | None = None
    artifact: dict[str, Any] | None = None
    package: ArtifactPackage | None = None
    package_path: Path | None = None
    findings: list[Finding] = field(default_factory=list)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    transformations: list[Any] = field(default_factory=list)
    raw_responses: dict[str, bytes] = field(default_factory=dict)
    decoded_responses: dict[str, Any] = field(default_factory=dict)
    prompts: dict[str, PromptPacket] = field(default_factory=dict)
    failure_evidence_path: Path | None = None
    review_status: dict[str, str] = field(default_factory=dict)
    allowances: dict[str, int] = field(default_factory=dict)
    review_reuse: dict[str, str] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    review_revision_allowances: dict[str, int] = field(default_factory=dict)


def _render_correction_packet(correction_context: dict[str, Any]) -> PromptPacket:
    """Render one shared correction prompt for every artifact caller."""

    source_original_context = correction_context.get("original_context")
    fact_ref_guidance = (
        deepcopy(source_original_context.get("semantic_judge_fact_ref_guidance"))
        if isinstance(source_original_context, dict)
        and isinstance(source_original_context.get("semantic_judge_fact_ref_guidance"), dict)
        else None
    )
    if fact_ref_guidance is None and isinstance(source_original_context, dict):
        authoritative = source_original_context.get("authoritative_context")
        facts = authoritative.get("facts") if isinstance(authoritative, dict) else None
        if isinstance(facts, list):
            fact_ref_guidance = _semantic_judge_fact_ref_guidance({"facts": facts})
    original_context = _correction_prompt_context(correction_context["original_context"])
    plan_field_meanings = original_context.pop("plan_field_meanings", None)
    neutral_outcome_example = original_context.pop("neutral_outcome_example", None)
    original_context.pop("semantic_judge_fact_ref_guidance", None)
    owner_scope = original_context.pop("owner_scope", None)
    observation_guide = original_context.pop(
        "observation_guide", correction_context.get("observation_guide")
    )
    accepted_plan = correction_context.get("original_context", {}).get("accepted_plan")
    runtime_evidence_interface = correction_context.get("original_context", {}).get(
        "runtime_evidence_interface"
    )
    runtime_contract = (
        runtime_evidence_interface.get("runtime_contract")
        if isinstance(runtime_evidence_interface, dict)
        else None
    )
    if correction_context.get("stage") == "artifact" and isinstance(runtime_contract, dict):
        observation = runtime_contract.get("observation")
        if isinstance(observation, dict):
            required_keys = _required_observation_keys(accepted_plan)
            runtime_observation = _matching_runtime_observations(
                required_keys,
                observation,
            )
            original_context["runtime_contract"] = {
                "observation": runtime_observation,
            }
    if correction_context.get("stage") == "artifact" and not isinstance(observation_guide, dict):
        observation_guide = artifact_observation_guide(
            accepted_plan if isinstance(accepted_plan, dict) else {},
            runtime_contract if isinstance(runtime_contract, dict) else None,
            omission=_context_has_not_called(correction_context.get("original_context")),
        )
    sections: list[tuple[str, Any]] = [
        (
            "FAILED STAGE",
            {
                "stage": correction_context["stage"],
                "failed_stage": correction_context["failed_stage"],
            },
        ),
    ]
    if (
        isinstance(accepted_plan, dict)
        and isinstance(accepted_plan.get("semantic_judge"), dict)
        and accepted_plan["semantic_judge"].get("needed") is False
    ):
        sections.append(
            (
                "FIXED PLAN DECISION",
                "The accepted plan requires no semantic judge. semantic_judge_spec must be null. "
                "This decision is fixed; correct the detector within it.",
            )
        )
    sections.append(("ORIGINAL STAGE CONTEXT", original_context))
    if owner_scope is not None:
        sections.append((_OWNER_SCOPE_SECTION_TITLE, owner_scope))
    supplied_stage_context = correction_context.get("supplied_stage_context")
    if supplied_stage_context is not None:
        sections.append(("SUPPLIED STAGE CONTEXT", supplied_stage_context))
    if isinstance(observation_guide, dict):
        sections.append(("OBSERVATION DECISION GUIDE", observation_guide))
    if isinstance(plan_field_meanings, str) and (
        correction_context.get("stage") != "artifact"
        or correction_context.get("detector_feedback") is None
    ):
        sections.append(("PLAN FIELD MEANINGS", plan_field_meanings))
    if isinstance(neutral_outcome_example, str):
        sections.append(("NEUTRAL OUTCOME EXAMPLE", neutral_outcome_example))
    unknown_fact_ref_findings = [
        finding
        for finding in correction_context.get("findings", [])
        if isinstance(finding, dict)
        and finding.get("code") == "unknown_reference"
        and str(finding.get("path", "")).startswith("semantic_judge_spec.fact_refs[")
    ]
    if (
        correction_context.get("stage") == "artifact"
        and unknown_fact_ref_findings
        and isinstance(fact_ref_guidance, dict)
    ):
        sections.append(
            (
                "SEMANTIC JUDGE FACT REFERENCE GUIDANCE",
                {
                    **fact_ref_guidance,
                    "triggered_findings": unknown_fact_ref_findings,
                },
            )
        )
    if correction_context.get("stage") == "artifact":
        evidence_interface = correction_context.get(
            "evidence_packet_interface",
            _render_evidence_packet_interface(
                claim_level=_plan_claim_level(accepted_plan),
                required_observations=(
                    accepted_plan.get("required_observations")
                    if isinstance(accepted_plan, dict)
                    else None
                ),
                semantic_judge_needed=_plan_semantic_judge_needed(accepted_plan),
            ),
        )
        if correction_context.get("detector_feedback") and isinstance(evidence_interface, str):
            # Exact control packets already demonstrate the input shape. Keep
            # the path/result contract, without a second unrelated input example.
            interface_view = json.loads(evidence_interface)
            interface_view.pop("full_example", None)
            interface_view.pop("full_example_label", None)
            evidence_interface = _canonical_json(interface_view)
        sections.append(
            (
                "RUNTIME EVIDENCE INTERFACE",
                evidence_interface,
            )
        )
    binding_repair_options = _binding_repair_options_for_correction(correction_context)
    sections.extend(
        (
            (
                "RESPONSE CONTRACT",
                (
                    _artifact_response_contract_for_prompt(
                        correction_context["response_contract"],
                        correction=True,
                    )
                    if correction_context.get("stage") == "artifact"
                    else correction_context["response_contract"]
                ),
            ),
            (
                "CURRENT OUTPUT",
                _correction_current_output_view(
                    correction_context["current_output"],
                    artifact=correction_context.get("stage") == "artifact",
                ),
            ),
            ("CURRENT FINDINGS", _correction_findings_view(correction_context["findings"])),
        )
    )
    reference_repair_options = _reference_repair_options_for_correction(correction_context)
    if reference_repair_options is not None:
        sections.append(("REFERENCE REPAIR OPTIONS", reference_repair_options))
    if binding_repair_options is not None:
        sections.append(
            (
                "BINDING REPAIR OPTION FIELDS",
                {
                    "description": binding_repair_options["description"],
                    "field_descriptions": binding_repair_options["field_descriptions"],
                },
            )
        )
        sections.append(
            (
                "BINDING REPAIR OPTIONS",
                {"options": binding_repair_options["options"]},
            )
        )
    if correction_context.get("detector_feedback") is not None:
        sections.append(
            (
                "DETECTOR CONTROL FEEDBACK",
                _correction_detector_feedback_view(correction_context["detector_feedback"]),
            )
        )
    correction_instruction = correction_context["instruction"]
    if correction_context.get("stage") == "artifact":
        correction_instruction += " " + _CURRENT_ARTIFACT_CORRECTION_GUIDANCE
    sections.append(
        (
            "CORRECTION INSTRUCTIONS",
            {
                "instruction": correction_instruction,
                "format": correction_context["format"],
                "accepted_plan_fixed": correction_context["accepted_plan_fixed"],
            },
        )
    )
    if "prior_unresolved_findings" in correction_context:
        sections.append(
            (
                "PRIOR UNRESOLVED FINDINGS",
                correction_context["prior_unresolved_findings"],
            )
        )
    payload = deepcopy(correction_context)
    if binding_repair_options is not None:
        payload["binding_repair_options"] = binding_repair_options
    if reference_repair_options is not None:
        payload["reference_repair_options"] = reference_repair_options
    packet = PromptPacket(
        stage="correction",
        version=(
            CORRECTION_PROMPT_VERSION_V25
            if correction_context.get("stage") == "artifact"
            else CORRECTION_PROMPT_VERSION_V27
        ),
        system=_CORRECTION_SYSTEM_V5,
        user=_render_correction_sections(tuple(sections)),
        payload=payload,
    )
    return packet


def _correction_detector_feedback_view(value: Any) -> Any:
    """Render exact failed control inputs/results without redundant wrappers.

    Each entry carries a code-derived ``input_shapes`` type summary of the
    failed control's inputs.
    """

    if not isinstance(value, dict):
        return value
    failed = value.get("failed_controls")
    if not isinstance(failed, list):
        return value
    rendered: list[dict[str, Any]] = []
    for item in failed:
        if not isinstance(item, dict):
            continue
        actual = item.get("actual_result")
        if actual is None and isinstance(item.get("error"), str):
            outcome_class = item.get("outcome_class")
            error = item["error"]
            if "timeout" in error.casefold():
                actual = {"timeout": error}
            elif outcome_class == "detector_exception":
                actual = {"exception": error}
            elif outcome_class == "invalid_returned_result":
                actual = {"invalid_result": error}
            else:
                actual = {"pre_result_failure": error}
        evidence = item.get("evidence")
        entry: dict[str, Any] = {"name": item.get("name"), "input": evidence}
        entry["input_shapes"] = (
            describe_input_shapes(evidence) if isinstance(evidence, dict) else {}
        )
        entry.update(
            {
                "expected": {
                    "outcome": item.get("expected_outcome"),
                    "claim_level": item.get("expected_claim_level"),
                },
                "actual": actual,
                "explanation": _compact_feedback_explanation(item),
            }
        )
        rendered.append(entry)
    passing = value.get("passing_controls")
    passing_rendered: list[dict[str, Any]] = []
    if isinstance(passing, list):
        for item in passing:
            if not isinstance(item, dict):
                continue
            passing_rendered.append(
                {
                    "name": item.get("name"),
                    "outcome": item.get("observed_outcome"),
                }
            )
    return {
        "failed_controls": rendered,
        "passing_controls": passing_rendered,
        "correction_guidance": value.get("correction_guidance"),
    }


def _compact_feedback_explanation(item: dict[str, Any]) -> str:
    """Keep each feedback explanation explicit without repeating result fields."""

    outcome_class = item.get("outcome_class")
    error = item.get("error")
    actual_result = item.get("actual_result")
    actual_outcome = item.get("actual_outcome")
    if not isinstance(actual_outcome, str) and isinstance(actual_result, dict):
        actual_outcome = actual_result.get("outcome")
    expected_outcome = item.get("expected_outcome")
    actual_claim_level = item.get("actual_claim_level")
    if not isinstance(actual_claim_level, str) and isinstance(actual_result, dict):
        actual_claim_level = actual_result.get("claim_level")
    expected_claim_level = item.get("expected_claim_level")
    if outcome_class == "structurally_valid_wrong_outcome":
        if actual_outcome != expected_outcome:
            return f"returned outcome {actual_outcome!r}; expected outcome {expected_outcome!r}"
        if actual_claim_level != expected_claim_level:
            return (
                f"returned claim level {actual_claim_level!r}; "
                f"expected claim level {expected_claim_level!r}"
            )
        return "returned result differs from the expected control result"
    if isinstance(error, str) and "timeout" in error.casefold():
        return "detector timed out before returning a result"
    if outcome_class == "detector_exception":
        return "detector raised an exception before returning a result"
    if outcome_class == "invalid_returned_result":
        verdict_note = (
            f" Its outcome {actual_outcome!r} also differs from expected {expected_outcome!r}."
            if actual_outcome is not None and actual_outcome != expected_outcome
            else ""
        )
        return f"Runtime rejected the unvalidated return: {error}." + verdict_note
    if outcome_class == "container/evaluator_failure_before_result":
        return "container or evaluator failed before exposing a result"
    runtime_explanation = item.get("runtime_contract_explanation")
    if isinstance(runtime_explanation, str) and runtime_explanation:
        return runtime_explanation
    return "returned outcome or claim level differs from the expected control result"


def _correction_findings_view(value: Any) -> Any:
    """Avoid repeating full control details beside exact feedback packets."""

    if not isinstance(value, list):
        return value
    result: list[Any] = []
    for item in value:
        if not isinstance(item, dict):
            result.append(item)
            continue
        path = item.get("path", "")
        if isinstance(path, str) and path.startswith("detector_controls."):
            continue
        if item.get("code") == "semantic_review" and "details" in item:
            # The review location and required change already appear in detail.
            item = {key: value for key, value in item.items() if key != "details"}
        result.append(item)
    return result


def _correction_current_output_view(value: Any, *, artifact: bool) -> Any:
    """Canonicalize artifact framing while retaining exact metadata and source."""

    if not artifact or not isinstance(value, str):
        return value
    try:
        parsed = parse_call2_response(value.encode("utf-8"))
    except (Call2FramingError, UnicodeDecodeError, ValueError):
        return value
    return (
        "```json\n"
        + _canonical_json(parsed.metadata)
        + "\n```\n```python\n"
        + parsed.python_source
        + "```\n"
    )


def _correction_prompt_context(context: dict[str, Any]) -> dict[str, Any]:
    """Keep correction context authoritative without replaying authoring payloads."""

    result = deepcopy(context)
    if "scenario_design" in result:
        result["scenario_design"] = _scenario_design_prompt_view(result["scenario_design"])
    authoritative = result.get("authoritative_context")
    interface = result.get("runtime_evidence_interface")
    if isinstance(authoritative, dict) and isinstance(interface, dict):
        if isinstance(interface.get("runtime_contract"), dict):
            authoritative.pop("runtime_capabilities", None)
        interface.pop("evidence_packet", None)
        _scope_correction_authoritative_context(
            authoritative,
            result.get("accepted_plan"),
        )
    response_contract = result.get("response_contract")
    if isinstance(response_contract, dict):
        response_contract.pop("evidence_packet", None)
        response_contract.pop("neutral_example", None)
    result.pop("evidence_packet_interface", None)
    result.pop("runtime_evidence_interface", None)
    result.pop("response_contract", None)
    result.pop("neutral_example", None)
    return result


def _scope_correction_authoritative_context(
    authoritative: dict[str, Any],
    accepted_plan: Any,
) -> None:
    """Drop source records unrelated to the fixed correction plan."""

    if not isinstance(accepted_plan, dict):
        return
    plan_text = _canonical_json(accepted_plan)
    operations = authoritative.get("operations")
    if isinstance(operations, list):
        authoritative["operations"] = [
            value
            for value in operations
            if isinstance(value, dict)
            and isinstance(value.get("name"), str)
            and value["name"] in plan_text
        ]
    authoritative.pop("facts", None)
    authoritative.pop("source_handles", None)
    authoritative.pop("runtime_capabilities", None)
    operations = authoritative.get("operations")
    if isinstance(operations, list):
        authoritative["operations"] = [
            {
                "name": item.get("name"),
                "description": item.get("description"),
            }
            for item in operations
            if isinstance(item, dict)
        ]


_BINDING_REPAIR_SELECTOR_LIMIT = 40
_BINDING_SELECTOR_FINDING_PATH = re.compile(r"^runtime_bindings\[(\d+)\]\.selector$")
_BINDING_SOURCE_REF_FINDING_PATH = re.compile(r"^runtime_bindings\[(\d+)\]\.source_ref$")
_BINDING_SOURCE_KIND_FINDING_PATH = re.compile(r"^runtime_bindings\[(\d+)\]\.source_kind$")
_UNKNOWN_BINDING_FINDING_PATH = re.compile(r"^prerequisites\[(\d+)\]\.binding$")
_BINDING_REPAIR_OPTIONS_DESCRIPTION = (
    "binding_repair_options is deterministic, source-derived assistance for binding "
    "findings. It is prompt context, not a new response field and not a recommended "
    "repair. Each option lists the exact source, selector, declaration, or consumer "
    "choices that the current validator accepts; choose among them using the scenario "
    "meaning and preserve supported content. If no listed option fits the scenario, "
    "the RESPONSE CONTRACT empty_value_guidance entries still apply. An equals value "
    "remains a JSON literal and still requires a declared binding."
)
_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS_V9 = {
    "description": (
        "Explains that this object is deterministic correction context, not a response "
        "field or a recommended repair."
    ),
    "field_descriptions": (
        "Maps each binding_repair_options field name to its model-facing meaning."
    ),
    "options": ("One deterministic entry for each binding-related finding that can be assisted."),
    "code": "The existing finding code; it does not change the finding or validation rules.",
    "path": "The exact existing finding path in the candidate plan.",
    "kind": ("The repair category: selector, unknown_binding, or consumer_mismatch."),
    "binding_name": "The candidate runtime binding or prerequisite binding name.",
    "expected_type": (
        "The candidate binding expected_type used for selector compatibility checks."
    ),
    "source_kind": ("The candidate binding source kind: supplied_input or setup_output."),
    "source_ref": "The candidate binding source_ref, when one is present.",
    "resolved_source": (
        "Whether source_kind and source_ref resolve to a documented source under the "
        "current inventory and runtime contract."
    ),
    "source_schema_type": (
        "The JSON type at the resolved source root, when the source schema declares one."
    ),
    "documented_selectors": (
        "A path-to-JSON-type mapping. Paths are the exact documented selectors accepted "
        "by the current selector validator, rooted at value for supplied_input or result "
        "for setup_output."
    ),
    "matching_expected_type": (
        "The documented selector paths whose JSON types are compatible with "
        "expected_type; integer is compatible with expected number."
    ),
    "no_matching_selector_note": (
        "Explicitly states that no enumerated documented selector produces the "
        "candidate expected type."
    ),
    "available_source_ref_forms": (
        "The valid facts:<ref> and permitted setup:<operation> source_ref forms when "
        "the candidate source does not resolve."
    ),
    "available_source_ref_forms_truncated": (
        "Whether the available source_ref form list was capped at the deterministic "
        "enumeration limit."
    ),
    "declared_binding_names": (
        "The currently declared runtime binding names, sorted deterministically; an "
        "empty list means no bindings are declared."
    ),
    "declared_binding_names_truncated": (
        "Whether the declared binding name list was capped at the deterministic enumeration limit."
    ),
    "declaration_requirement": (
        "The exact requirement for repairing unknown_binding: a runtime_bindings entry "
        "with the named binding whose consumers include prerequisites.<name>."
    ),
    "evidence_ref_sources": (
        "For each evidence_refs entry that exactly names a supplied fact, the "
        "corresponding facts:<ref> source and its documented selectors and JSON types."
    ),
    "evidence_ref_sources_truncated": (
        "Whether the supplied fact source list was capped at the deterministic enumeration limit."
    ),
    "evidence_ref": "The supplied fact reference copied from the prerequisite evidence_refs.",
    "required_consumer": ("The exact consumer path that the binding declaration must include."),
    "consumer_requirement": (
        "The exact action needed to repair consumer_mismatch without changing the consumer rule."
    ),
    "truncated": (
        "Whether the selector mapping was capped at the deterministic enumeration limit."
    ),
    "truncation_note": (
        "An explicit note when a deterministic enumeration exceeds 40 entries and "
        "only the first 40 sorted entries are shown."
    ),
}
_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS = {
    # v10 lists unresolved sources in source options, never as bare source_ref forms.
    **{
        name: meaning
        for name, meaning in _BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS_V9.items()
        if name not in {"available_source_ref_forms", "available_source_ref_forms_truncated"}
    },
    "findings": (
        "The grouped existing finding code and exact path entries for this binding, "
        "in finding order."
    ),
    "kind": ("The repair category: source, selector, unknown_binding, or consumer_mismatch."),
    "referenced_fact_sources": (
        "Bindable supplied fact sources that the candidate plan cites by exact "
        "facts:<ref> or <ref> value, with their documented selectors."
    ),
    "referenced_fact_sources_truncated": (
        "Whether the referenced supplied fact source list was capped at 40 entries."
    ),
    "permitted_setup_sources": (
        "Bindable setup operation results permitted by the runtime contract, with "
        "result-rooted documented selectors; an empty list means no setup operation "
        "is permitted."
    ),
    "permitted_setup_sources_truncated": (
        "Whether the permitted setup source list was capped at 40 entries."
    ),
    "other_fact_source_refs": (
        "Bindable supplied fact source names not cited by exact reference in the "
        "candidate plan, sorted and capped at 40."
    ),
    "other_fact_source_refs_truncated": (
        "Whether the other supplied fact source name list was capped at 40 entries."
    ),
    "supplied_value_empty": (
        "Present and true when the supplied fact value is an empty list or empty "
        "object; the source then contains nothing to bind."
    ),
    "supplied_value_empty_note": ("Names the empty supplied fact source and its empty shape."),
    "named_record_key": (
        "Present when source_ref names one record of a keyed supplied fact, as in "
        "facts:<ref>:<record_key>; the record key it names."
    ),
    "named_record_sources": (
        "Each supplied fact that documents the named record, listing only that record's "
        "selectors written in full from value; each source_ref and selector pair is "
        "accepted as written. A facts:<ref>:records source holds the record key itself "
        "at value.<record_key>.record_key."
    ),
}
_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS_V26 = {
    **_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS,
    "code": (
        "The existing finding code; it does not change the finding or validation rules. "
        "semantic_review marks a binding that a semantic review finding concerns."
    ),
    "path": (
        "The exact existing finding path in the candidate plan; for a review_binding "
        "option, the runtime binding that the review finding concerns."
    ),
    "kind": (
        "The repair category: source, selector, review_binding, unknown_binding, or "
        "consumer_mismatch. review_binding lists the documented choices for a binding "
        "that a semantic review finding concerns; when the binding selects inside one "
        "keyed record, it lists only that record's sources in named_record_sources."
    ),
    "selector": "The candidate binding selector, as written.",
    "named_record_key": (
        "Present when source_ref names one record of a keyed supplied fact, as in "
        "facts:<ref>:<record_key>, or when the selector selects inside one such "
        "record, as in value.<record_key>.<field>; the record key it names."
    ),
    "review_selector_checks": (
        "Each selector path that the review finding's required change names, checked "
        "by code against the documented selectors. documented_on_binding_source states "
        "whether the binding's current source_ref documents that selector; "
        "documented_source_refs lists every source_ref that documents it. A selector is "
        "valid only together with a listed source_ref; an empty list means no supplied "
        "source documents it."
    ),
}
_REVIEW_BINDING_LOCATION = re.compile(r"^(?:candidate_plan\.|plan\.)?runtime_bindings\[(\d+)\]")
_REVIEW_SELECTOR_TOKEN = re.compile(r"(?<![\w.:-])((?:value|result)(?:\.[A-Za-z0-9_-]+)+)")


def _correction_plan_candidate(correction_context: dict[str, Any]) -> dict[str, Any] | None:
    """Decode the failed plan only for deterministic correction assistance."""

    candidate = correction_context.get("current_output")
    if isinstance(candidate, dict):
        return deepcopy(candidate)
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    try:
        decoded, _ = _decode_v2_json_response(candidate.encode("utf-8"))
    except (Call1FramingError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _correction_binding_inputs(
    correction_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recover the source inventory and runtime contract from the author context."""

    original_context = correction_context.get("original_context")
    if not isinstance(original_context, dict):
        return {}, {}
    source_context = original_context.get("source_context")
    if not isinstance(source_context, dict):
        source_context = original_context.get("authoritative_context")
    if not isinstance(source_context, dict):
        source_context = {}
    inventory = {
        "facts": deepcopy(source_context.get("facts", [])),
        "operations": deepcopy(source_context.get("operations", [])),
    }
    execution_capabilities = original_context.get("execution_capabilities")
    runtime_contract = (
        execution_capabilities.get("runtime_contract")
        if isinstance(execution_capabilities, dict)
        else None
    )
    if not isinstance(runtime_contract, dict):
        runtime_contract = source_context.get("runtime_capabilities")
    return inventory, deepcopy(runtime_contract) if isinstance(runtime_contract, dict) else {}


def _documented_binding_selectors(
    schema: dict[str, Any],
    *,
    root: str,
    limit: int = _BINDING_REPAIR_SELECTOR_LIMIT,
) -> tuple[dict[str, str], bool]:
    """Enumerate the selectors accepted by ``_binding_selector_type``."""

    discovered: dict[str, str] = {}

    def visit(current: Any, path: str) -> None:
        if len(discovered) >= limit:
            return
        if not isinstance(current, dict):
            return
        current_type = current.get("type")
        if not isinstance(current_type, str):
            return
        discovered[path] = current_type
        if current_type == "object":
            properties = current.get("properties")
            if isinstance(properties, dict):
                for property_name in sorted(properties):
                    visit(properties[property_name], f"{path}.{property_name}")
        elif current_type == "array":
            visit(current.get("items"), f"{path}.items")

    visit(schema, root)
    has_more = False

    def count_paths(current: Any) -> int:
        if not isinstance(current, dict) or not isinstance(current.get("type"), str):
            return 0
        count = 1
        if current["type"] == "object" and isinstance(current.get("properties"), dict):
            count += sum(
                count_paths(current.get("properties", {}).get(name))
                for name in current["properties"]
            )
        elif current["type"] == "array":
            count += count_paths(current.get("items"))
        return count

    has_more = count_paths(schema) > limit
    return discovered, has_more


def _available_binding_source_refs(
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> tuple[list[str], bool]:
    """Return valid source_ref forms in stable lexical order."""

    source_refs = {
        f"facts:{fact['ref']}"
        for fact in inventory.get("facts", [])
        if (
            isinstance(fact, dict)
            and isinstance(fact.get("ref"), str)
            and fact["ref"]
            and isinstance(fact.get("schema"), dict)
        )
    }
    permitted = runtime_contract.get("setup_permissions", [])
    permitted_names = set(permitted) if isinstance(permitted, list) else set()
    source_refs.update(
        f"setup:{operation['name']}"
        for operation in inventory.get("operations", [])
        if (
            isinstance(operation, dict)
            and isinstance(operation.get("name"), str)
            and operation["name"] in permitted_names
            and isinstance(operation.get("result_schema"), dict)
        )
    )
    ordered = sorted(source_refs)
    return ordered[:_BINDING_REPAIR_SELECTOR_LIMIT], len(ordered) > _BINDING_REPAIR_SELECTOR_LIMIT


def _repair_truncation_note(label: str) -> str:
    return (
        f"{label} enumeration truncated after {_BINDING_REPAIR_SELECTOR_LIMIT} entries; "
        f"only the first {_BINDING_REPAIR_SELECTOR_LIMIT} sorted entries are shown."
    )


def _repair_selector_option(
    finding: Finding | dict[str, Any],
    binding: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    selected_record: bool = False,
) -> dict[str, Any]:
    """Build selector repair choices from the exact source schema.

    ``selected_record`` also lists the named record sources when the selector,
    rather than source_ref, names one keyed record.
    """

    code = finding.code if isinstance(finding, Finding) else finding.get("code")
    path = finding.path if isinstance(finding, Finding) else finding.get("path", "")
    name = binding.get("name")
    source_kind = binding.get("source_kind")
    source_ref = binding.get("source_ref")
    expected_type = binding.get("expected_type")
    option: dict[str, Any] = {
        "code": code,
        "path": path,
        "kind": "selector",
        "binding_name": name,
        "expected_type": expected_type,
        "source_kind": source_kind,
        "source_ref": source_ref,
    }
    schema = None
    if isinstance(source_kind, str) and isinstance(source_ref, str):
        schema, _ = _binding_source_schema(
            source_kind,
            source_ref,
            inventory,
            runtime_contract,
            name,
        )
    if schema is None:
        available_sources, available_sources_truncated = _available_binding_source_refs(
            inventory, runtime_contract
        )
        option.update(
            {
                "resolved_source": False,
                "available_source_ref_forms": available_sources,
                "available_source_ref_forms_truncated": available_sources_truncated,
            }
        )
        if available_sources_truncated:
            option["truncation_note"] = _repair_truncation_note("source_ref form")
        return option

    root = "value" if source_kind == "supplied_input" else "result"
    selectors, matching, truncated = _binding_selector_details(
        schema,
        root=root,
        expected_type=expected_type,
    )
    option.update(
        {
            "resolved_source": True,
            "source_schema_type": schema.get("type"),
            "documented_selectors": selectors,
            "matching_expected_type": matching,
            "truncated": truncated,
        }
    )
    option.update(_supplied_value_empty_fields(source_kind, source_ref, inventory))
    named_fields = _named_record_source_fields(source_kind, source_ref, expected_type, inventory)
    if not named_fields and selected_record:
        named_fields = _selected_record_source_fields(
            source_kind, source_ref, binding.get("selector"), expected_type, inventory
        )
    option.update(named_fields)
    if truncated:
        option["truncation_note"] = (
            "Documented selector enumeration truncated after "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} selectors; only the first "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} sorted paths are shown."
        )
    if not matching:
        option["no_matching_selector_note"] = (
            f"No documented selector of source {source_ref} yields expected_type {expected_type}."
        )
    return option


def _supplied_value_empty_fields(
    source_kind: Any,
    source_ref: Any,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """Mark a supplied fact whose captured value is an empty list or object.

    Its schema can still document ``value``, so the selector list alone does
    not show that the source holds nothing to bind.
    """

    if source_kind != "supplied_input" or not isinstance(source_ref, str):
        return {}
    canonical_ref, _ = canonical_binding_paths(source_kind, source_ref, "value", inventory)
    reference = canonical_ref.removeprefix("facts:")
    fact = next(
        (
            item
            for item in inventory.get("facts", [])
            if isinstance(item, dict) and item.get("ref") == reference
        ),
        None,
    )
    if not isinstance(fact, dict) or "value" not in fact:
        return {}
    value = fact["value"]
    if isinstance(value, list) and not value:
        shape = "list"
    elif isinstance(value, dict) and not value:
        shape = "object"
    else:
        return {}
    return {
        "supplied_value_empty": True,
        "supplied_value_empty_note": (
            f"The supplied value of source facts:{reference} is an empty {shape}; "
            "it contains no element or field to bind."
        ),
    }


def _named_record_source_fields(
    source_kind: Any,
    source_ref: Any,
    expected_type: Any,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """List only the named record's selectors when source_ref names one keyed record.

    The whole-source enumeration is sorted and capped, so a named record late
    in a large keyed fact can fall outside it; the key itself lives only in
    the records companion fact.
    """

    if not isinstance(source_kind, str) or not isinstance(source_ref, str):
        return {}
    named = named_record_facts(source_kind, source_ref, inventory)
    if named is None:
        return {}
    record_key, facts = named
    sources: list[dict[str, Any]] = []
    for reference, record_schema in facts:
        fact_schema = next(
            item["schema"]
            for item in inventory.get("facts", [])
            if isinstance(item, dict) and item.get("ref") == reference
        )
        selectors, matching, truncated = _binding_selector_details(
            record_schema,
            root=f"value.{record_key}",
            expected_type=expected_type,
        )
        entry: dict[str, Any] = {
            "source_kind": "supplied_input",
            "source_ref": f"facts:{reference}",
            "source_schema_type": fact_schema.get("type"),
            "documented_selectors": selectors,
            "matching_expected_type": matching,
            "truncated": truncated,
        }
        if truncated:
            entry["truncation_note"] = (
                "Documented selector enumeration truncated after "
                f"{_BINDING_REPAIR_SELECTOR_LIMIT} selectors; only the first "
                f"{_BINDING_REPAIR_SELECTOR_LIMIT} sorted paths are shown."
            )
        sources.append(entry)
    return {"named_record_key": record_key, "named_record_sources": sources}


def _selected_record_source_fields(
    source_kind: Any,
    source_ref: Any,
    selector: Any,
    expected_type: Any,
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """List the named record's sources when the selector selects inside one keyed record.

    A binding such as facts:<ref> with value.<record key>.<field> names the
    record in its selector; the record key string itself is documented only
    by the <ref>:records companion.
    """

    if (
        source_kind != "supplied_input"
        or not isinstance(source_ref, str)
        or not isinstance(selector, str)
    ):
        return {}
    canonical_ref, canonical_selector = canonical_binding_paths(
        source_kind, source_ref, selector, inventory
    )
    parts = canonical_selector.split(".")
    if len(parts) < 2 or parts[0] != "value" or not parts[1]:
        return {}
    return _named_record_source_fields(
        source_kind, f"{canonical_ref}:{parts[1]}", expected_type, inventory
    )


def _review_selector_checks(
    required_change: str,
    binding: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    """Check the selector paths a review's required change names against documented sources.

    This is a structural lookup of exact dot paths; it makes no judgment about
    which selector the scenario needs.
    """

    selectors = list(dict.fromkeys(_REVIEW_SELECTOR_TOKEN.findall(required_change)))
    if not selectors:
        return []
    source_kind = binding.get("source_kind")
    source_ref = binding.get("source_ref")
    binding_schema = None
    if isinstance(source_kind, str) and isinstance(source_ref, str):
        binding_schema, _ = _binding_source_schema(
            source_kind, source_ref, inventory, runtime_contract, binding.get("name")
        )
    documented_sources: list[tuple[str, dict[str, Any]]] = [
        (f"facts:{fact['ref']}", fact["schema"])
        for fact in inventory.get("facts", [])
        if isinstance(fact, dict)
        and isinstance(fact.get("ref"), str)
        and fact["ref"]
        and isinstance(fact.get("schema"), dict)
    ]
    permitted = runtime_contract.get("setup_permissions", [])
    permitted_names = set(permitted) if isinstance(permitted, list) else set()
    documented_sources.extend(
        (f"setup:{operation['name']}", operation["result_schema"])
        for operation in inventory.get("operations", [])
        if isinstance(operation, dict)
        and isinstance(operation.get("name"), str)
        and operation["name"] in permitted_names
        and isinstance(operation.get("result_schema"), dict)
    )
    checks: list[dict[str, Any]] = []
    for selector in selectors[:_BINDING_REPAIR_SELECTOR_LIMIT]:
        root = "value" if selector.startswith("value") else "result"
        refs = sorted(
            {
                reference
                for reference, schema in documented_sources
                if reference.startswith("facts:" if root == "value" else "setup:")
                and _binding_selector_type(schema, selector) is not None
            }
        )
        checks.append(
            {
                "selector": selector,
                "documented_on_binding_source": (
                    binding_schema is not None
                    and _binding_selector_type(binding_schema, selector) is not None
                ),
                "documented_source_refs": refs[:_BINDING_REPAIR_SELECTOR_LIMIT],
            }
        )
    return checks


def _review_binding_indices(
    finding: Finding | dict[str, Any],
    bindings: list[Any],
) -> list[int]:
    """Return the runtime bindings a semantic review finding points to.

    The finding's location pointer names one binding index, or its location
    or required change names declared binding identifiers exactly.
    """

    details = finding.details if isinstance(finding, Finding) else finding.get("details")
    if not isinstance(details, dict):
        return []
    location = details.get("review_location")
    required_change = details.get("review_required_change")
    location = location if isinstance(location, str) else ""
    required_change = required_change if isinstance(required_change, str) else ""
    match = _REVIEW_BINDING_LOCATION.match(location.strip())
    if match:
        index = int(match.group(1))
        return [index] if index < len(bindings) else []
    indices: list[int] = []
    for index, binding in enumerate(bindings):
        name = binding.get("name") if isinstance(binding, dict) else None
        if not isinstance(name, str) or not name:
            continue
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
        if pattern.search(location) or pattern.search(required_change):
            indices.append(index)
    return indices


def _repair_review_binding_option(
    finding: Finding | dict[str, Any],
    index: int,
    binding: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the documented choices for a binding that a semantic review finding concerns."""

    option = _repair_selector_option(
        {"code": "semantic_review", "path": f"runtime_bindings[{index}]"},
        binding,
        inventory,
        runtime_contract,
        selected_record=True,
    )
    if option.get("resolved_source") is not True:
        return None
    option["kind"] = "review_binding"
    option["selector"] = binding.get("selector")
    if "named_record_sources" in option:
        # The capped whole-source list mostly repeats other records' fields.
        for key in (
            "documented_selectors",
            "matching_expected_type",
            "truncated",
            "truncation_note",
            "no_matching_selector_note",
        ):
            option.pop(key, None)
    details = finding.details if isinstance(finding, Finding) else finding.get("details")
    required_change = details.get("review_required_change") if isinstance(details, dict) else None
    if isinstance(required_change, str):
        checks = _review_selector_checks(required_change, binding, inventory, runtime_contract)
        if checks:
            option["review_selector_checks"] = checks
    return option


def _binding_selector_details(
    schema: dict[str, Any],
    *,
    root: str,
    expected_type: Any,
) -> tuple[dict[str, str], list[str], bool]:
    """Return documented selectors and those compatible with one expected type."""

    selectors, truncated = _documented_binding_selectors(schema, root=root)
    matching = [
        selector
        for selector, actual_type in selectors.items()
        if isinstance(expected_type, str)
        and expected_type in CLOSED_TYPES
        and _binding_types_compatible(actual_type, expected_type)
    ]
    return selectors, matching, truncated


def _supplied_fact_selector_source(
    reference: str,
    inventory: dict[str, Any],
) -> dict[str, Any] | None:
    """Return one evidence fact's selector choices, if it is bindable."""

    fact = next(
        (
            item
            for item in inventory.get("facts", [])
            if isinstance(item, dict) and item.get("ref") == reference
        ),
        None,
    )
    if not isinstance(fact, dict) or not isinstance(fact.get("schema"), dict):
        return None
    selectors, _, truncated = _binding_selector_details(
        fact["schema"],
        root="value",
        expected_type=None,
    )
    result: dict[str, Any] = {
        "evidence_ref": reference,
        "source_kind": "supplied_input",
        "source_ref": f"facts:{reference}",
        "source_schema_type": fact["schema"].get("type"),
        "documented_selectors": selectors,
        "truncated": truncated,
    }
    if truncated:
        result["truncation_note"] = (
            "Documented selector enumeration truncated after "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} selectors; only the first "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} sorted paths are shown."
        )
    return result


def _repair_unknown_binding_option(
    finding: Finding | dict[str, Any],
    prerequisite: dict[str, Any],
    candidate: dict[str, Any],
    inventory: dict[str, Any],
) -> dict[str, Any]:
    """Build declaration and evidence-source choices for an unknown binding."""

    code = finding.code if isinstance(finding, Finding) else finding.get("code")
    path = finding.path if isinstance(finding, Finding) else finding.get("path", "")
    name = prerequisite.get("binding")
    declared_bindings = candidate.get("runtime_bindings", [])
    all_declared_names = sorted(
        {
            item.get("name")
            for item in declared_bindings
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
    )
    declared_names = all_declared_names[:_BINDING_REPAIR_SELECTOR_LIMIT]
    declared_names_truncated = len(all_declared_names) > _BINDING_REPAIR_SELECTOR_LIMIT
    evidence_sources: list[dict[str, Any]] = []
    evidence_refs = prerequisite.get("evidence_refs", [])
    if isinstance(evidence_refs, list):
        for reference in sorted({ref for ref in evidence_refs if isinstance(ref, str)}):
            source = _supplied_fact_selector_source(reference, inventory)
            if source is not None:
                evidence_sources.append(source)
    evidence_sources_truncated = len(evidence_sources) > _BINDING_REPAIR_SELECTOR_LIMIT
    option = {
        "code": code,
        "path": path,
        "kind": "unknown_binding",
        "binding_name": name,
        "declared_binding_names": declared_names,
        "declared_binding_names_truncated": declared_names_truncated,
        "declaration_requirement": (
            f'Add a runtime_bindings entry with name "{name}" whose consumers '
            f'include "prerequisites.{name}".'
        ),
        "evidence_ref_sources": evidence_sources[:_BINDING_REPAIR_SELECTOR_LIMIT],
        "evidence_ref_sources_truncated": evidence_sources_truncated,
    }
    if declared_names_truncated or evidence_sources_truncated:
        labels = []
        if declared_names_truncated:
            labels.append("binding name")
        if evidence_sources_truncated:
            labels.append("supplied fact source")
        option["truncation_note"] = _repair_truncation_note(" and ".join(labels))
    return option


def _repair_consumer_mismatch_option(
    finding: Finding | dict[str, Any],
    prerequisite: dict[str, Any],
) -> dict[str, Any]:
    """Build the exact consumer repair for one prerequisite binding."""

    code = finding.code if isinstance(finding, Finding) else finding.get("code")
    path = finding.path if isinstance(finding, Finding) else finding.get("path", "")
    name = prerequisite.get("binding")
    consumer = f"prerequisites.{name}"
    return {
        "code": code,
        "path": path,
        "kind": "consumer_mismatch",
        "binding_name": name,
        "required_consumer": consumer,
        "consumer_requirement": (
            f'Add "{consumer}" to the consumers list of the "{name}" runtime binding.'
        ),
    }


def _repair_source_entry(
    *,
    source_kind: str,
    source_ref: str,
    schema: dict[str, Any],
    expected_type: Any,
) -> dict[str, Any]:
    """Build one source choice with selectors rooted at its source result."""

    root = "value" if source_kind == "supplied_input" else "result"
    selectors, matching, truncated = _binding_selector_details(
        schema,
        root=root,
        expected_type=expected_type,
    )
    entry: dict[str, Any] = {
        "source_kind": source_kind,
        "source_ref": source_ref,
        "source_schema_type": schema.get("type"),
        "documented_selectors": selectors,
        "matching_expected_type": matching,
        "truncated": truncated,
    }
    if truncated:
        entry["truncation_note"] = (
            "Documented selector enumeration truncated after "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} selectors; only the first "
            f"{_BINDING_REPAIR_SELECTOR_LIMIT} sorted paths are shown."
        )
    return entry


def _candidate_string_values(candidate: dict[str, Any]) -> set[str]:
    """Return every string value in a candidate plan for exact citation checks."""

    values: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            values.add(value)

    visit(candidate)
    return values


def _repair_source_option(
    findings: list[Finding | dict[str, Any]],
    binding: dict[str, Any],
    candidate: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> dict[str, Any]:
    """Build merged source repair choices for one runtime binding."""

    source_kind = binding.get("source_kind")
    source_ref = binding.get("source_ref")
    binding_name = binding.get("name")
    expected_type = binding.get("expected_type")
    resolved_schema: dict[str, Any] | None = None
    if isinstance(source_kind, str) and isinstance(source_ref, str):
        resolved_schema, _ = _binding_source_schema(
            source_kind,
            source_ref,
            inventory,
            runtime_contract,
            binding_name,
        )

    cited_values = _candidate_string_values(candidate)
    referenced_fact_sources: list[dict[str, Any]] = []
    other_fact_source_refs: list[str] = []
    for fact in sorted(
        (
            item
            for item in inventory.get("facts", [])
            if (
                isinstance(item, dict)
                and isinstance(item.get("ref"), str)
                and item["ref"]
                and isinstance(item.get("schema"), dict)
            )
        ),
        key=lambda item: item["ref"],
    ):
        reference = fact["ref"]
        if reference in cited_values or f"facts:{reference}" in cited_values:
            referenced_fact_sources.append(
                {
                    **_repair_source_entry(
                        source_kind="supplied_input",
                        source_ref=f"facts:{reference}",
                        schema=fact["schema"],
                        expected_type=expected_type,
                    ),
                    **_supplied_value_empty_fields(
                        "supplied_input", f"facts:{reference}", inventory
                    ),
                }
            )
        else:
            other_fact_source_refs.append(f"facts:{reference}")

    permitted_names = {
        operation
        for operation in runtime_contract.get("setup_permissions", [])
        if isinstance(operation, str)
    }
    permitted_setup_sources: list[dict[str, Any]] = []
    seen_operations: set[str] = set()
    for operation in sorted(
        (
            item
            for item in inventory.get("operations", [])
            if (
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"] in permitted_names
                and isinstance(item.get("result_schema"), dict)
            )
        ),
        key=lambda item: item["name"],
    ):
        operation_name = operation["name"]
        if operation_name in seen_operations:
            continue
        seen_operations.add(operation_name)
        permitted_setup_sources.append(
            _repair_source_entry(
                source_kind="setup_output",
                source_ref=f"setup:{operation_name}",
                schema=operation["result_schema"],
                expected_type=expected_type,
            )
        )

    referenced_fact_sources_truncated = (
        len(referenced_fact_sources) > _BINDING_REPAIR_SELECTOR_LIMIT
    )
    permitted_setup_sources_truncated = (
        len(permitted_setup_sources) > _BINDING_REPAIR_SELECTOR_LIMIT
    )
    other_fact_source_refs_truncated = len(other_fact_source_refs) > _BINDING_REPAIR_SELECTOR_LIMIT
    option: dict[str, Any] = {
        "kind": "source",
        "findings": [
            {
                "code": (finding.code if isinstance(finding, Finding) else finding.get("code")),
                "path": (
                    finding.path if isinstance(finding, Finding) else finding.get("path", "")
                ),
            }
            for finding in findings
        ],
        "binding_name": binding_name,
        "expected_type": expected_type,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "resolved_source": resolved_schema is not None,
        "referenced_fact_sources": referenced_fact_sources[:_BINDING_REPAIR_SELECTOR_LIMIT],
        "referenced_fact_sources_truncated": referenced_fact_sources_truncated,
        "permitted_setup_sources": permitted_setup_sources[:_BINDING_REPAIR_SELECTOR_LIMIT],
        "permitted_setup_sources_truncated": permitted_setup_sources_truncated,
        "other_fact_source_refs": other_fact_source_refs[:_BINDING_REPAIR_SELECTOR_LIMIT],
        "other_fact_source_refs_truncated": other_fact_source_refs_truncated,
        **_named_record_source_fields(source_kind, source_ref, expected_type, inventory),
    }
    truncated_labels = []
    if referenced_fact_sources_truncated:
        truncated_labels.append("referenced fact source")
    if permitted_setup_sources_truncated:
        truncated_labels.append("permitted setup source")
    if other_fact_source_refs_truncated:
        truncated_labels.append("other fact source")
    if truncated_labels:
        option["truncation_note"] = _repair_truncation_note(" and ".join(truncated_labels))
    return option


_REFERENCE_FIELD_PATTERNS = (
    ("interpretation.source_refs", re.compile(r"interpretation\.source_refs\[\d+\]")),
    ("selected_evidence[].ref", re.compile(r"selected_evidence\[\d+\](?:\.ref)?")),
    ("assumptions[].ref", re.compile(r"assumptions\[\d+\]\.ref")),
    ("prerequisites[].evidence_refs", re.compile(r"prerequisites\[\d+\]\.evidence_refs\[\d+\]")),
)
_REFERENCE_REPAIR_DESCRIPTION = (
    "Each option explains one unknown_reference finding: the rejected value, what "
    "kind of value it is, and the reference rule for its field from EVIDENCE "
    "REFERENCES in the original stage context. Replace the value with a listed "
    "reference that supports the same claim, keep a valid scenario lineage or "
    "attack-tree node ID in interpretation.source_refs or assumptions[].ref as "
    "permitted by that field, move an observation scope to required_observations, "
    "or remove the entry when no supplied reference supports it."
)
_REFERENCE_VALUE_KIND_REPAIRS = {
    "provenance_id": (
        "This is a scenario lineage or attack-tree node ID. It is valid only in "
        "interpretation.source_refs or assumptions[].ref; keep it at this field "
        "only when that field's rule permits provenance_ids."
    ),
    "observation_scope": (
        "This is an observation scope, not a reference. Declare the capture in "
        "required_observations and cite a supplied fact, source handle, or "
        "operation:<name> at this field."
    ),
    "unlisted": (
        "This value is not listed in EVIDENCE REFERENCES. Use an exact listed "
        "reference, or remove the entry when no supplied reference supports it."
    ),
}


def _reference_repair_options_for_correction(
    correction_context: dict[str, Any],
) -> dict[str, Any] | None:
    """Explain each rejected plan reference against the rendered reference rules."""

    original = correction_context.get("original_context")
    references = original.get("evidence_references") if isinstance(original, dict) else None
    if not isinstance(references, dict):
        return None
    provenance = references.get("provenance_ids", {})
    listed = provenance.get("ids", {}) if isinstance(provenance, dict) else {}
    provenance_ids = set(listed) if isinstance(listed, dict) else set()
    capabilities = original.get("execution_capabilities")
    observation = capabilities.get("observation") if isinstance(capabilities, dict) else None
    scopes = set(observation) if isinstance(observation, dict) else set()
    scopes.update({"assistant_messages", "messages", "tool_calls"})
    field_rules = references.get("field_rules", {})
    options: list[dict[str, Any]] = []
    for finding in correction_context.get("findings", []):
        if not isinstance(finding, dict) or finding.get("code") != "unknown_reference":
            continue
        path = finding.get("path")
        field = next(
            (
                name
                for name, pattern in _REFERENCE_FIELD_PATTERNS
                if isinstance(path, str) and pattern.fullmatch(path)
            ),
            None,
        )
        if field is None:
            continue
        detail = str(finding.get("detail", ""))
        value = detail.split(": ", 1)[1] if detail.startswith("unknown_reference: ") else detail
        if value in provenance_ids:
            kind = "provenance_id"
        elif value in scopes or value.partition(":")[2] in scopes:
            kind = "observation_scope"
        else:
            kind = "unlisted"
        options.append(
            {
                "path": path,
                "rejected_value": value,
                "rejected_value_kind": kind,
                "field_rule": field_rules.get(field),
                "repair": _REFERENCE_VALUE_KIND_REPAIRS[kind],
            }
        )
    if not options:
        return None
    return {
        "description": _REFERENCE_REPAIR_DESCRIPTION,
        "valid_provenance_ids": sorted(provenance_ids),
        "options": options,
    }


def _binding_repair_options_for_correction(
    correction_context: dict[str, Any],
) -> dict[str, Any] | None:
    """Compute plan correction repair choices.

    The choices also cover bindings that semantic review findings concern and
    list the record sources of a selector that names one keyed record.
    """

    if correction_context.get("stage") != "plan":
        return None
    candidate = _correction_plan_candidate(correction_context)
    inventory, runtime_contract = _correction_binding_inputs(correction_context)
    options: list[dict[str, Any]] = []
    findings = correction_context.get("findings", [])
    if candidate is not None and isinstance(findings, list):
        bindings = candidate.get("runtime_bindings", [])
        prerequisites = candidate.get("prerequisites", [])
        binding_findings: dict[int, dict[str, list[Finding | dict[str, Any]]]] = {}
        binding_finding_order: dict[int, list[Finding | dict[str, Any]]] = {}
        for finding in findings:
            path = finding.path if isinstance(finding, Finding) else finding.get("path", "")
            if not isinstance(path, str):
                continue
            for field_name, pattern in (
                ("source_ref", _BINDING_SOURCE_REF_FINDING_PATH),
                ("source_kind", _BINDING_SOURCE_KIND_FINDING_PATH),
                ("selector", _BINDING_SELECTOR_FINDING_PATH),
            ):
                match = pattern.fullmatch(path)
                if match:
                    index = int(match.group(1))
                    binding_findings.setdefault(index, {}).setdefault(field_name, []).append(
                        finding
                    )
                    binding_finding_order.setdefault(index, []).append(finding)
                    break

        if isinstance(bindings, list):
            for index, grouped in binding_findings.items():
                if index >= len(bindings) or not isinstance(bindings[index], dict):
                    continue
                binding = bindings[index]
                source_findings = grouped.get("source_ref", []) + grouped.get("source_kind", [])
                selector_findings = grouped.get("selector", [])
                source_schema = None
                source_kind = binding.get("source_kind")
                source_ref = binding.get("source_ref")
                name = binding.get("name")
                if isinstance(source_kind, str) and isinstance(source_ref, str):
                    source_schema, _ = _binding_source_schema(
                        source_kind,
                        source_ref,
                        inventory,
                        runtime_contract,
                        name,
                    )
                if source_findings or source_schema is None:
                    options.append(
                        _repair_source_option(
                            binding_finding_order[index],
                            binding,
                            candidate,
                            inventory,
                            runtime_contract,
                        )
                    )
                elif selector_findings:
                    options.append(
                        _repair_selector_option(
                            selector_findings[0],
                            binding,
                            inventory,
                            runtime_contract,
                            selected_record=True,
                        )
                    )
            reviewed: set[int] = set(binding_findings)
            for finding in findings:
                code = finding.code if isinstance(finding, Finding) else finding.get("code")
                if code != "semantic_review":
                    continue
                for index in _review_binding_indices(finding, bindings):
                    if index in reviewed or not isinstance(bindings[index], dict):
                        continue
                    review_option = _repair_review_binding_option(
                        finding,
                        index,
                        bindings[index],
                        inventory,
                        runtime_contract,
                    )
                    if review_option is not None:
                        reviewed.add(index)
                        options.append(review_option)

        if isinstance(prerequisites, list):
            for finding in findings:
                code = finding.code if isinstance(finding, Finding) else finding.get("code")
                path = finding.path if isinstance(finding, Finding) else finding.get("path", "")
                prerequisite_match = (
                    _UNKNOWN_BINDING_FINDING_PATH.fullmatch(path)
                    if isinstance(path, str)
                    else None
                )
                if not prerequisite_match or code not in {"unknown_binding", "consumer_mismatch"}:
                    continue
                index = int(prerequisite_match.group(1))
                if index >= len(prerequisites) or not isinstance(prerequisites[index], dict):
                    continue
                if code == "unknown_binding":
                    options.append(
                        _repair_unknown_binding_option(
                            finding,
                            prerequisites[index],
                            candidate,
                            inventory,
                        )
                    )
                else:
                    options.append(
                        _repair_consumer_mismatch_option(
                            finding,
                            prerequisites[index],
                        )
                    )

    deduplicated: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for option in options:
        if option.get("kind") == "source":
            findings_key = tuple(
                (item.get("code"), item.get("path"))
                for item in option.get("findings", [])
                if isinstance(item, dict)
            )
            key = (option.get("kind"), option.get("binding_name"), findings_key)
        else:
            key = (option.get("kind"), option.get("path"))
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(option)
    if not deduplicated:
        return None
    return {
        "description": _BINDING_REPAIR_OPTIONS_DESCRIPTION,
        "field_descriptions": deepcopy(_BINDING_REPAIR_OPTION_FIELD_DESCRIPTIONS_V26),
        "options": deduplicated,
    }


class PrivateModelAuthoringTransport:
    """Explicit OpenAI-compatible private authoring client with retries off."""

    max_retries = 0

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        profile_name: str | None = None,
        temperature: float = 0.0,
        extra_body: dict[str, Any] | None = None,
        review_extra_body: dict[str, Any] | None = None,
        max_completion_tokens: int | None = None,
        context_window_tokens: int | None = None,
        review_fill_context: bool = False,
        reasoning_effort: str | None = None,
        service_tier: str | None = None,
        service_tier_fallback: str | None = None,
        sampling_controls: bool = True,
        strict_json_schema: bool | None = None,
        timeout: float | int | None = None,
    ) -> None:
        """Create the client.

        ``extra_body`` applies to author and correction requests.  When
        ``review_extra_body`` is supplied it replaces ``extra_body`` for
        semantic-review requests; otherwise reviews use ``extra_body`` too.

        With ``review_fill_context``, a semantic-review request's completion
        limit is the context window minus the conservative prompt estimate and
        the framing reserve.  The context guard still reserves
        ``max_completion_tokens``, so a review that passes the guard never
        receives less than that limit.
        """

        from openai import OpenAI

        if max_completion_tokens is not None and (
            isinstance(max_completion_tokens, bool)
            or not isinstance(max_completion_tokens, int)
            or max_completion_tokens <= 0
        ):
            raise ValueError("max_completion_tokens must be a positive integer when provided")
        if context_window_tokens is not None and (
            isinstance(context_window_tokens, bool)
            or not isinstance(context_window_tokens, int)
            or context_window_tokens <= 0
        ):
            raise ValueError("context_window_tokens must be a positive integer when provided")
        if (
            context_window_tokens is not None
            and max_completion_tokens is not None
            and max_completion_tokens + _CONTEXT_FRAMING_TOKEN_RESERVE >= context_window_tokens
        ):
            raise ValueError(
                "max_completion_tokens leaves no room for the prompt in the context window"
            )
        self.model = model
        self.profile_name = profile_name
        self.temperature = temperature
        if not isinstance(sampling_controls, bool):
            raise ValueError("sampling_controls must be a boolean")
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str) or not reasoning_effort.strip()
        ):
            raise ValueError("reasoning_effort must be a nonblank string when provided")
        if service_tier is not None and (
            not isinstance(service_tier, str) or not service_tier.strip()
        ):
            raise ValueError("service_tier must be a nonblank string when provided")
        if service_tier_fallback is not None and (
            not isinstance(service_tier_fallback, str) or not service_tier_fallback.strip()
        ):
            raise ValueError("service_tier_fallback must be a nonblank string when provided")
        if strict_json_schema is not None and not isinstance(strict_json_schema, bool):
            raise ValueError("strict_json_schema must be a boolean when provided")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
        ):
            raise ValueError("timeout must be a positive number when provided")
        self.reasoning_effort = reasoning_effort
        self.service_tier = service_tier
        self.service_tier_fallback = service_tier_fallback
        self.sampling_controls = sampling_controls
        self.strict_json_schema = strict_json_schema
        self.timeout = timeout
        self.last_controls: dict[str, Any] | None = None
        self.extra_body = deepcopy(extra_body) if extra_body is not None else None
        self.review_extra_body = (
            deepcopy(review_extra_body) if review_extra_body is not None else None
        )
        if review_fill_context and (
            context_window_tokens is None or max_completion_tokens is None
        ):
            raise ValueError(
                "review_fill_context requires context_window_tokens and max_completion_tokens"
            )
        self.max_completion_tokens = max_completion_tokens
        self.context_window_tokens = context_window_tokens
        self.review_fill_context = review_fill_context
        self._endpoint_netloc, self._endpoint_hostname = _endpoint_identity(base_url)
        client_options: dict[str, Any] = {
            "base_url": base_url,
            "api_key": api_key,
            "max_retries": 0,
        }
        if timeout is not None:
            client_options["timeout"] = timeout
        self._client = OpenAI(**client_options)

    def extra_body_for(self, packet: PromptPacket) -> dict[str, Any] | None:
        """Return the extra_body controls for this packet's role."""

        body = (
            self.review_extra_body
            if packet.stage in _REVIEW_STAGES and self.review_extra_body is not None
            else self.extra_body
        )
        if body is None:
            return None
        result = deepcopy(body)
        if not self.sampling_controls:
            for key in ("chat_template_kwargs", "temperature", "top_p", "top_k", "seed"):
                result.pop(key, None)
        return result or None

    def max_completion_tokens_for(self, packet: PromptPacket) -> int | None:
        """Return the completion limit sent with this packet."""

        if (
            self.review_fill_context
            and packet.stage in _REVIEW_STAGES
            and self.context_window_tokens is not None
            and self.max_completion_tokens is not None
        ):
            estimate = _context_budget_estimate(packet)["estimated_prompt_tokens"]
            filled = self.context_window_tokens - int(estimate) - _CONTEXT_FRAMING_TOKEN_RESERVE
            # The default completion limit is a floor that a review fills from
            # the remaining context; treat a larger profile completion limit
            # as the review role's explicit cap. A 1.05M context must not turn
            # a review into a million-token request.
            if self.max_completion_tokens > AUTHORING_MAX_COMPLETION_TOKENS:
                return min(filled, self.max_completion_tokens)
            return max(filled, self.max_completion_tokens)
        return self.max_completion_tokens

    def complete(self, packet: PromptPacket) -> TransportResponse:
        self.preflight_context_budget(packet)
        extra_body = self.extra_body_for(packet)
        max_completion_tokens = self.max_completion_tokens_for(packet)
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": packet.system},
                {"role": "user", "content": packet.user},
            ],
        }
        if self.sampling_controls:
            request["temperature"] = self.temperature
        if extra_body is not None:
            request["extra_body"] = deepcopy(extra_body)
        if max_completion_tokens is not None:
            request["max_completion_tokens"] = max_completion_tokens
        if self.reasoning_effort is not None:
            request["reasoning_effort"] = self.reasoning_effort
        if self.service_tier is not None:
            request["service_tier"] = self.service_tier
        fallback_used = False
        self.last_controls = self._request_controls(
            extra_body=extra_body,
            max_completion_tokens=max_completion_tokens,
            service_tier=request.get("service_tier"),
            fallback_used=fallback_used,
        )
        try:
            response = self._client.chat.completions.create(**request)
        except self._rate_limit_error_type():
            if self.service_tier is None or self.service_tier_fallback is None:
                raise
            fallback_request = deepcopy(request)
            fallback_request["service_tier"] = self.service_tier_fallback
            request = fallback_request
            fallback_used = True
            self.last_controls = self._request_controls(
                extra_body=extra_body,
                max_completion_tokens=max_completion_tokens,
                service_tier=request.get("service_tier"),
                fallback_used=fallback_used,
            )
            response = self._client.chat.completions.create(**fallback_request)
        choice = response.choices[0]
        message = choice.message
        provider_model = getattr(response, "model", None)
        if not isinstance(provider_model, str) or not provider_model.strip():
            provider_model = None
        content = _provider_field(message, "content")
        if content is _MISSING or content is None:
            raw = b""
        elif isinstance(content, str):
            raw = content.encode("utf-8")
        else:
            raw = b""
        usage = _model_dump(getattr(response, "usage", None))
        controls = deepcopy(self.last_controls) if self.last_controls is not None else {}
        return TransportResponse(
            raw=raw,
            usage=usage,
            controls=controls,
            response_capture=_provider_response_capture(choice, message),
            provider_model=provider_model,
        )

    def _request_controls(
        self,
        *,
        extra_body: dict[str, Any] | None,
        max_completion_tokens: int | None,
        service_tier: Any,
        fallback_used: bool,
    ) -> dict[str, Any]:
        """Return the non-secret controls for the request currently in flight."""

        controls: dict[str, Any] = {"max_retries": 0}
        if self.sampling_controls:
            controls.update({"temperature": self.temperature, "extra_body": extra_body})
        elif extra_body is not None:
            controls["extra_body"] = extra_body
        if max_completion_tokens is not None:
            controls["max_completion_tokens"] = max_completion_tokens
        if self.context_window_tokens is not None:
            controls["context_window_tokens"] = self.context_window_tokens
        if self.reasoning_effort is not None:
            controls["reasoning_effort"] = self.reasoning_effort
        if service_tier is not None:
            controls["service_tier"] = service_tier
            controls["service_tier_requested"] = self.service_tier
        if self.service_tier_fallback is not None:
            controls["service_tier_fallback"] = self.service_tier_fallback
            controls["service_tier_fallback_used"] = fallback_used
        if not self.sampling_controls:
            controls["sampling_controls"] = False
        if self.strict_json_schema is not None:
            controls["strict_json_schema"] = self.strict_json_schema
        if self.timeout is not None:
            controls["timeout"] = self.timeout
        return controls

    @staticmethod
    def _rate_limit_error_type() -> type[BaseException]:
        """Resolve the SDK exception lazily so offline fakes remain simple."""

        from openai import RateLimitError

        return RateLimitError

    def preflight_context_budget(
        self, packet: PromptPacket
    ) -> dict[str, int | float | str] | None:
        """Expose the guard so orchestration can reject before reserving budget.

        The guard also rejects a prompt that names this transport's configured
        endpoint, whatever the provenance of the text that carries it.
        """

        paths = _endpoint_prompt_paths(packet, self._endpoint_netloc, self._endpoint_hostname)
        if paths:
            raise PromptPreflightError(f"secret-bearing authoring evidence: {', '.join(paths)}")
        if self.context_window_tokens is None or self.max_completion_tokens is None:
            return None
        return _enforce_context_budget(
            packet,
            context_window_tokens=self.context_window_tokens,
            max_completion_tokens=self.max_completion_tokens,
        )


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


# Caller-supplied extra detector-control cases: either a static sequence of
# ControlCase objects or a provider callable that receives the current
# candidate plan and metadata and returns the extra cases for that candidate.
SuppliedControlCases = (
    Sequence[ControlCase] | Callable[[Mapping[str, Any], Mapping[str, Any]], Sequence[ControlCase]]
)


def _validate_supplied_control_cases(
    supplied_control_cases: SuppliedControlCases,
) -> SuppliedControlCases:
    """Validate the static supplied-control form eagerly; providers self-report."""

    if callable(supplied_control_cases):
        return supplied_control_cases
    if isinstance(supplied_control_cases, (str, bytes)) or not isinstance(
        supplied_control_cases, Sequence
    ):
        raise ValueError(
            "supplied_control_cases must be ControlCase instances or a callable that returns them"
        )
    for case in supplied_control_cases:
        if not isinstance(case, ControlCase):
            raise ValueError("supplied_control_cases must contain only ControlCase instances")
    return supplied_control_cases


def _mark_control_origins(
    results: list[dict[str, Any]],
    normal_count: int,
) -> None:
    """Label each combined control result with its mechanical or supplied origin."""

    for index, record in enumerate(results):
        record["origin"] = "normal" if index < normal_count else "supplied"


def _deduplicate_control_cases(
    normal_cases: Sequence[ControlCase],
    supplied_cases: Sequence[ControlCase],
) -> tuple[tuple[ControlCase, ...], dict[str, Any]]:
    """Drop supplied cases that exactly duplicate generated expectations."""

    generated = tuple(normal_cases)
    supplied = tuple(supplied_cases)
    selected = list(generated)
    duplicates_dropped: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    generated_by_evidence: dict[str, list[ControlCase]] = {}
    for case in generated:
        evidence_key = _canonical_json(case.evidence)
        generated_by_evidence.setdefault(evidence_key, []).append(case)

    for supplied_case in supplied:
        evidence_key = _canonical_json(supplied_case.evidence)
        matching_generated = generated_by_evidence.get(evidence_key, [])
        exact_matches = [
            case
            for case in matching_generated
            if (
                case.expected_outcome == supplied_case.expected_outcome
                and case.expected_claim_level == supplied_case.expected_claim_level
            )
        ]
        if exact_matches:
            duplicates_dropped.append(
                {
                    "supplied_name": supplied_case.name,
                    "generated_name": exact_matches[0].name,
                }
            )
            continue
        for generated_case in matching_generated:
            conflicts.append(
                {
                    "supplied_name": supplied_case.name,
                    "generated_name": generated_case.name,
                    "reason": "same_evidence_different_expectation",
                }
            )
        selected.append(supplied_case)

    return tuple(selected), {
        "generated_count": len(generated),
        "supplied_count": len(supplied),
        "executed_count": len(selected),
        "duplicates_dropped": duplicates_dropped,
        "conflicts": conflicts,
    }


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
        if getattr(transport, "max_retries", None) != 0:
            raise ValueError("authoring transport must set max_retries=0")
        if supplied_control_cases is not None:
            _validate_supplied_control_cases(supplied_control_cases)
        if discovery_provenance is not None and not isinstance(discovery_provenance, dict):
            raise ValueError("discovery_provenance must be a mapping")
        if not isinstance(policy, AuthoringPolicy):
            raise ValueError("policy must be an AuthoringPolicy instance")
        self.transport = transport
        self.package_dir = Path(package_dir)
        self.task_id = task_id
        self.discovery_provenance = deepcopy(discovery_provenance or {})
        self.policy = policy
        self.review_model_profile = policy.review_model_profile
        if budget is None:
            # The default budget covers the policy's own closed worst case; an
            # explicitly supplied budget is honored as an earlier stop and
            # never raised to the policy maximum.
            role_limits = policy_role_limits(policy)
            budget = AuthoringBudget(
                aggregate_limit=MAX_AUTHORING_REQUESTS,
                task_limit=policy_max_dispatches(policy),
                author_limit=max(MAX_AUTHOR_CORRECTION_REQUESTS_PER_TASK, role_limits["author"]),
                review_limit=max(MAX_REVIEW_REQUESTS_PER_TASK, role_limits["reviewer"]),
            )
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
        self._review_reuse: dict[str, str] = {}
        self._review_evidence: dict[str, dict[str, Any]] = {}
        self._saved_plan_review_packet: PromptPacket | None = None
        self._last_controls: list[dict[str, Any]] | None = None
        self._control_condition: dict[str, Any] | None = None
        self._last_detector_feedback: tuple[DetectorControlFeedback, ...] = ()
        self._supplied_control_cases = (
            None if supplied_control_cases is None else supplied_control_cases
        )
        self._raw_responses: dict[str, bytes] = {}
        self._decoded_responses: dict[str, Any] = {}
        self._prompt_packets: dict[str, PromptPacket] = {}
        self._transformations: list[Any] = []
        self._failure_evidence = new_failure_evidence(self.task_id, self.package_dir)
        self._failure_evidence["budget"] = self.budget.snapshot(self.task_id)
        self._failure_evidence_file: Path | None = None
        self._call2_python_bytes: bytes | None = None

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
            self._ledger[-1]["detector_controls"] = controls
            self._ledger[-1]["control_deduplication"] = deepcopy(deduplication)
            if skips:
                self._ledger[-1]["detector_control_skips"] = deepcopy(skips)
        if self._failure_evidence.get("attempts"):
            self._failure_attempt()["detector_controls"] = controls
            self._failure_attempt()["control_deduplication"] = deepcopy(deduplication)
            if skips:
                self._failure_attempt()["detector_control_skips"] = deepcopy(skips)
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
        except PromptOverflowError as exc:
            finding = _prompt_overflow_finding(exc, stage)
            self._findings.append(finding)
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            return None, [finding], b""
        except BudgetExceeded as exc:
            # Budget stops happen before dispatch: no ledger record exists to
            # annotate, and no stage attempt was created for this request.
            finding = Finding("budget_exhausted", _safe_error(exc), stage)
            self._findings.append(finding)
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            return None, [finding], b""
        except Exception as exc:
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
            return None, [finding], b""
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
        try:
            if stage == "call2":
                decoded: Any = parse_call2_response(raw)
                self._call2_python_bytes = decoded.python_bytes
                self._raw_responses["call2-python"] = decoded.python_bytes
                validation_value: dict[str, Any] | ParsedCall2Response = decoded
                record["framing"] = "two-block-v2"
                record["decoded_output"] = decoded.metadata
                self._decoded_responses[stage] = decoded.metadata
                self._failure_attempt()["decoded_output"] = decoded.metadata
                self._record_candidate_digest(decoded)
            else:
                decoded, transformation = _decode_v2_json_response(raw)
                validation_value = decoded
                if transformation:
                    self._transformations.append(transformation)
                    record["transformation"] = transformation
                    self._failure_attempt()["transformation"] = transformation
                    self._failure_evidence["transformations"] = list(self._transformations)
                self._decoded_responses[stage] = decoded
                record["decoded_output"] = decoded
                self._failure_attempt()["decoded_output"] = decoded
                if isinstance(decoded, dict):
                    self._record_candidate_digest(decoded)
        except (Call1FramingError, Call2FramingError) as exc:
            self._findings.extend(exc.findings)
            record["framing_findings"] = [finding.to_dict() for finding in exc.findings]
            self._record_checks_not_run(
                ["plan_validation"]
                if stage == "call1"
                else ["artifact_validation", "detector_controls"]
            )
            self._record_failures(exc.findings)
            return None, exc.findings, raw
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            finding = Finding("response_parse_error", str(exc), stage)
            record["parse_error"] = str(exc)
            self._record_checks_not_run(
                ["plan_validation"]
                if stage == "call1"
                else ["artifact_validation", "detector_controls"]
            )
            self._findings.append(finding)
            self._record_failure(finding)
            return None, [finding], raw
        try:
            assert_no_secrets(
                validation_value.metadata
                if isinstance(validation_value, ParsedCall2Response)
                else validation_value
            )
        except AuthoringError as exc:
            finding = Finding("secret_in_response", str(exc), stage)
            self._findings.append(finding)
            self._record_failure(finding)
            return None, [finding], raw
        if stage == "call1" and not isinstance(validation_value, dict):
            finding = Finding("response_type_error", "plan must decode to an object", stage)
            self._findings.append(finding)
            self._record_failure(finding)
            return None, [finding], raw
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
        stage_attempt_index = (
            sum(1 for prior in self._ledger if prior.get("stage") == packet.stage) + 1
        )
        failed_stage = (
            packet.payload.get("failed_stage")
            if packet.stage == "correction" and isinstance(packet.payload, dict)
            else None
        )
        correction_index = (
            sum(
                1
                for prior in self._ledger
                if prior.get("stage") == "correction" and prior.get("failed_stage") == failed_stage
            )
            + 1
            if packet.stage == "correction"
            else 0
        )
        profile_alias = getattr(self.transport, "profile_name", None)
        if not isinstance(profile_alias, str) or not profile_alias.strip():
            profile_alias = None
        requested_model = getattr(self.transport, "model", None)
        if not isinstance(requested_model, str) or not requested_model.strip():
            requested_model = None
        model_identity = {
            "profile_alias": profile_alias,
            "requested_model": requested_model,
            "returned_model": metadata_record(None, unavailable_reason="not_returned"),
        }
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
            input_digest, candidate_digest = _review_packet_digests(packet)
            effective_controls = self._review_controls(None)
            record.update(
                {
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
            )
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
        self._persist_failure_evidence()
        try:
            response = self.transport.complete(packet)
        except Exception:
            last_controls = getattr(self.transport, "last_controls", None)
            if isinstance(last_controls, dict):
                record["controls"] = _safe_metadata(last_controls)
                self._failure_attempt()["controls"] = metadata_record(
                    last_controls,
                    unavailable_reason="provider_did_not_return_response",
                )
            raise
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
        self._persist_failure_evidence()
        return response

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
        self._prompt_packets["correction"] = packet
        started = time.monotonic()
        try:
            response = self._dispatch(packet)
        except PromptOverflowError as exc:
            finding = _prompt_overflow_finding(exc, packet.stage)
            self._findings.append(finding)
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            return None
        except BudgetExceeded as exc:
            # Budget stops happen before dispatch: no ledger record or stage
            # attempt exists for the refused correction request.
            finding = Finding("budget_exhausted", _safe_error(exc), failed_stage)
            self._findings.append(finding)
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            return None
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
            return None
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
        try:
            if failed_stage == "call2":
                parsed = parse_call2_response(raw)
                self._call2_python_bytes = parsed.python_bytes
                self._raw_responses["call2-python"] = parsed.python_bytes
                validation_value: dict[str, Any] | ParsedCall2Response = parsed
                decoded: Any = parsed.metadata
            else:
                decoded, transformation = _decode_v2_json_response(raw)
                validation_value = decoded
                if transformation:
                    self._transformations.append(transformation)
                    self._failure_evidence["transformations"] = list(self._transformations)
                    self._ledger[-1]["transformation"] = transformation
                    self._failure_attempt()["transformation"] = transformation
            assert_no_secrets(
                validation_value.metadata
                if isinstance(validation_value, ParsedCall2Response)
                else validation_value
            )
            self._decoded_responses[f"correction-{failed_stage}"] = decoded
            self._failure_attempt()["decoded_output"] = decoded
            self._record_candidate_digest(validation_value)
            self._persist_failure_evidence()
            if not isinstance(decoded, dict):
                raise ValueError("correction response must decode to an object")
            transformation_count = len(self._transformations)
            if failed_stage == "call1":
                replacement_findings = collect_plan_findings_v2(
                    decoded,
                    inventory,
                    runtime_contract,
                    provenance_ids=scenario_provenance_ids(view),
                    condition=view.payload.get("discriminating_condition"),
                    transformations=self._transformations,
                )
            else:
                replacement_findings = collect_artifact_findings_v2(
                    validation_value,
                    self._decoded_responses["call1"],
                    inventory,
                    runtime_contract,
                    transformations=self._transformations,
                )
            self._record_validation_transformations(transformation_count)
            if replacement_findings:
                self._ledger[-1]["findings"] = [
                    finding.to_dict() for finding in replacement_findings
                ]
                self._findings.extend(replacement_findings)
                self._record_failures(replacement_findings)
                return validation_value, replacement_findings, raw
        except (Call1FramingError, Call2FramingError) as exc:
            self._ledger[-1]["framing_findings"] = [finding.to_dict() for finding in exc.findings]
            self._record_checks_not_run(
                ["plan_validation"]
                if failed_stage == "call1"
                else ["artifact_validation", "detector_controls"]
            )
            self._findings.extend(exc.findings)
            self._record_failures(exc.findings)
            return None
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, AuthoringError) as exc:
            finding = Finding("correction_failed", str(exc), failed_stage)
            self._ledger[-1]["findings"] = [finding.to_dict()]
            if isinstance(exc, (UnicodeDecodeError, json.JSONDecodeError)):
                self._ledger[-1]["parse_error"] = str(exc)
                self._record_checks_not_run(
                    ["plan_validation"]
                    if failed_stage == "call1"
                    else ["artifact_validation", "detector_controls"]
                )
            self._findings.append(finding)
            self._record_failure(finding)
            return None
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
            review_reuse=dict(self._review_reuse),
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

        policy = self.policy
        try:
            packet = build_call1_packet_v2(view, inventory, runtime_contract)
        except PromptPreflightError as exc:
            finding = _prompt_preflight_finding(exc, "call1")
            status = "prompt_overflow" if finding.code == "prompt_overflow" else "failed"
            return _StageStop(status, (finding,))

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

        candidate: dict[str, Any] | None = None
        pending: list[Finding] | None = None
        review_driven = False
        raw = b""
        while True:
            if candidate is None:
                if pending is None:
                    decoded, pending, raw = self._request_and_validate_v2(packet, collector)
                    candidate = decoded if isinstance(decoded, dict) else None
                if candidate is None:
                    pending = pending or [
                        Finding("call1_failed", "Call 1 did not return a plan", "call1")
                    ]
                    blocked = candidate or self._decoded_responses.get("call1")
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
                                self._allowance_exhausted_finding(
                                    "plan", review_revision=review_driven
                                ),
                            ),
                        )
                    self._record_allowances()
                    allowance_kind = "review_revision" if review_driven else "correction"
                    review_driven = False
                    corrected = self._correction_v2(
                        failed_stage="call1",
                        failed_packet=packet,
                        failed_response=raw,
                        findings=list(pending),
                        view=view,
                        inventory=inventory,
                        runtime_contract=runtime_contract,
                        allowance_kind=allowance_kind,
                    )
                    if corrected is None:
                        stop = self._stop_for_author_findings(list(self._findings))
                        if stop is not None:
                            return stop
                        return _StageStop("unresolved", tuple(self._findings or pending))
                    candidate, correction_findings, raw = corrected
                    if correction_findings:
                        pending = list(correction_findings)
                        candidate = None
                        continue
                    else:
                        pending = None
            assert candidate is not None
            if _is_blocked_plan(candidate):
                _persist_blocked_plan(self.package_dir, candidate)
                return _StageStop("blocked")
            if not policy.review_plan:
                self._review_status["plan"] = "not_requested"
                self._review_reuse["plan"] = "not_requested"
                return candidate
            self._review_reuse["plan"] = "fresh_dispatch"
            try:
                review_packet = build_plan_review_packet(
                    view, candidate, inventory, runtime_contract
                )
            except PromptPreflightError as exc:
                finding = _prompt_preflight_finding(exc, "plan_review")
                status = "prompt_overflow" if finding.code == "prompt_overflow" else "failed"
                return _StageStop(status, (finding,))
            outcome = self._semantic_review("plan", review_packet)
            if outcome.stop is not None:
                return outcome.stop
            if outcome.decision == "accept":
                self._review_status["plan"] = "accepted"
                return candidate
            if outcome.decision == "blocked":
                self._review_status["plan"] = "blocked"
                return _StageStop("blocked", self._semantic_finding_objects(outcome, "plan"))
            # Semantic revise findings join the stage correction path but spend
            # the stage's separate review-revision allowance.
            self._review_status["plan"] = "revise"
            pending = self._semantic_finding_objects(outcome, "plan")
            review_driven = True
            candidate = None

    def _artifact_stage_policy(
        self,
        view: InputView,
        plan: dict[str, Any],
        inventory: dict[str, Any],
        runtime_contract: dict[str, Any],
    ) -> tuple[ParsedCall2Response, dict[str, Any], dict[str, Any]] | _StageStop:
        """Author, check, control, correct, and review the artifact stage."""

        policy = self.policy
        try:
            packet = build_call2_packet_v2(view, plan, inventory, runtime_contract)
        except PromptPreflightError as exc:
            finding = _prompt_preflight_finding(exc, "call2")
            status = "prompt_overflow" if finding.code == "prompt_overflow" else "failed"
            return _StageStop(status, (finding,))

        def collector(decoded: Any) -> list[Finding]:
            return collect_artifact_findings_v2(
                decoded,
                plan,
                inventory,
                runtime_contract,
                transformations=self._transformations,
            )

        parsed: ParsedCall2Response | None = None
        pending: list[Finding] | None = None
        review_driven = False
        raw = b""
        while True:
            if parsed is None:
                if pending is None:
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
                    if candidate is not None and not pending:
                        parsed = candidate
                if parsed is None:
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
                                self._allowance_exhausted_finding(
                                    "artifact", review_revision=review_driven
                                ),
                            ),
                        )
                    self._record_allowances()
                    allowance_kind = "review_revision" if review_driven else "correction"
                    review_driven = False
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
                        stop = self._stop_for_author_findings(list(self._findings))
                        if stop is not None:
                            return stop
                        return _StageStop("unresolved", tuple(self._findings or pending))
                    corrected_candidate, correction_findings, raw = correction
                    if not isinstance(corrected_candidate, ParsedCall2Response):
                        stop = self._stop_for_author_findings(list(self._findings))
                        if stop is not None:
                            return stop
                        return _StageStop(
                            "unresolved",
                            tuple(self._findings or correction_findings),
                        )
                    pending = self._run_detector_controls(
                        corrected_candidate,
                        plan,
                        inventory,
                        runtime_contract,
                        list(correction_findings),
                    )
                    if pending:
                        # A corrected candidate can fail deterministic checks,
                        # controls, or both. Keep every finding and use the
                        # latest candidate/control feedback for the next
                        # correction while allowance remains.
                        parsed = None
                        continue
                    parsed = corrected_candidate
            assert parsed is not None
            if not policy.review_artifact:
                self._review_status["artifact"] = "not_requested"
                self._review_reuse["artifact"] = "not_requested"
                return self._artifact_parts(parsed, plan)
            self._review_reuse["artifact"] = "fresh_dispatch"
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
            pending = self._semantic_finding_objects(outcome, "artifact")
            review_driven = True
            parsed = None

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
        except PromptOverflowError as exc:
            finding = _prompt_overflow_finding(exc, packet.stage)
            self._findings.append(finding)
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            self._review_status[review_key] = "prompt_overflow"
            return _ReviewOutcome(
                decision="",
                stop=_StageStop("prompt_overflow", (finding,)),
            )
        except BudgetExceeded as exc:
            # Budget stops happen before dispatch: no ledger record or stage
            # attempt exists for the refused review request.
            finding = Finding("budget_exhausted", _safe_error(exc), packet.stage)
            self._findings.append(finding)
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            self._review_status[review_key] = "unavailable"
            return _ReviewOutcome(
                decision="",
                stop=_StageStop("budget_exhausted", (finding,)),
            )
        except Exception as exc:
            finding = Finding("transport_failure", _safe_error(exc), packet.stage)
            self._findings.append(finding)
            if self._dispatch_recorded:
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
            self._record_unavailable_response(
                reason="provider_failure",
                detail=_safe_error(exc),
                finding=finding,
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
            self._review_status[review_key] = "unavailable"
            return _ReviewOutcome(
                decision="",
                stop=_StageStop("review_unavailable", (finding,)),
            )
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
        try:
            review = parse_review_response(raw)
        except ReviewResponseError as exc:
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
            if self._dispatch_recorded:
                self._ledger[-1]["failure"] = {
                    "phase": "post_response",
                    "code": finding.code,
                    "detail": finding.detail,
                }
            self._failure_attempt()["failure"] = {
                "phase": "post_response",
                "code": finding.code,
                "detail": finding.detail,
            }
            self._failure_evidence["findings"].append(finding.to_dict())
            self._persist_failure_evidence()
            self._review_status[review_key] = "unavailable"
            return _ReviewOutcome(
                decision="",
                stop=_StageStop("review_unavailable", (finding,)),
            )
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
        if any(finding.code == "prompt_overflow" for finding in stop_findings):
            return _StageStop(
                "prompt_overflow",
                tuple(finding for finding in stop_findings if finding.code == "prompt_overflow"),
            )
        if any(finding.code == "budget_exhausted" for finding in stop_findings):
            return _StageStop(
                "budget_exhausted",
                tuple(finding for finding in stop_findings if finding.code == "budget_exhausted"),
            )
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
        evidence.update(
            {
                "status": status,
                "prompt_version": packet.version,
                "prompt_sha256": packet.sha256,
                "reviewed_input_sha256": record.get(
                    "reviewed_input_sha256", _review_packet_digests(packet)[0]
                ),
                "reviewed_candidate_sha256": record.get(
                    "reviewed_candidate_sha256", _review_packet_digests(packet)[1]
                ),
                "candidate_bytes_sha256": record.get(
                    "candidate_bytes_sha256", _review_packet_digests(packet)[1]
                ),
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
            for key in (
                "decision",
                "original_decision",
                "decision_after_scope_filter",
                "summary",
                "findings",
                "out_of_scope_findings",
                "question_ids",
            ):
                if key in review:
                    evidence[key] = deepcopy(review[key])
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
        result.review_reuse = dict(self._review_reuse)
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
        result: list[Finding] = []
        for item in latest:
            if not isinstance(item, dict):
                continue
            code = item.get("code")
            detail = item.get("detail")
            path = item.get("path", "")
            if isinstance(code, str) and isinstance(detail, str):
                result.append(
                    Finding(
                        code,
                        detail,
                        path if isinstance(path, str) else "",
                        item.get("details", {}) if isinstance(item.get("details"), dict) else {},
                    )
                )
        return result or list(fallback)

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
        self._failure_evidence["terminal"] = {
            "stage": self._terminal_stage(terminal_findings),
            "attempt_index": (
                len(self._failure_evidence["attempts"]) - 1
                if self._failure_evidence["attempts"]
                else None
            ),
            "reason": status if not terminal_findings else terminal_findings[-1].code,
        }
        for attempt in self._failure_evidence["attempts"]:
            attempt["terminal_status"] = status
            attempt["stage_status"] = status
        for record in self._ledger:
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
            if finding.path in {"call1", "plan"}:
                return "plan"
            if finding.path in {"plan_review"}:
                return "plan"
            if finding.path in {"call2", "artifact"}:
                return "artifact"
            if finding.path in {"artifact_review"}:
                return "artifact"
        attempts = self._failure_evidence.get("attempts")
        if isinstance(attempts, list) and attempts:
            attempt = attempts[-1]
            stage = attempt.get("stage")
            if stage in {"call1", "plan_review"}:
                return "plan"
            if stage in {"call2", "artifact_review"}:
                return "artifact"
            if stage == "correction":
                return "plan" if attempt.get("failed_stage") == "call1" else "artifact"
        return None


_PLAN_CORRECTION_GUIDANCE = (
    "Evaluate every finding against the source context and PLAN FIELD MEANINGS. "
    "Preserve a correct distinction between evidence requirements and missing-"
    "evidence fallback. Do not make violation, absence, and inconclusive describe "
    "the same situation merely to satisfy a criticism that confuses alternative "
    "branches. Correct real schema, reference, or semantic errors and retain "
    "supported meaning elsewhere. If criticism is not substantiated, preserve the "
    "supported content; do not invent a defect or new field. Return only the complete "
    "replacement plan in the existing response format. Your response will still "
    "undergo the normal checks and review."
)
_ARTIFACT_CORRECTION_GUIDANCE = (
    "Keep the accepted plan fixed. Correct the implementation using the exact "
    "failed-control evidence and substantiated review findings. Do not solve a "
    "missing-evidence failure by assuming the missing value "
    "exists, removing the fallback, or changing the observation level. Use the "
    "plan-derived observation guide to distinguish requested capture inventory from "
    "the evidence needed for each outcome. Correct the supplied candidate against "
    "the fixed accepted "
    "plan and actual evidence interface. Address the listed plan conflicts and "
    "control failures together. Each control includes its exact input, expected "
    "outcome, actual return or exception, and explanation. Preserve working behavior "
    "beyond these examples. Return a complete replacement artifact, including "
    "metadata and Python, in the unchanged response format. Keep an accepted "
    "no-judge decision as null judge metadata; repair the candidate rather than "
    "changing the plan to fit it. Plan source handles and prerequisite citations are "
    "provenance, not runtime evidence paths. Setup binding source_ref and selector "
    "define downstream resolution; read the resolved value at "
    "evidence.bindings.<declared name>. Return evidence_refs that resolve only "
    "against the actual evidence object passed to evaluate. Use the accepted plan's "
    "runtime binding for execution identities; neutral examples and controls use "
    "substitute values."
)
_CURRENT_ARTIFACT_CORRECTION_GUIDANCE = (
    "If a deterministic finding reports an undeclared evidence "
    "root or binding, replace the read with a standard packet root or the declared "
    "evidence.bindings.<binding_name> path. Runtime bindings come from the accepted "
    "plan, and artifact authoring cannot add, rename, or change one, so a correction "
    "cannot declare a new binding. Never read evidence.state or hardcode a "
    "supplied record fact. If a binding lists stimulus.user_text, keep that consumer "
    "only when its exact resolved value occurs in the authored text or the text "
    "contains its {{binding_name}} slot; otherwise remove the consumer."
)
_CURRENT_JUDGE_CORRECTION_GUIDANCE = (
    " For a judge-enabled package, treat runner-normalized "
    "evidence.judge.verdict unresolved with evidence_refs [] as inconclusive. "
    "For supported or contradicted verdicts, cite only judge.evidence_refs as "
    "judge support. Each reference resolves to captured message content or a "
    "non-null tool-call result value; call metadata, arguments, and null results "
    "are unusable. Do not cite or repair judge references in detector code."
)


def build_correction_context(
    *,
    failed_stage: str,
    original_context: dict[str, Any],
    current_output: bytes | str,
    findings: list[dict[str, Any]] | tuple[dict[str, Any], ...] | list[Finding],
    prior_unresolved_findings: list[dict[str, Any]] | None = None,
    detector_feedback: list[DetectorControlFeedback]
    | tuple[DetectorControlFeedback, ...]
    | None = None,
) -> dict[str, Any]:
    """Build a stage-aware correction context without competing formats."""

    stage = (
        "plan"
        if failed_stage in {"plan", "call1", "plan_review"}
        else "artifact"
        if failed_stage in {"artifact", "call2", "artifact_review"}
        else failed_stage
    )
    if isinstance(current_output, bytes):
        output_text, output_encoding = _readable_response(current_output)
    else:
        output_text, output_encoding = current_output, "text-input"
    normalized_findings = [
        finding.to_dict() if isinstance(finding, Finding) else deepcopy(finding)
        for finding in findings
    ]
    if detector_feedback:
        control_summaries = [
            {
                "code": finding.get("code", "detector_control_failure"),
                "detail": "See the shared feedback section for the exact executed case.",
                "path": finding.get("path", "detector_controls"),
            }
            for finding in normalized_findings
            if str(finding.get("path", "")).startswith("detector_controls.")
        ]
        normalized_findings = [
            finding
            for finding in normalized_findings
            if not str(finding.get("path", "")).startswith("detector_controls.")
        ]
        normalized_findings.extend(control_summaries)
    instruction = (
        "Address every substantiated finding together. Verify criticism against "
        "the original scenario and supplied evidence, preserve supported meaning, "
        "and retain an essential unsupported requirement as unresolved instead "
        "of inventing facts."
    )
    if failed_stage in {"plan_review", "artifact_review"} or any(
        isinstance(finding, Finding) and finding.code == "semantic_review" for finding in findings
    ):
        instruction += (
            " The CURRENT FINDINGS contain only semantic-review findings whose "
            "question IDs are in the closed scope for this stage. Out-of-scope "
            "review findings are intentionally omitted; do not reconstruct or "
            "address them."
        )
    context: dict[str, Any] = {
        "stage": stage,
        "failed_stage": failed_stage,
        "original_context": deepcopy(original_context),
        "current_output": output_text,
        "current_output_encoding": output_encoding,
        "findings": normalized_findings,
        "instruction": instruction,
    }
    if stage == "artifact":
        accepted_plan = original_context.get("accepted_plan")
        runtime_evidence_interface = original_context.get("runtime_evidence_interface")
        runtime_contract = (
            runtime_evidence_interface.get("runtime_contract")
            if isinstance(runtime_evidence_interface, dict)
            else None
        )
        context["observation_guide"] = artifact_observation_guide(
            accepted_plan if isinstance(accepted_plan, dict) else {},
            runtime_contract if isinstance(runtime_contract, dict) else None,
            omission=_context_has_not_called(original_context),
        )
        context["evidence_packet_interface"] = _render_evidence_packet_interface(
            claim_level=_plan_claim_level(accepted_plan),
            required_observations=(
                accepted_plan.get("required_observations")
                if isinstance(accepted_plan, dict)
                else None
            ),
            semantic_judge_needed=_plan_semantic_judge_needed(accepted_plan),
        )
    if detector_feedback:
        context["detector_feedback"] = build_detector_feedback_prompt_context(detector_feedback)
    if prior_unresolved_findings:
        context["prior_unresolved_findings"] = deepcopy(prior_unresolved_findings)
    if stage == "plan":
        context.update(
            {
                "accepted_plan_fixed": False,
                "format": (
                    "Return one complete plan replacement as one bare JSON object or "
                    "exactly one lowercase ```json fenced JSON object."
                ),
                "response_contract": _call1_contract_v2(),
            }
        )
        context["instruction"] = (
            instruction + " Call 1 uses one bare JSON object or exactly one lowercase ```json "
            "fenced JSON object. " + _PLAN_CORRECTION_GUIDANCE
        )
    elif stage == "artifact":
        context.update(
            {
                "accepted_plan_fixed": True,
                "format": (
                    "Return exactly one ```json metadata block followed by one raw "
                    "python block using the artifact response contract."
                ),
                "response_contract": _call2_contract_v2(
                    original_context.get("accepted_plan")
                    if isinstance(original_context, dict)
                    else None,
                ),
            }
        )
        context["instruction"] = (
            instruction + " Call 2 uses exactly one JSON metadata block followed by one raw "
            "Python block. " + _ARTIFACT_CORRECTION_GUIDANCE + _CURRENT_JUDGE_CORRECTION_GUIDANCE
        )
    else:
        raise ValueError(f"unsupported correction stage: {failed_stage}")
    return context


def _render_correction_sections(sections: tuple[tuple[str, Any], ...]) -> str:
    """Render correction sections compactly while preserving each value exactly."""

    rendered: list[str] = []
    for title, value in sections:
        rendered.append(title)
        rendered.append(
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )
        rendered.append("")
    return "\n".join(rendered).rstrip() + "\n"


def _package_from_responses(
    *,
    view: InputView,
    plan: dict[str, Any],
    artifact: dict[str, Any],
    task_id: str,
    ledger: list[dict[str, Any]],
    raw_responses: dict[str, bytes],
    decoded_responses: dict[str, Any],
    prompt_packets: dict[str, PromptPacket],
    transformations: list[Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    discovery_provenance: dict[str, Any] | None = None,
    detector_bytes: bytes,
    policy: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    review_status: dict[str, str] | None = None,
    preserved_reviews: dict[str, dict[str, Any]] | None = None,
    terminal_status: str | None = None,
) -> ArtifactPackage:
    authoring_records: dict[str, bytes] = {}
    for index, record in enumerate(ledger, start=1):
        stage = record["stage"]
        package_record = dict(record)
        if terminal_status is not None:
            package_record["terminal_status"] = terminal_status
            package_record["stage_status"] = terminal_status
        authoring_records[f"authoring/{index:02d}-{stage}.json"] = (
            _canonical_json(package_record).encode("utf-8") + b"\n"
        )
        raw = raw_responses.get(record.get("raw_response_key", ""))
        if raw is None:
            raw = raw_responses.get(stage)
        if raw is not None:
            authoring_records[f"authoring/{index:02d}-{stage}.raw"] = raw
        prompt_user = record.get("prompt_user")
        if isinstance(prompt_user, str):
            authoring_records[f"authoring/{index:02d}-{stage}.prompt"] = prompt_user.encode(
                "utf-8"
            )
        else:
            packet = prompt_packets.get(stage)
            if packet is not None:
                authoring_records[f"authoring/{index:02d}-{stage}.prompt"] = packet.user.encode(
                    "utf-8"
                )
    authoring_records["authoring/transformations.json"] = (
        _canonical_json(transformations).encode("utf-8") + b"\n"
    )
    review_records = _package_review_records(
        ledger,
        review_status=review_status,
        preserved_reviews=preserved_reviews,
    )
    authoring_records["authoring/reviews.json"] = (
        _canonical_json(review_records).encode("utf-8") + b"\n"
    )
    package_ledger = [
        {
            **record,
            **(
                {
                    "terminal_status": terminal_status,
                    "stage_status": terminal_status,
                }
                if terminal_status is not None
                else {}
            ),
        }
        for record in ledger
    ]
    authoring_records["authoring/ledger.json"] = (
        _canonical_json(package_ledger).encode("utf-8") + b"\n"
    )
    authoring_input_pins = _expected_authoring_input_pins(
        input_view=view,
        inventory=inventory,
        runtime_contract=runtime_contract,
    )
    members = {
        "plan.json": _json_bytes(plan),
        "stimulus.json": _json_bytes(artifact["stimulus"]),
        "setup.json": _json_bytes(artifact["setup_recipe"]),
        "bindings.json": _json_bytes(artifact["runtime_bindings"]),
        "prerequisites.json": _json_bytes(artifact["prerequisites"]),
        "detector.py": detector_bytes,
        "checks.json": _json_bytes(
            {"interface": AUTHORING_INTERFACE_VERSION_V2, "status": "structurally_valid"}
        ),
        "inputs.json": _json_bytes(
            {
                "model_facing_input": _input_view_payload(view),
                "source_input": _source_input_payload(view),
                "inventory": inventory,
                "discovery_provenance": deepcopy(discovery_provenance or {}),
                "runtime_contract": runtime_contract,
                "authoring_input_pins": authoring_input_pins,
            }
        ),
        "source-hashes.json": _json_bytes(view.source_digests),
        "observations.json": _json_bytes(artifact["required_observations"]),
        "explanation.json": _json_bytes({"text": artifact["explanation"]}),
        "examples.json": _json_bytes(artifact["examples"]),
        **authoring_records,
    }
    resolved_judge = _resolved_judge_spec(artifact["semantic_judge_spec"], inventory)
    if resolved_judge is not None:
        members["judge.json"] = _json_bytes(resolved_judge)
    safe_ledger = [
        {
            key: value
            for key, value in (
                {
                    **record,
                    **(
                        {
                            "terminal_status": terminal_status,
                            "stage_status": terminal_status,
                        }
                        if terminal_status is not None
                        else {}
                    ),
                }
            ).items()
            if key not in {"prompt_system", "prompt_user"} and not (key == "usage" and not value)
        }
        for record in ledger
    ]

    def summary_usage(record: dict[str, Any]) -> dict[str, Any]:
        usage = record.get("usage")
        # Existing ledgers can already carry the failure-evidence metadata
        # envelope. Preserve it so the manifest scanner validates the closed
        # shape instead of treating the envelope as provider counters.
        if isinstance(usage, dict) and "availability" in usage:
            return deepcopy(usage)
        return metadata_record(
            usage if usage else None,
            unavailable_reason="provider_did_not_report_usage",
        )

    authoring_summary = {
        "interface": AUTHORING_INTERFACE_VERSION_V2,
        "attempts": len(ledger),
        "correction_used": any(record["stage"] == "correction" for record in ledger),
        "max_retries": 0,
        "usage": [summary_usage(record) for record in ledger],
        "ledger": safe_ledger,
        "authoring_input_pins": authoring_input_pins,
    }
    if policy is not None:
        authoring_summary["policy"] = dict(policy)
    if budget is not None:
        authoring_summary["budget"] = dict(budget)
    if review_status is not None:
        authoring_summary["review_status"] = dict(review_status)
    if terminal_status is not None:
        authoring_summary["status"] = terminal_status
        authoring_summary["terminal_status"] = terminal_status
    creation_model = {"model": "configured-private-authoring", "controls": {"max_retries": 0}}
    assert_no_secrets({"authoring": authoring_summary, "creation_model": creation_model})
    package_id = f"{task_id}-{view.scenario_id}"
    return build_package(
        package_id=package_id,
        scenario_id=view.scenario_id,
        input_kind=view.kind.value,
        source_digests=view.source_digests or {"input": view.source_sha256},
        members=members,
        authoring=authoring_summary,
        runtime_capabilities=runtime_contract,
        creation_model=creation_model,
    )


def _package_review_records(
    ledger: list[dict[str, Any]],
    *,
    review_status: dict[str, str] | None,
    preserved_reviews: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the package-owned review truth, including disabled stages."""

    records: dict[str, Any] = {}
    for stage, key in (("plan_review", "plan"), ("artifact_review", "artifact")):
        requested_status = (review_status or {}).get(key)
        matching = [
            record.get("review")
            for record in ledger
            if record.get("stage") == stage and isinstance(record.get("review"), dict)
        ]
        if requested_status == "not_requested":
            records[key] = {"status": "not_requested"}
        elif matching:
            records[key] = deepcopy(matching[-1])
        elif isinstance(preserved_reviews, dict) and isinstance(preserved_reviews.get(key), dict):
            records[key] = deepcopy(preserved_reviews[key])
        else:
            records[key] = {
                "status": requested_status or "not_requested",
            }
    return {
        "schema_version": "authoring-review-evidence-v1",
        "plan": records["plan"],
        "artifact": records["artifact"],
    }


def _persist_blocked_plan(destination: Path, plan: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = destination.with_suffix(destination.suffix + ".blocked.json")
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_bytes(
        _json_bytes({"status": "blocked", "plan": plan, "package_path": str(destination)})
    )
    temporary.replace(target)


_CORRECTION_SYSTEM_V3 = (
    "Correct the current output for the named authoring stage. Return a complete "
    "replacement in that stage's required format and address every substantiated "
    "finding together. Verify criticism against the original scenario and supplied "
    "evidence, preserve supported meaning, and retain an essential unsupported "
    "requirement as unresolved instead of inventing facts. Plan correction may revise "
    "the plan within the supplied context. Artifact correction keeps the accepted plan "
    "fixed and cannot rewrite setup, bindings, prerequisites, or observation level. "
    "Do not add target access, setup, judge calls, retries, or self-approval."
)
_CORRECTION_SYSTEM_V4 = _CORRECTION_SYSTEM_V3
_CORRECTION_SYSTEM_V5 = _CORRECTION_SYSTEM_V4


__all__ = [
    "AUTHORING_INTERFACE_VERSION_V2",
    "MAX_AUTHOR_CORRECTION_REQUESTS_PER_TASK",
    "MAX_REVIEW_REQUESTS_PER_TASK",
    "AuthoringBudget",
    "AuthoringError",
    "AuthoringOrchestrator",
    "AuthoringPolicy",
    "AuthoringResult",
    "ArtifactValidationError",
    "SuppliedControlCases",
    "artifact_observation_guide",
    "ARTIFACT_REVIEW_PROMPT_VERSION",
    "BudgetExceeded",
    "Call1FramingError",
    "CALL1_PROMPT_VERSION_V18",
    "CALL2_PROMPT_VERSION_V21",
    "CORRECTION_PROMPT_VERSION_V25",
    "CORRECTION_PROMPT_VERSION_V27",
    "Call2FramingError",
    "Finding",
    "PlanValidationError",
    "PLAN_REVIEW_PROMPT_VERSION",
    "PLAN_REVIEW_PROMPT_VERSION_V17",
    "ARTIFACT_REVIEW_PROMPT_VERSION_V16",
    "PLAN_FIELD_MEANINGS",
    "NEUTRAL_PLAN_OUTCOME_EXAMPLE",
    "NEUTRAL_OMISSION_OUTCOME_EXAMPLE",
    "NEUTRAL_ESTABLISHED_OMISSION_OUTCOME_EXAMPLE",
    "PromptPacket",
    "ParsedCall2Response",
    "ReviewResponse",
    "ReviewResponseError",
    "PrivateModelAuthoringTransport",
    "PromptPreflightError",
    "PromptOverflowError",
    "TransportResponse",
    "assert_no_secrets",
    "assert_no_prompt_secrets",
    "neutral_artifact_plan_v2",
    "build_artifact_review_packet",
    "build_artifact_author_context",
    "build_call1_packet_v2",
    "build_call2_packet_v2",
    "build_correction_context",
    "build_plan_review_packet",
    "build_plan_author_context",
    "build_plan_reviewer_context",
    "build_artifact_reviewer_context",
    "PLAN_REVIEW_QUESTION_IDS",
    "ARTIFACT_REVIEW_QUESTION_IDS",
    "evidence_packet_contract",
    "collect_artifact_findings_v2",
    "collect_plan_findings",
    "collect_plan_findings_v2",
    "parse_review_response",
    "policy_max_dispatches",
    "policy_role_limits",
    "run_detector_controls",
    "load_failure_evidence",
    "neutral_observation_cases",
    "neutral_artifact_response_without_source",
    "neutral_artifact_plan",
    "scan_for_secrets",
    "prompt_data_urls",
    "scan_for_prompt_secrets",
    "scan_prompt_duplicates",
    "assert_no_prompt_duplicates",
    "parse_call2_response",
]
