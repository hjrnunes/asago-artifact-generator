"""The tool-call-condition evaluator, re-exported from the ``asago-package`` member.

The validator and the evaluator live in ``packages/asago-package``, where authoring, the
adapters and the orchestrator's probe read them without the authoring application. This
module keeps the consumer's old import path working and binds the same objects, so
``asago_artifact_generator.authoring.condition_eval`` and
``asago_package.condition_eval`` are one implementation.
"""

from __future__ import annotations

from asago_package.condition_eval import (
    DETECTED,
    INCONCLUSIVE,
    NOT_DETECTED,
    ConditionEvaluation,
    evaluate_condition,
    validate_condition,
)

__all__ = [
    "DETECTED",
    "INCONCLUSIVE",
    "NOT_DETECTED",
    "ConditionEvaluation",
    "evaluate_condition",
    "validate_condition",
]
