"""Supplied detector control cases: validation, origin marks, and deduplication."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..detector_controls import ControlCase
from .core import _canonical_json

# Caller-supplied extra detector-control cases: either a static sequence of
# ControlCase objects or a provider callable that receives the current
# candidate plan and metadata and returns the extra cases for that candidate.
SuppliedControlCases = (
    Sequence[ControlCase] | Callable[[Mapping[str, Any], Mapping[str, Any]], Sequence[ControlCase]]
)


def _validate_supplied_control_cases(
    supplied_control_cases: SuppliedControlCases,
) -> SuppliedControlCases:
    """Validate the static supplied-control form eagerly; providers self-report."""

    if callable(supplied_control_cases):
        return supplied_control_cases
    if isinstance(supplied_control_cases, (str, bytes)) or not isinstance(
        supplied_control_cases, Sequence
    ):
        raise ValueError(
            "supplied_control_cases must be ControlCase instances or a callable that returns them"
        )
    for case in supplied_control_cases:
        if not isinstance(case, ControlCase):
            raise ValueError("supplied_control_cases must contain only ControlCase instances")
    return supplied_control_cases


def _mark_control_origins(
    results: list[dict[str, Any]],
    normal_count: int,
) -> None:
    """Label each combined control result with its mechanical or supplied origin."""

    for index, record in enumerate(results):
        record["origin"] = "normal" if index < normal_count else "supplied"


def _deduplicate_control_cases(
    normal_cases: Sequence[ControlCase],
    supplied_cases: Sequence[ControlCase],
) -> tuple[tuple[ControlCase, ...], dict[str, Any]]:
    """Drop supplied cases that exactly duplicate generated expectations."""

    generated = tuple(normal_cases)
    supplied = tuple(supplied_cases)
    selected = list(generated)
    duplicates_dropped: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
    generated_by_evidence: dict[str, list[ControlCase]] = {}
    for case in generated:
        evidence_key = _canonical_json(case.evidence)
        generated_by_evidence.setdefault(evidence_key, []).append(case)

    for supplied_case in supplied:
        evidence_key = _canonical_json(supplied_case.evidence)
        matching_generated = generated_by_evidence.get(evidence_key, [])
        exact_matches = [
            case
            for case in matching_generated
            if (
                case.expected_outcome == supplied_case.expected_outcome
                and case.expected_claim_level == supplied_case.expected_claim_level
            )
        ]
        if exact_matches:
            duplicates_dropped.append(
                {
                    "supplied_name": supplied_case.name,
                    "generated_name": exact_matches[0].name,
                }
            )
            continue
        for generated_case in matching_generated:
            conflicts.append(
                {
                    "supplied_name": supplied_case.name,
                    "generated_name": generated_case.name,
                    "reason": "same_evidence_different_expectation",
                }
            )
        selected.append(supplied_case)

    return tuple(selected), {
        "generated_count": len(generated),
        "supplied_count": len(supplied),
        "executed_count": len(selected),
        "duplicates_dropped": duplicates_dropped,
        "conflicts": conflicts,
    }
