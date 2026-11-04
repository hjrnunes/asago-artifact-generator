"""The tool-call-condition-v2 evaluator, re-exported from the shared ``asago_handoff``.

The one definition lives in ``asago_handoff.condition_eval``, the producer's package, where
authoring, the adapters, the injection-spec compiler and the orchestrator's probe read it.
This module keeps the consumer's old import path working and binds the same objects.
"""

from __future__ import annotations

from asago_handoff.condition_eval import (
    DETECTED,
    INCONCLUSIVE,
    NOT_DETECTED,
    ConditionEvaluation,
    evaluate_condition,
    evaluate_condition_v2,
    validate_condition,
)

__all__ = [
    "DETECTED",
    "INCONCLUSIVE",
    "NOT_DETECTED",
    "ConditionEvaluation",
    "evaluate_condition",
    "evaluate_condition_v2",
    "validate_condition",
]
