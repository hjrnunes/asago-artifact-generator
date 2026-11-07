"""Plan trigger observations for omission scenarios.

An omission scenario (a ``not_called`` comparison) names the operation that
must stay absent; the other plan-selected operations are its triggers. These
helpers find the supplied result observations a plan must cite for them, and
name the omission plans that state no trigger at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _not_called_operations(condition: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Return the operations a discriminating condition requires to be absent."""

    if not isinstance(condition, Mapping):
        return ()
    comparisons = condition.get("comparisons")
    if not isinstance(comparisons, list):
        return ()
    names: list[str] = []
    for comparison in comparisons:
        if (
            isinstance(comparison, Mapping)
            and comparison.get("kind") == "not_called"
            and isinstance(comparison.get("operation"), str)
            and comparison["operation"] not in names
        ):
            names.append(comparison["operation"])
    return tuple(names)


def _observation_facts(inventory: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Return supplied facts that record a tool result, keyed by fact ref."""

    facts = inventory.get("facts")
    observations: dict[str, Mapping[str, Any]] = {}
    for item in facts if isinstance(facts, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("ref"), str):
            continue
        provenance = item.get("provenance")
        if (
            "value" in item
            and isinstance(provenance, Mapping)
            and isinstance(provenance.get("tool_name"), str)
        ):
            observations[item["ref"]] = item
    return observations


def _selected_operations(plan: Mapping[str, Any], inventory: Mapping[str, Any]) -> list[str]:
    """Return plan-selected operations, including those an observation ref records."""

    names: list[str] = []
    selected = plan.get("selected_evidence")
    if not isinstance(selected, list):
        return names
    observations = _observation_facts(inventory)
    for item in selected:
        if not isinstance(item, Mapping):
            continue
        name = _selected_operation_name(item, observations)
        if name is not None and name not in names:
            names.append(name)
    return names


def _selected_operation_name(
    item: Mapping[str, Any], observations: Mapping[str, Mapping[str, Any]]
) -> str | None:
    ref = item.get("ref")
    if isinstance(ref, str) and ref.startswith("operation:"):
        return ref.split(":", 1)[1]
    if isinstance(ref, str) and ref.startswith("observation:") and ref in observations:
        return observations[ref]["provenance"]["tool_name"]
    if isinstance(item.get("operation"), str):
        return item["operation"]
    return None


ESTABLISHED_TRIGGER_ROLE = "established_trigger"


def uncited_trigger_observations(
    plan: Mapping[str, Any],
    inventory: Mapping[str, Any],
    condition: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    """Map each omission trigger lacking a cited observation to its supplied ones.

    A trigger is a plan-selected operation that a ``not_called`` comparison does
    not omit. It is listed only when the inventory supplies a result observation
    for it and the plan cites or binds none of them; a trigger with no supplied
    observation is not listed.
    """

    omitted = _not_called_operations(condition)
    if not omitted:
        return {}
    observations = _observation_facts(inventory)
    cited = set(_plan_cited_fact_refs(plan))
    gaps: dict[str, list[str]] = {}
    for name in _selected_operations(plan, inventory):
        if name in omitted:
            continue
        supplied = [
            ref for ref, fact in observations.items() if fact["provenance"]["tool_name"] == name
        ]
        if supplied and not cited.intersection(supplied):
            gaps[name] = supplied
    return gaps


def _plan_cited_fact_refs(plan: Mapping[str, Any]) -> list[str]:
    """Fact refs the plan cites or binds from supplied inputs, in plan order."""

    refs: list[str] = []
    for ref in (
        *_selected_evidence_refs(plan),
        *_supplied_binding_fact_refs(plan),
        *_prerequisite_evidence_refs(plan),
    ):
        if isinstance(ref, str) and ref not in refs:
            refs.append(ref)
    return refs


def _plan_list(plan: Mapping[str, Any], key: str) -> list[Any]:
    value = plan.get(key)
    return value if isinstance(value, list) else []


def _selected_evidence_refs(plan: Mapping[str, Any]) -> list[Any]:
    return [
        item.get("ref")
        for item in _plan_list(plan, "selected_evidence")
        if isinstance(item, Mapping)
    ]


def _supplied_binding_fact_refs(plan: Mapping[str, Any]) -> list[str]:
    return [
        item["source_ref"].removeprefix("facts:")
        for item in _plan_list(plan, "runtime_bindings")
        if isinstance(item, Mapping)
        and item.get("source_kind") == "supplied_input"
        and isinstance(item.get("source_ref"), str)
        and item["source_ref"].startswith("facts:")
    ]


def _prerequisite_evidence_refs(plan: Mapping[str, Any]) -> list[Any]:
    return [
        ref
        for item in _plan_list(plan, "prerequisites")
        if isinstance(item, Mapping) and isinstance(item.get("evidence_refs"), list)
        for ref in item["evidence_refs"]
    ]


def omission_states_no_trigger(
    plan: Mapping[str, Any],
    inventory: Mapping[str, Any],
    condition: Mapping[str, Any] | None,
) -> bool:
    """Return whether an omission plan cites no trigger observation and names no trigger.

    An omission plan states its trigger by citing a supplied result observation
    of another operation (in selected_evidence, or through a supplied-input
    binding or prerequisite evidence), or by selecting that operation as
    evidence. A plan that does none of these grounds its omission in nothing:
    a ``not_called`` covers the whole conversation, so the plan would fire
    whenever the agent answers without calling the operation, flagging correct
    replies.
    """

    omitted = _not_called_operations(condition)
    if not omitted:
        return False
    observations = _observation_facts(inventory)
    for ref in _plan_cited_fact_refs(plan):
        fact = observations.get(ref)
        if fact is not None and fact["provenance"]["tool_name"] not in omitted:
            return False
    for name in _selected_operations(plan, inventory):
        if name not in omitted:
            return False
    return True


__all__ = [
    "ESTABLISHED_TRIGGER_ROLE",
    "omission_states_no_trigger",
    "uncited_trigger_observations",
]
