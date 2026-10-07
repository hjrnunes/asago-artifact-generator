"""Run the handoff's tool-call condition on the artifact's own example captures.

The producer's tool-call condition decides a command_attempt package at run time.
An author who writes an unsafe example that the condition does not detect, or a
safe example that it does detect, has built an artifact whose own examples
contradict its oracle. The check reads no model output beyond the captures and
makes no model call: it evaluates the condition on each capture and reports a
disagreement. The finding names the example and the evaluator's reason and
leaves the repair to the author.

The condition belongs to the producer and the plan is fixed during artifact
authoring, so no correction can change what the condition decides. A package
whose examples disagree with the condition stops there, with one defect finding
that carries the condition and the outcome on each example.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .condition_eval import (
    DETECTED,
    NOT_DETECTED,
    ConditionEvaluation,
    evaluate_condition,
    validate_condition,
)
from .core import Finding, staged_findings

QUIET_CODE = "oracle_quiet_on_unsafe_example"
FIRES_CODE = "oracle_fires_on_safe_example"
UNDECIDED_CODE = "oracle_inconclusive_on_safe_example"
ORACLE_CODES = (QUIET_CODE, FIRES_CODE, UNDECIDED_CODE)
DEFECT_CODE = "oracle_condition_defective"
CONDITION_PATH = "tool_call_condition"
_REQUIRED = (("unsafe", DETECTED), ("safe", NOT_DETECTED))


def oracle_self_test_findings(examples: Mapping[str, Any], condition: Any) -> list[Finding]:
    """Return a finding for each captured example that the condition scores wrongly.

    The unsafe capture must be detected and the safe capture must be not_detected.
    A missing or invalid condition decides nothing, and the producer owns it, so
    it yields no finding. The captures must already pass the shape checks.
    """

    findings: list[Finding] = []
    for label, wanted, result in _evaluations(examples, condition):
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


def oracle_defect_findings(
    findings: Sequence[Finding], document: Any, condition: Any
) -> list[Finding]:
    """Return the one defect finding that ends a package whose examples contradict the condition.

    ``findings`` are the artifact checks' findings for the Call 2 ``document``;
    the defect exists only when they include a per-example oracle finding. The
    defect repeats the evaluation on every captured example so that the
    evidence shows each outcome.
    """

    if not any(finding.code in ORACLE_CODES for finding in findings):
        return []
    examples = document["examples"]
    outcomes = {
        label: {
            "outcome": result.outcome,
            "reason": result.reason,
            "matched_calls": list(result.matched_calls),
            "required": wanted,
        }
        for label, wanted, result in _evaluations(examples, condition)
    }
    detail = (
        "The examples contradict the handoff's tool-call condition; the producer owns "
        "the condition and it is fixed for this package: "
        + "; ".join(
            f"examples.{label} must score {item['required']} and scored {item['outcome']}"
            for label, item in outcomes.items()
        )
        + "."
    )
    details = {"condition": condition, "examples": outcomes}
    return staged_findings([Finding(DEFECT_CODE, detail, CONDITION_PATH, details)], "artifact")


def _evaluations(
    examples: Mapping[str, Any], condition: Any
) -> list[tuple[str, str, ConditionEvaluation]]:
    """Evaluate the condition on each captured example with the outcome that example requires."""

    if validate_condition(condition) is not None:
        return []
    evaluations = []
    for label, wanted in _REQUIRED:
        example = examples.get(label)
        capture = example.get("capture") if isinstance(example, Mapping) else None
        if not isinstance(capture, Mapping):
            continue
        calls = [
            {"name": call["operation"], "arguments": call["arguments"]}
            for call in capture["tool_calls"]
        ]
        evaluations.append(
            (label, wanted, evaluate_condition(condition, calls, capture["complete"]))
        )
    return evaluations


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
    "DEFECT_CODE",
    "FIRES_CODE",
    "ORACLE_CODES",
    "QUIET_CODE",
    "UNDECIDED_CODE",
    "oracle_defect_findings",
    "oracle_self_test_findings",
]
