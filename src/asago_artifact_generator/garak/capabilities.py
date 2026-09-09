"""Deterministic capability declaration for the STPA Garak adapter.

The declaration is intentionally conservative.  Garak can represent a static
chat transcript and inspect emitted tool calls or text, but this adapter does
not claim to provide a real clock, persistent state, multi-agent execution, or
state observers merely because a prompt can mention those concepts.
"""

from __future__ import annotations

from ..models.readiness import PlatformCapabilities

GARAK_ADAPTER_VERSION = "garak-stpa-v1"


def garak_capabilities() -> PlatformCapabilities:
    """Return the immutable, platform-owned Garak capability facts."""

    return PlatformCapabilities(
        platform="garak",
        adapter_version=GARAK_ADAPTER_VERSION,
        writable_surfaces=(
            "system_prompt",
            "user_turn",
            "tool_call",
            "tool_result",
            "tool_definition",
        ),
        invocable_operations=("chat_completion", "tool_call"),
        observer_kinds=("tool_call", "tool_argument", "output_text", "event_order"),
        supports_multi_turn=True,
        supports_persistent_state=False,
        supports_multi_agent=False,
        supports_real_clock=False,
        supports_state_observation=False,
    )


__all__ = ["GARAK_ADAPTER_VERSION", "garak_capabilities"]
