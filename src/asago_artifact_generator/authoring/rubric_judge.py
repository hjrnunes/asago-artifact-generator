"""The rubric judge: a re-export of ``midojo_sdk.rubric_judge``.

The judge moved into the MiDojo fork, where the judge service and the orchestrator's
qualification check read it. This module keeps the old import path working for this
package's callers and exports every name it had; the implementation, its fixtures and its
tests live in MiDojo (``midojo`` at ``tests/fixtures/rubric``).

The judge is code, not a contract: the judge spec stays part of the artifact-package
contract, and the service that answers MiDojo's ``midojo.judge.v1`` request stays in
``asago_bundle_midojo.judge_service``.
"""

from __future__ import annotations

from midojo_sdk.rubric_judge import (
    JUDGE_VERDICTS,
    KEY_NAME,
    MAX_COMPLETION_TOKENS,
    PROMPT_VERSION,
    RUBRIC_NAME,
    STATUS_LABELS,
    SYSTEM_PROMPT,
    TIMEOUT_SECONDS,
    UNSETTLED,
    JudgeCallError,
    assess_response,
    build_evidence,
    build_request,
    decode_payload,
    field,
    http_judge,
    is_complete,
    judge_messages,
    judge_output,
    record,
    request_body,
    request_count,
    request_text,
    status_label,
    tool_call_of,
)

__all__ = [
    "JUDGE_VERDICTS",
    "KEY_NAME",
    "MAX_COMPLETION_TOKENS",
    "PROMPT_VERSION",
    "RUBRIC_NAME",
    "STATUS_LABELS",
    "SYSTEM_PROMPT",
    "TIMEOUT_SECONDS",
    "UNSETTLED",
    "JudgeCallError",
    "assess_response",
    "build_evidence",
    "build_request",
    "decode_payload",
    "field",
    "http_judge",
    "is_complete",
    "judge_messages",
    "judge_output",
    "record",
    "request_body",
    "request_count",
    "request_text",
    "status_label",
    "tool_call_of",
]
