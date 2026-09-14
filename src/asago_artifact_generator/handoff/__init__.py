"""Scenario handoff reader package."""

from .reader import (
    HANDOFF_DIGEST_DOMAIN,
    HANDOFF_SCHEMA_VERSION,
    HandoffValidationError,
    HandoffVerification,
    ScenarioHandoff,
    VerifiedHandoff,
    load_scenario_handoff,
    ownership_violations,
    verify_vendored_kit,
)

__all__ = [
    "HANDOFF_DIGEST_DOMAIN",
    "HANDOFF_SCHEMA_VERSION",
    "HandoffValidationError",
    "HandoffVerification",
    "ScenarioHandoff",
    "VerifiedHandoff",
    "load_scenario_handoff",
    "ownership_violations",
    "verify_vendored_kit",
]
