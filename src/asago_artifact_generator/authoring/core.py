"""Shared authoring types, prompt versions, request limits, and small helpers.

Errors, findings, prompt packets, and parsed responses live here so that the
other submodules depend on one common base.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..failure_evidence import redact_metadata

AUTHORING_INTERFACE_VERSION_V2 = "artifact-authoring-v2"
# The response wire remains v2, while its model-facing templates advance
# independently; each constant names the template version dispatched now.
CALL1_PROMPT_VERSION_V18 = "authoring-call1-v18"
CALL2_PROMPT_VERSION_V21 = "authoring-call2-v21"
CORRECTION_PROMPT_VERSION_V25 = "authoring-correction-v25"
CORRECTION_PROMPT_VERSION_V27 = "authoring-correction-v27"
# Semantic-review roles.  Each review is a separate provider request recorded
# beside the author dispatches; the reviewer contract is the small closed
# decision/summary/findings shape parsed by ``parse_review_response``.
PLAN_REVIEW_PROMPT_VERSION_V17 = "authoring-plan-review-v17"
ARTIFACT_REVIEW_PROMPT_VERSION_V16 = "authoring-artifact-review-v16"
PLAN_REVIEW_PROMPT_VERSION = PLAN_REVIEW_PROMPT_VERSION_V17
ARTIFACT_REVIEW_PROMPT_VERSION = ARTIFACT_REVIEW_PROMPT_VERSION_V16
_REVIEW_STAGES = frozenset({"plan_review", "artifact_review"})


# New authoring/review dispatches share the approved aggregate ceiling.  A
# policy-driven task derives its own per-task and per-role limits from
# policy_max_dispatches/policy_role_limits; these constants are the defaults for
# a budget built without a policy.
MAX_AUTHORING_REQUESTS = 32
# One revision requested by semantic review per reviewed stage, independent of
# the stage's mechanical correction allowance.
REVIEW_REVISION_ALLOWANCE_PER_STAGE = 1
MAX_REQUESTS_PER_TASK = 8
MAX_AUTHOR_CORRECTION_REQUESTS_PER_TASK = 4
MAX_REVIEW_REQUESTS_PER_TASK = 4
MAX_RENDERED_PROMPT_BYTES = 1_000_000
AUTHORING_CONTEXT_WINDOW_TOKENS = 32_768
AUTHORING_MAX_COMPLETION_TOKENS = 8_192
_CONTEXT_FRAMING_TOKEN_RESERVE = 256
# The provider request adds a small JSON message/schema envelope around the
# rendered system and user content. This fixed UTF-8-byte allowance and the
# separate framing-token reserve stay in every context estimate.
_CONTEXT_MESSAGE_SCHEMA_OVERHEAD_BYTES = 128


# Normal private authoring sets thinking per role through the transport's
# additive extra_body.  Every role currently runs with thinking off: with
# thinking on, reviews on a measured open-weight model repeated the same reasoning lines
# until the completion limit and returned no answer in 11 of 35 scenarios.
# The values are non-secret and are recorded as per-call controls.
AUTHORING_THINKING_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}
REVIEW_THINKING_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False}}
_SLOT_RE = re.compile(r"\{\{([^{}]*)\}\}")
_PROMPT_URL_RE = re.compile(r"\bhttps?://[^\s\"'<>]+", re.IGNORECASE)
_PROMPT_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_-]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b"
)


class AuthoringTransport(Protocol):
    """Adapter for one provider request. Implementations must not retry."""

    max_retries: int

    def complete(self, packet: PromptPacket) -> TransportResponse | str | bytes: ...


class AuthoringError(ValueError):
    """Base class for deterministic authoring failures."""

    def __init__(self, message: str, path: str = "") -> None:
        self.message = message
        self.path = path
        super().__init__(message)


class PlanValidationError(AuthoringError):
    """Raised for structural Call 1 response errors."""


class ArtifactValidationError(AuthoringError):
    """Raised for structural or plan-consistency Call 2 response errors."""


class BudgetExceeded(AuthoringError):
    """Raised before dispatch when an aggregate, task, or role cap is exhausted."""

    def __init__(
        self,
        message: str,
        *,
        scope: str | None = None,
        task_id: str | None = None,
        role: str | None = None,
        used: int | None = None,
        limit: int | None = None,
    ) -> None:
        self.scope = scope
        self.task_id = task_id
        self.role = role
        self.used = used
        self.limit = limit
        super().__init__(message)


class PromptPreflightError(AuthoringError):
    """Raised when a rendered prompt fails a before-dispatch guard."""


class PromptOverflowError(PromptPreflightError):
    """Raised before dispatch when a complete prompt exceeds its explicit bound."""

    def __init__(
        self,
        message: str,
        *,
        estimated_prompt_tokens: int | None = None,
        remaining_input_budget: int | None = None,
        total_model_facing_utf8_bytes: int | None = None,
    ) -> None:
        self.estimated_prompt_tokens = estimated_prompt_tokens
        self.remaining_input_budget = remaining_input_budget
        self.total_model_facing_utf8_bytes = total_model_facing_utf8_bytes
        super().__init__(message)


@dataclass(frozen=True)
class Finding:
    """A typed, deterministic finding retained beside the failed response."""

    code: str
    detail: str
    path: str = ""
    details: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def to_dict(self) -> dict[str, Any]:
        result = {"code": self.code, "detail": self.detail}
        if self.path:
            result["path"] = self.path
        if self.details:
            result["details"] = deepcopy(self.details)
        return result


def _prompt_overflow_finding(exc: PromptOverflowError, stage: str) -> Finding:
    """Retain the calibrated estimate and remaining input budget as data."""

    details = {
        key: value
        for key, value in (
            ("estimated_prompt_tokens", exc.estimated_prompt_tokens),
            ("remaining_input_budget_estimate", exc.remaining_input_budget),
            ("model_facing_utf8_bytes", exc.total_model_facing_utf8_bytes),
        )
        if value is not None
    }
    return Finding("prompt_overflow", str(exc), stage, details)


def _prompt_preflight_finding(exc: PromptPreflightError, stage: str) -> Finding:
    """Keep size and calibrated context rejections on one terminal path."""

    if isinstance(exc, PromptOverflowError):
        return _prompt_overflow_finding(exc, stage)
    return Finding("prompt_preflight", str(exc), stage)


@dataclass(frozen=True)
class PromptPacket:
    """A fully rendered, versioned prompt and its stage-local request payload."""

    stage: str
    version: str
    system: str
    user: str
    payload: dict[str, Any]

    @property
    def byte_size(self) -> int:
        """Return the exact UTF-8 bytes sent as the two prompt messages."""

        return len(self.system.encode("utf-8")) + len(self.user.encode("utf-8"))

    @property
    def sha256(self) -> str:
        """Return the digest of the exact rendered role prompt."""

        rendered = "\0".join((self.stage, self.version, self.system, self.user))
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParsedCall2Response:
    """The v2 metadata object and exact bytes between the Python fences."""

    metadata: dict[str, Any]
    python_bytes: bytes

    @property
    def python_source(self) -> str:
        """Decode the source for syntax validation without changing its bytes."""

        return self.python_bytes.decode("utf-8")


class Call2FramingError(AuthoringError):
    """Raised when a v2 Call 2 response is not exactly two fenced blocks."""

    def __init__(self, findings: list[Finding]) -> None:
        self.findings = list(findings)
        detail = "; ".join(finding.detail for finding in self.findings)
        super().__init__(detail or "invalid Call 2 framing", "call2")


class Call1FramingError(AuthoringError):
    """Raised when a v2 Call 1 response has unsupported outer framing."""

    def __init__(self, findings: list[Finding]) -> None:
        self.findings = list(findings)
        detail = "; ".join(finding.detail for finding in self.findings)
        super().__init__(detail or "invalid Call 1 framing", "call1")


class ReviewResponseError(AuthoringError):
    """Raised when a reviewer response fails framing, shape, or consistency."""

    def __init__(self, findings: list[Finding]) -> None:
        self.findings = list(findings)
        detail = "; ".join(finding.detail for finding in self.findings)
        super().__init__(detail or "invalid reviewer response", "review")


@dataclass(frozen=True)
class ReviewResponse:
    """One validated semantic-review response.

    ``decision`` is ``accept``, ``revise``, or ``blocked``; ``findings`` is a
    tuple of complete finding objects with exactly ``question``, ``location``,
    ``problem``, ``basis``, and ``required_change``. The question is scoped
    against the stage's closed question list after parsing.
    """

    decision: str
    summary: str
    findings: tuple[dict[str, Any], ...] = ()
    transformation: str | None = None


@dataclass(frozen=True)
class TransportResponse:
    """Raw provider response plus non-secret provider metadata."""

    raw: bytes
    usage: dict[str, Any] | None = None
    controls: dict[str, Any] | None = None
    response_capture: dict[str, Any] | None = None
    provider_model: str | None = None


def _claim_levels() -> tuple[str, ...]:
    return ("command_attempt", "reply", "returned_result", "state_effect")


# Capturing a decoded tool result or a snapshot does not mean downstream
# execution accepts a claim at that level, so result- and state-level claims
# need an explicit runtime_contract.observation.claim_levels declaration.
_DEFAULT_SUPPORTED_CLAIM_LEVELS = ("command_attempt", "reply")


def _supported_claim_levels(runtime_contract: Any) -> tuple[str, ...]:
    observation = (
        runtime_contract.get("observation") if isinstance(runtime_contract, dict) else None
    )
    declared = observation.get("claim_levels") if isinstance(observation, dict) else None
    if isinstance(declared, list):
        return tuple(level for level in _claim_levels() if level in declared)
    return _DEFAULT_SUPPORTED_CLAIM_LEVELS


def _mapping_sha256(value: dict[str, Any]) -> str:
    return _sha256(_canonical_json(value).encode("utf-8"))


def _findings_from_error(exc: Exception) -> list[Finding]:
    text = str(exc)
    code = "plan_validation" if isinstance(exc, PlanValidationError) else "artifact_validation"
    if text.startswith("unknown_reference:"):
        code = "unknown_reference"
    elif text.startswith("plan_conflict:"):
        code = "plan_conflict"
    elif text.startswith("undocumented selector"):
        code = "undocumented_selector"
    elif "type mismatch" in text:
        code = "schema_type_mismatch"
    elif text.startswith("schema_type_mismatch:"):
        code = "schema_type_mismatch"
    elif "not permitted" in text:
        code = "unpermitted_setup"
    elif text.startswith("non_user_history"):
        code = "non_user_history"
    return [Finding(code, text)]


def _safe_metadata(value: Any) -> dict[str, Any]:
    return redact_metadata(value) if isinstance(value, dict) else {}


def _set_record_usage(record: dict[str, Any], usage: Any) -> None:
    """Persist provider usage only when the transport supplied it."""

    if usage is None:
        record.pop("usage", None)
    else:
        record["usage"] = _safe_metadata(usage)


def _safe_error(exc: BaseException) -> str:
    text = str(exc)
    text = re.sub(r"https?://[^\s)]+", "<redacted-url>", text)
    text = re.sub(
        r"(api[_-]?key|authorization|token|password)=?[^\s,;]+",
        r"\1=<redacted>",
        text,
        flags=re.I,
    )
    return text


def _model_dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        try:
            result = value.model_dump(exclude_none=True)
        except TypeError:
            result = value.model_dump()
    elif isinstance(value, dict):
        result = value
    else:
        result = {}
    return result if isinstance(result, dict) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_bytes(value: Any) -> bytes:
    return (_canonical_json(value) + "\n").encode("utf-8")


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return not isinstance(value, float) or value == value
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _json_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_schema_type(value: Any, schema_type: str) -> bool:
    if isinstance(value, str) and _SLOT_RE.fullmatch(value):
        return True
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    return True


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
