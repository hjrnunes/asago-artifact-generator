"""Pre-dispatch verification of a frozen plan's execution-critical prerequisites.

Finding B3: the frozen plan carries its execution-critical prerequisite
dependencies, and the pre-dispatch path verifies them against the CURRENT
live runtime state before dispatch. File digests alone do not establish that
the environment still matches — a runtime that no longer matches the recorded
prerequisites fails dispatch with a typed reason instead of executing against
a silently changed environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .authoring import (
    _Blocked,
    _precondition_records,
    _record_collections,
    _record_party_values,
    _session_identity,
)
from .records import ArtifactDesignPlan


class PrerequisiteMismatchError(Exception):
    """Typed dispatch failure: the live runtime no longer matches the plan's
    recorded execution-critical prerequisites. Carries the typed mismatch
    records; dispatch is blocked, never silently degraded."""

    def __init__(self, mismatches: tuple[Mapping[str, Any], ...]) -> None:
        self.mismatches = mismatches
        reasons = "; ".join(str(mismatch.get("reason")) for mismatch in mismatches)
        super().__init__(f"prerequisite-runtime-mismatch: {reasons}")


@dataclass(frozen=True, slots=True)
class DispatchPrerequisiteResult:
    """The typed outcome of one pre-dispatch prerequisite verification."""

    verified: bool
    checked: tuple[Mapping[str, Any], ...]
    mismatches: tuple[Mapping[str, Any], ...]


def _values_match(live: Any, expected: Any) -> bool:
    """Type-aware equality: booleans never match numbers, numbers compare
    numerically, everything else compares directly."""

    if isinstance(expected, bool) or isinstance(live, bool):
        return isinstance(expected, bool) and isinstance(live, bool) and live is expected
    if isinstance(expected, (int, float)) and isinstance(live, (int, float)):
        return float(live) == float(expected)
    return live == expected


def _live_record(
    state: Mapping[str, Any],
    records: Mapping[str, Mapping[str, Any]],
    dependency: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Resolve one dependency's record against the live state.

    The lookup unifies the design path's two indexings (second consumer
    correction, 2026-09-16): mapping-valued collections through
    ``_record_collections`` (the record id is the collection key), and — when
    the dependency carries the identity field its record was indexed by —
    list-valued collections through ``_precondition_records``. Strictly
    fail-closed: an ambiguous identity (conflicting state across collections)
    returns the typed mismatch instead of guessing a reading.
    """

    record_id = dependency.get("record_id")
    identity_field = dependency.get("identity_field")
    record = records.get(str(record_id)) if record_id is not None else None
    if not (isinstance(identity_field, str) and identity_field):
        return record, None
    try:
        list_records = _precondition_records(state, identity_field)
    except _Blocked as blocked:
        return None, {
            "name": str(dependency.get("name")),
            "record_id": record_id,
            "reason": str(blocked),
        }
    list_record = list_records.get(str(record_id)) if record_id is not None else None
    if list_record is None:
        return record, None
    if record is not None and record != list_record:
        return None, {
            "name": str(dependency.get("name")),
            "record_id": record_id,
            "reason": (
                f"record {record_id!r} is observed with conflicting state across the "
                "live runtime collections; the record identity is ambiguous"
            ),
        }
    return list_record, None


def verify_dispatch_prerequisites(
    plan: ArtifactDesignPlan,
    runtime_context: Mapping[str, Any],
) -> DispatchPrerequisiteResult:
    """Verify the plan's recorded execution-critical prerequisite dependencies
    against the CURRENT live runtime state.

    Returns the typed verification record; ``require_dispatch_prerequisites``
    raises the typed dispatch-blocking error for a mismatched runtime.
    """

    state = runtime_context.get("state") if isinstance(runtime_context, Mapping) else None
    if not isinstance(state, Mapping):
        mismatch: dict[str, Any] = {
            "check": "runtime_state_present",
            "reason": "the live runtime context carries no state observation",
        }
        return DispatchPrerequisiteResult(verified=False, checked=(), mismatches=(mismatch,))
    records = _record_collections(state)
    checked: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for dependency in plan.prerequisite_dependencies:
        checked.append(dict(dependency))
        name = str(dependency.get("name"))
        record_id = dependency.get("record_id")
        record, ambiguity = _live_record(state, records, dependency)
        if ambiguity is not None:
            mismatches.append(ambiguity)
            continue
        if dependency.get("check") == "session_not_owner":
            session, _session_key = _session_identity(state)
            if session is None:
                mismatches.append(
                    {
                        "name": name,
                        "record_id": record_id,
                        "reason": (
                            "the live runtime no longer establishes the authenticated "
                            "session identity the plan's recorded ownership prerequisite "
                            "was verified against"
                        ),
                    }
                )
            elif record is None:
                mismatches.append(
                    {
                        "name": name,
                        "record_id": record_id,
                        "reason": (
                            f"record {record_id!r} is no longer present in the live runtime state"
                        ),
                    }
                )
            else:
                party_values = _record_party_values(record)
                if not party_values or session in party_values:
                    mismatches.append(
                        {
                            "name": name,
                            "record_id": record_id,
                            "expected": dependency.get("expected"),
                            "reason": (
                                f"record {record_id!r} is no longer established outside "
                                "the authenticated session in the live runtime state"
                            ),
                        }
                    )
            continue
        if record is None:
            mismatches.append(
                {
                    "name": name,
                    "record_id": record_id,
                    "reason": (
                        f"record {record_id!r} is no longer present in the live runtime state"
                    ),
                }
            )
            continue
        field = dependency.get("field")
        live_value = record.get(field) if isinstance(field, str) else None
        expected_value = dependency.get("expected")
        if not _values_match(live_value, expected_value):
            mismatches.append(
                {
                    "name": name,
                    "record_id": record_id,
                    "field": field,
                    "expected": expected_value,
                    "observed": live_value,
                    "reason": (
                        f"the live runtime state has {record_id!r}.{field} = "
                        f"{live_value!r}, but the plan's recorded prerequisite "
                        f"expects {expected_value!r}"
                    ),
                }
            )
    return DispatchPrerequisiteResult(
        verified=not mismatches,
        checked=tuple(checked),
        mismatches=tuple(mismatches),
    )


def require_dispatch_prerequisites(
    plan: ArtifactDesignPlan,
    runtime_context: Mapping[str, Any],
) -> DispatchPrerequisiteResult:
    """The pre-dispatch gate: verify the plan's execution-critical
    prerequisites against the live runtime and block dispatch with a typed
    reason when the runtime no longer matches."""

    result = verify_dispatch_prerequisites(plan, runtime_context)
    if not result.verified:
        raise PrerequisiteMismatchError(result.mismatches)
    return result


__all__ = [
    "DispatchPrerequisiteResult",
    "PrerequisiteMismatchError",
    "require_dispatch_prerequisites",
    "verify_dispatch_prerequisites",
]
