"""Deprecated alias for :mod:`asago_bundle_midojo.injection_spec`, kept for one release.

MiDojo renamed its document to the injection spec v1, and this adapter followed. Import
:mod:`asago_bundle_midojo.injection_spec`; every name here is that module's, and this module
goes away at M9.
"""

from __future__ import annotations

from .injection_spec import (
    EVERY,
    FIRST,
    HOST_INTERCEPTION,
    IDENTITY,
    INJECTION_SPEC_FIELDS,
    PROBE_TASK,
    STRICT,
    STRUCTURED_CONTENT,
    TOOL_OUTPUT,
    UNMANAGED_RUNTIME,
    injection_plan,
    injection_spec,
    probe_key,
    tool_output_instruction,
)

# The document's old name, and the builder that writes it.
EPISODE_FIELDS = INJECTION_SPEC_FIELDS
episode = injection_spec

__all__ = [
    "EVERY",
    "EPISODE_FIELDS",
    "FIRST",
    "HOST_INTERCEPTION",
    "IDENTITY",
    "INJECTION_SPEC_FIELDS",
    "PROBE_TASK",
    "STRICT",
    "STRUCTURED_CONTENT",
    "TOOL_OUTPUT",
    "UNMANAGED_RUNTIME",
    "episode",
    "injection_plan",
    "injection_spec",
    "probe_key",
    "tool_output_instruction",
]
