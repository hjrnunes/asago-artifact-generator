"""Run the handoff's tool-call condition on the artifact's own example captures.

The producer's tool-call condition decides a command_attempt package at run time.
An author who writes an unsafe example that the condition does not detect, or a
safe example that it does detect, has built an artifact whose own examples
contradict its oracle. The check reads no model output beyond the captures and
makes no model call: it evaluates the condition on each capture and reports a
disagreement. The finding names the example and the evaluator's reason and
leaves the repair to the author.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .condition_eval import DETECTED, NOT_DETECTED, evaluate_condition, validate_condition
from .core import Finding, staged_findings

QUIET_CODE = "oracle_quiet_on_unsafe_example"
FIRES_CODE = "oracle_fires_on_safe_example"
UNDECIDED_CODE = "oracle_inconclusive_on_safe_example"
ORACLE_CODES = (QUIET_CODE, FIRES_CODE, UNDECIDED_CODE)
CONDITION_PATH = "tool_call_condition"


def oracle_self_test_findings(examples: Mapping[str, Any], condition: Any) -> list[Finding]:
    """Return a finding for each captured example that the condition scores wrongly.

    The unsafe capture must be detected and the safe capture must be not_detected.
    A missing or invalid condition decides nothing, and the producer owns it, so
    it yields no finding. The captures must already pass the shape checks.
    """

    if validate_condition(condition) is not None:
        return []
    findings: list[Finding] = []
    for label, wanted in (("unsafe", DETECTED), ("safe", NOT_DETECTED)):
        example = examples.get(label)
        capture = example.get("capture") if isinstance(example, Mapping) else None
        if not isinstance(capture, Mapping):
            continue
        calls = [
            {"name": call["operation"], "arguments": call["arguments"]}
            for call in capture["tool_calls"]
        ]
        result = evaluate_condition(condition, calls, capture["complete"])
        if result.outcome == wanted:
            continue
        findings.append(
            Finding(
                _code(label, result.outcome),
                _detail(label, result.outcome, result.reason, wanted),
                CONDITION_PATH,
                {
                    "example": label,
                    "outcome": result.outcome,
                    "reason": result.reason,
                    "matched_calls": list(result.matched_calls),
                },
            )
        )
    return staged_findings(findings, "artifact")


def _code(label: str, outcome: str) -> str:
    if label == "unsafe":
        return QUIET_CODE
    return FIRES_CODE if outcome == DETECTED else UNDECIDED_CODE


def _detail(label: str, outcome: str, reason: str, wanted: str) -> str:
    verdict = {
        DETECTED: "detects",
        NOT_DETECTED: "does not detect",
    }.get(outcome, "cannot decide")
    return (
        f"The scenario's tool-call condition {verdict} examples.{label}: the evaluator "
        f"returned {outcome} ({reason}). The {label} example must score {wanted} under the "
        "condition."
    )


__all__ = [
    "CONDITION_PATH",
    "FIRES_CODE",
    "ORACLE_CODES",
    "QUIET_CODE",
    "UNDECIDED_CODE",
    "oracle_self_test_findings",
]
