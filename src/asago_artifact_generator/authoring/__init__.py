"""Target-free two-call authoring orchestration.

The model owns experiment meaning and detector source.  This module owns the
small mechanical surface around that response: deterministic prompt views,
closed structural checks, request accounting, and immutable package assembly.
It never contacts a target, setup transport, discovery service, or judge.
"""

from __future__ import annotations

from ..detector_controls import run_detector_controls
from ..failure_evidence import load_failure_evidence
from .checks import _binding_selector_type as _binding_selector_type
from .checks import _is_blocked_plan as _is_blocked_plan
from .checks import (
    collect_artifact_findings_v2,
    collect_plan_findings,
    collect_plan_findings_v2,
    parse_call2_response,
)
from .context_budget import CONTEXT_GUARD_CALIBRATION as CONTEXT_GUARD_CALIBRATION
from .context_budget import _context_budget_estimate as _context_budget_estimate
from .context_budget import _context_guard_ratio as _context_guard_ratio
from .context_budget import _enforce_context_budget as _enforce_context_budget
from .contracts import _NEUTRAL_DETECTOR_SOURCE as _NEUTRAL_DETECTOR_SOURCE
from .contracts import (
    NEUTRAL_ESTABLISHED_OMISSION_OUTCOME_EXAMPLE,
    NEUTRAL_OMISSION_OUTCOME_EXAMPLE,
    NEUTRAL_PLAN_OUTCOME_EXAMPLE,
    PLAN_FIELD_MEANINGS,
    evidence_packet_contract,
    neutral_artifact_plan,
    neutral_artifact_plan_v2,
    neutral_artifact_response_without_source,
    neutral_observation_cases,
)
from .contracts import _binding_contract as _binding_contract
from .contracts import _evidence_packet_contract as _evidence_packet_contract
from .controls import SuppliedControlCases
from .core import _CONTEXT_FRAMING_TOKEN_RESERVE as _CONTEXT_FRAMING_TOKEN_RESERVE
from .core import (
    ARTIFACT_REVIEW_PROMPT_VERSION,
    ARTIFACT_REVIEW_PROMPT_VERSION_V16,
    AUTHORING_INTERFACE_VERSION_V2,
    CALL1_PROMPT_VERSION_V18,
    CALL2_PROMPT_VERSION_V21,
    CORRECTION_PROMPT_VERSION_V25,
    CORRECTION_PROMPT_VERSION_V27,
    MAX_AUTHOR_CORRECTION_REQUESTS_PER_TASK,
    MAX_REVIEW_REQUESTS_PER_TASK,
    PLAN_REVIEW_PROMPT_VERSION,
    PLAN_REVIEW_PROMPT_VERSION_V17,
    ArtifactValidationError,
    AuthoringError,
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
)
from .core import AUTHORING_CONTEXT_WINDOW_TOKENS as AUTHORING_CONTEXT_WINDOW_TOKENS
from .core import AUTHORING_MAX_COMPLETION_TOKENS as AUTHORING_MAX_COMPLETION_TOKENS
from .core import AUTHORING_THINKING_EXTRA_BODY as AUTHORING_THINKING_EXTRA_BODY
from .core import MAX_AUTHORING_REQUESTS as MAX_AUTHORING_REQUESTS
from .core import MAX_RENDERED_PROMPT_BYTES as MAX_RENDERED_PROMPT_BYTES
from .core import REVIEW_THINKING_EXTRA_BODY as REVIEW_THINKING_EXTRA_BODY
from .core import _json_bytes as _json_bytes
from .core import _sha256 as _sha256
from .correction import (
    _CURRENT_ARTIFACT_CORRECTION_GUIDANCE as _CURRENT_ARTIFACT_CORRECTION_GUIDANCE,
)
from .correction import _correction_detector_feedback_view as _correction_detector_feedback_view
from .correction import _render_correction_packet as _render_correction_packet
from .correction import build_correction_context
from .orchestrator import AuthoringOrchestrator
from .package_assembly import _package_from_responses as _package_from_responses
from .policy import (
    AuthoringBudget,
    AuthoringPolicy,
    AuthoringResult,
    policy_max_dispatches,
    policy_role_limits,
)
from .prompt_context import _CURRENT_PLAN_AUTHOR_GUIDANCE as _CURRENT_PLAN_AUTHOR_GUIDANCE
from .prompt_context import (
    artifact_observation_guide,
    build_artifact_author_context,
    build_plan_author_context,
)
from .prompt_context import scenario_provenance_ids as scenario_provenance_ids
from .prompt_packets import build_call1_packet_v2, build_call2_packet_v2
from .prompt_safety import (
    assert_no_prompt_duplicates,
    assert_no_prompt_secrets,
    assert_no_secrets,
    prompt_data_urls,
    scan_for_prompt_secrets,
    scan_for_secrets,
    scan_prompt_duplicates,
)
from .review import _PLAN_REVIEW_QUESTIONS as _PLAN_REVIEW_QUESTIONS
from .review import (
    ARTIFACT_REVIEW_QUESTION_IDS,
    PLAN_REVIEW_QUESTION_IDS,
    build_artifact_review_packet,
    build_artifact_reviewer_context,
    build_plan_review_packet,
    build_plan_reviewer_context,
    parse_review_response,
)
from .review import _review_finding_to_finding as _review_finding_to_finding
from .transport import PrivateModelAuthoringTransport

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
