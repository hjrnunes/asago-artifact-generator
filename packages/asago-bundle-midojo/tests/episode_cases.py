"""Deprecated alias for ``injection_spec_cases``, kept for one release.

``episode_cases`` is the name this module had before the document's rename (M2): its
builders, its recorded fixture paths and its compile command are the same objects as
``injection_spec_cases``'s. It goes away at M9.
"""

from __future__ import annotations

from injection_spec_cases import (
    CASE_OPERATIONS,
    CONDITION,
    EPISODES,
    EPISODES_A5F81E9,
    FIXTURES,
    JUDGE,
    PIGGYBANK,
    PLANTED,
    READ_SLOT,
    REGRADED,
    REPO,
    SEQUENTIAL_TURNS,
    SINGLE_TURNS,
    SLOT,
    SLOTS,
    SLOTS_BY_ID,
    SPEC,
    TARGET,
    TOOLS,
    compile_case,
    compile_cases,
    planted_stimulus,
    runtime_contract,
    seed_document,
    seed_item,
    target_variant,
    write_case_package,
    write_planted_package,
)

__all__ = [
    "CASE_OPERATIONS",
    "CONDITION",
    "EPISODES",
    "EPISODES_A5F81E9",
    "FIXTURES",
    "JUDGE",
    "PIGGYBANK",
    "PLANTED",
    "READ_SLOT",
    "REGRADED",
    "REPO",
    "SEQUENTIAL_TURNS",
    "SINGLE_TURNS",
    "SLOT",
    "SLOTS",
    "SLOTS_BY_ID",
    "SPEC",
    "TARGET",
    "TOOLS",
    "compile_case",
    "compile_cases",
    "planted_stimulus",
    "runtime_contract",
    "seed_document",
    "seed_item",
    "target_variant",
    "write_case_package",
    "write_planted_package",
]
