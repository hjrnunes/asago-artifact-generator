"""Deprecated alias for :mod:`asago_injection_spec.injection_spec_compile`, kept until M9.

MiDojo renamed its document to the injection spec v1, and this adapter followed. The
compiler now lives in the ``asago-injection-spec`` member; import
:mod:`asago_injection_spec.injection_spec_compile`. ``compile_episode`` here is
``compile_injection_spec``, ``episode_document`` is ``injection_spec_document``, and
``EPISODE_FILE`` is the file compile wrote before the rename. This module goes away at M9.
"""

from __future__ import annotations

from asago_injection_spec.injection_spec_compile import (
    BLOCK,
    CLAIM_LEVELS,
    COMMAND_ATTEMPT,
    CONDITION_MEMBER,
    FORWARD,
    INJECTION_SPEC_FILE,
    INPUTS_MEMBER,
    JUDGE_MARGIN_SECONDS,
    JUDGE_MEMBER,
    JUDGE_TOKEN_ENV,
    JUDGE_URL_ENV,
    LEGACY_EPISODE_FILE,
    PLANTED_ITEM,
    REPLY,
    SEED_MEMBER,
    TOOL,
    TOOL_CALL_CONDITION,
    TOOL_REVISION_RANGE,
    WRITE_DISPOSITIONS,
    CompileError,
    allowed_tools,
    claim_level,
    compile_injection_spec,
    delivery_mode,
    gap_reasons,
    injection_spec_document,
    judge_timeout_seconds,
    user_turns,
    write_dispositions,
)

# The old names: the compile, the document builder and the file name it wrote.
compile_episode = compile_injection_spec
episode_document = injection_spec_document
EPISODE_FILE = LEGACY_EPISODE_FILE

__all__ = [
    "BLOCK",
    "CLAIM_LEVELS",
    "COMMAND_ATTEMPT",
    "CONDITION_MEMBER",
    "EPISODE_FILE",
    "FORWARD",
    "INPUTS_MEMBER",
    "INJECTION_SPEC_FILE",
    "JUDGE_MARGIN_SECONDS",
    "JUDGE_MEMBER",
    "JUDGE_TOKEN_ENV",
    "JUDGE_URL_ENV",
    "LEGACY_EPISODE_FILE",
    "PLANTED_ITEM",
    "REPLY",
    "SEED_MEMBER",
    "TOOL",
    "TOOL_CALL_CONDITION",
    "TOOL_REVISION_RANGE",
    "WRITE_DISPOSITIONS",
    "CompileError",
    "allowed_tools",
    "claim_level",
    "compile_episode",
    "compile_injection_spec",
    "delivery_mode",
    "episode_document",
    "gap_reasons",
    "injection_spec_document",
    "judge_timeout_seconds",
    "user_turns",
    "write_dispositions",
]
