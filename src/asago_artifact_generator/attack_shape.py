"""Re-export of the shared ``attack_shape`` rule.

``asago_handoff.shape`` states the rule the producer's ``AttackShape`` model
checks. The producer, the consumer and the orchestrator use it; this module
keeps the import path the adapter's callers and tests already have.
"""

from __future__ import annotations

from asago_handoff.shape import (
    CHANNEL_DIRECT,
    CHANNEL_FORGED,
    CHANNEL_INDIRECT,
    attack_shape_violation,
)

__all__ = [
    "CHANNEL_DIRECT",
    "CHANNEL_FORGED",
    "CHANNEL_INDIRECT",
    "attack_shape_violation",
]
