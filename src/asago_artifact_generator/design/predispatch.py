"""Pre-dispatch verification of a frozen plan's execution-critical prerequisites.

Finding B3: the frozen plan carries its execution-critical prerequisite
dependencies, and the pre-dispatch path verifies them against the CURRENT
live runtime state before dispatch. File digests alone do not establish that
the environment still matches — a runtime that no longer matches the recorded
prerequisites fails dispatch with a typed reason instead of executing against
a silently changed environment.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .authoring import (
    _Blocked,
    _observed_record_readings,
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


def _value_stated(value: Any, text: str) -> bool:
    """Whether the frozen stimulus text states the recorded dependency value.

    Strings compare as substrings; numbers compare as whole numeric tokens so
    the recorded amount stays attributed to the request that states it.
    """

    if isinstance(value, str):
        return bool(value) and value in text
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if str(value) in text:
            return True
        return any(float(token) == float(value) for token in re.findall(r"\d+(?:\.\d+)?", text))
    return False


def _embedded_identity(
    state: Mapping[str, Any],
    record_id: Any,
    identity_field: Any,
) -> Any:
    """Find a conflicting embedded identity for one indexed record."""

    if not isinstance(record_id, str) or not isinstance(identity_field, str):
        return None
    for collection in state.values():
        if isinstance(collection, Mapping):
            record = collection.get(record_id)
            if isinstance(record, Mapping) and identity_field in record:
                return record.get(identity_field)
        elif isinstance(collection, list):
            for record in collection:
                if (
                    isinstance(record, Mapping)
                    and record.get(identity_field) == record_id
                    and identity_field in record
                ):
                    return record.get(identity_field)
    return None


def _live_record(
    state: Mapping[str, Any],
    records: Mapping[str, Mapping[str, Any]],
    dependency: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    """Resolve one dependency's record against the live state.

    List-indexed dependencies resolve through the shared observed-record
    resolver (R3/VAL-DEP-002): mapping-valued and identity-indexed list
    collections must agree on one reading of the record identity. A duplicate
    identity within one list collection, a mapping key contradicted by the
    record's embedded identity field, or conflicting state across collections
    returns the typed ambiguity mismatch instead of guessing a reading.
    """

    record_id = dependency.get("record_id")
    identity_field = dependency.get("identity_field")
    if isinstance(identity_field, str) and identity_field:
        try:
            readings = _observed_record_readings(state, str(record_id), identity_field)
        except _Blocked as blocked:
            return None, {
                "name": str(dependency.get("name")),
                "record_id": record_id,
                "expected": record_id,
                "observed": _embedded_identity(state, record_id, identity_field),
                "reason": str(blocked),
            }
        return (readings[0] if readings else None), None
    return (records.get(str(record_id)) if record_id is not None else None), None


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
    try:
        records = _record_collections(state)
    except _Blocked as blocked:
        # R3/VAL-DEP-002: conflicting readings of one record identity in the
        # live mapping-valued collections are ambiguous and block dispatch.
        ambiguity: dict[str, Any] = {
            "check": "runtime_state_consistent",
            "reason": str(blocked),
        }
        return DispatchPrerequisiteResult(
            verified=False,
            checked=(),
            mismatches=(ambiguity,),
        )
    checked: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for dependency in plan.prerequisite_dependencies:
        checked.append(dict(dependency))
        name = str(dependency.get("name"))
        check = dependency.get("check")
        if check == "state_identity":
            field = dependency.get("field")
            expected_value = dependency.get("expected")
            live_value = state.get(field) if isinstance(field, str) else None
            if not _values_match(live_value, expected_value):
                mismatches.append(
                    {
                        "name": name,
                        "argument": dependency.get("argument"),
                        "check": check,
                        "field": field,
                        "expected": expected_value,
                        "observed": live_value,
                        "reason": (
                            f"the live runtime state has {field} = {live_value!r}, but "
                            "the plan's recorded target-context identity expects "
                            f"{expected_value!r}"
                        ),
                    }
                )
            continue
        if check == "authored_stimulus":
            expected_value = dependency.get("expected")
            delivered = "\n".join(turn.text for turn in plan.stimulus.turns)
            if not _value_stated(expected_value, delivered):
                mismatches.append(
                    {
                        "name": name,
                        "argument": dependency.get("argument"),
                        "check": check,
                        "expected": expected_value,
                        "reason": (
                            "the plan's recorded authored-argument value is not stated "
                            "by the frozen stimulus text; the design-to-artifact "
                            "continuity is broken"
                        ),
                    }
                )
            continue
        record_id = dependency.get("record_id")
        record, ambiguity = _live_record(state, records, dependency)
        if ambiguity is not None:
            mismatches.append(ambiguity)
            continue
        if check == "record_present":
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
            else:
                argument = dependency.get("argument")
                if isinstance(argument, str) and argument:
                    embedded = record.get(argument)
                    if isinstance(embedded, str) and embedded and embedded != str(record_id):
                        mismatches.append(
                            {
                                "name": name,
                                "record_id": record_id,
                                "argument": argument,
                                "expected": record_id,
                                "observed": embedded,
                                "reason": (
                                    f"record {record_id!r} is indexed under a live "
                                    f"collection key that its embedded {argument!r} "
                                    f"identity {embedded!r} contradicts; the record "
                                    "identity is ambiguous"
                                ),
                            }
                        )
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
            continue
        session_field = dependency.get("session_field")
        if isinstance(session_field, str) and session_field:
            live_session = state.get(session_field)
            if not _values_match(live_session, expected_value):
                mismatches.append(
                    {
                        "name": name,
                        "record_id": record_id,
                        "field": session_field,
                        "expected": expected_value,
                        "observed": live_session,
                        "reason": (
                            f"the live runtime session has {session_field} = "
                            f"{live_session!r}, but the plan's recorded patient "
                            f"association expects {expected_value!r}"
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
