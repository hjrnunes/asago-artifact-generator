"""Target-free two-call authoring orchestration.

The model owns experiment meaning and detector source.  This module owns the
small mechanical surface around that response: deterministic prompt views,
closed structural checks, request accounting, and immutable package assembly.
It never contacts a target, setup transport, discovery service, or judge.
"""

from __future__ import annotations

from ..detector_controls import run_detector_controls
from .checks import (
    collect_artifact_findings_v2,
    collect_plan_findings,
    collect_plan_findings_v2,
    parse_call2_response,
)
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
from .controls import SuppliedControlCases
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
from .correction import build_correction_context
from .orchestrator import AuthoringOrchestrator
from .policy import (
    AuthoringBudget,
    AuthoringPolicy,
    AuthoringResult,
    policy_max_dispatches,
    policy_role_limits,
)
from .prompt_context import (
    artifact_observation_guide,
    build_artifact_author_context,
    build_plan_author_context,
)
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
from .review import (
    ARTIFACT_REVIEW_QUESTION_IDS,
    PLAN_REVIEW_QUESTION_IDS,
    build_artifact_review_packet,
    build_artifact_reviewer_context,
    build_plan_review_packet,
    build_plan_reviewer_context,
    parse_review_response,
)
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
