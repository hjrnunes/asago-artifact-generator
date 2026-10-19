"""The MiDojo verifiers of a bundle: a neutral utility check and the rubric judge.

This file is copied into the bundle's suite package and runs in MiDojo's
interpreter, which has none of the consumer's packages. It imports the standard
library; ``midojo.verifiers`` is imported when a verifier is registered or asked
for a result, so the module also loads where MiDojo is not installed. A command
attempt is graded by MiDojo's builtin ``tool_call_condition`` predicate, so no
condition verifier lives here.
"""

from __future__ import annotations

from typing import Any

from .verifier_names import NEUTRAL_NAME

NEUTRAL_REASON = "no utility check for an adversarial-only package"


class NeutralVerifier:
    """A utility check that always passes: an adversarial-only package has no task to grade."""

    name = NEUTRAL_NAME

    def parse(self, spec: Any) -> None:
        if spec not in ({}, None):
            raise ValueError(f"{NEUTRAL_NAME} takes no arguments")

    def assess(self, check: None, ctx: Any) -> Any:
        from midojo.verifiers import VerificationResult

        return VerificationResult(True, NEUTRAL_REASON)

    def evaluate(self, check: None, ctx: Any) -> bool:
        return True


def register() -> None:
    """Register the verifiers; a module imported twice registers them once."""

    from midojo.verifiers import register_verifier

    from .asago_rubric import RubricJudgeVerifier

    for verifier in (RubricJudgeVerifier(), NeutralVerifier()):
        try:
            register_verifier(verifier)
        except ValueError:
            continue
