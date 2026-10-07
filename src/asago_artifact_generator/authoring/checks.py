"""Closed structural checks for plan and artifact responses.

This module collects the deterministic findings that run before semantic
review.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

from ..bindings import (
    BINDING_SPEC,
    CLOSED_TYPES,
    JUDGE_CONSUMERS,
    MISSING_POLICIES,
    SOURCE_KINDS,
    BindingValidationError,
    _binding_selector_type,
    _binding_types_compatible,
    canonical_binding_paths,
    find_stimulus_user_text_consumer_mismatches,
    normalize_binding_declarations,
    supplied_binding_values,
    validate_bindings,
)
from ..contract_kit import CLAIM_LEVELS, ClaimLevel
from .contracts import _SEMANTIC_JUDGE_SPEC_RULES, _call1_contract_v2
from .core import (
    _SLOT_RE,
    Finding,
    PlanValidationError,
    _findings_from_error,
    _is_json_value,
    _json_value_type,
    _matches_schema_type,
    _supported_claim_levels,
    staged_findings,
)
from .example_capture import CAPTURED_EXAMPLES, capture_shape_findings, example_capture_findings
from .inventory import _first_fact_named, _inventory_fact_map, _inventory_references
from .oracle_self_test import oracle_self_test_findings
from .placeholder import artifact_placeholder_findings, plan_placeholder_findings
from .plan_triggers import ESTABLISHED_TRIGGER_ROLE, uncited_trigger_observations

# The spelling JUDGE_CONSUMERS replaced. It stays out of the closed vocabulary, so a plan
# that still writes it gets a finding that names the replacement instead of an alias.
_RETIRED_DETECTOR_PREFIX = "detector."


def _validate_call2_metadata_shape(value: Any) -> list[Finding]:
    if not isinstance(value, dict):
        return [Finding("metadata_type_error", "Call 2 response must be a JSON object", "call2")]
    findings = _call2_root_field_findings(value)
    if "stimulus" in value:
        findings.extend(_call2_stimulus_findings(value["stimulus"]))
    findings.extend(_call2_semantic_judge_spec_findings(value.get("semantic_judge_spec")))
    if "examples" in value:
        findings.extend(_call2_examples_findings(value["examples"]))
    if "explanation" in value and not isinstance(value["explanation"], str):
        findings.append(Finding("type_error", "explanation must be a string", "explanation"))
    return findings


_CALL2_METADATA_FIELDS = frozenset({"stimulus", "semantic_judge_spec", "examples", "explanation"})
_CALL2_PLAN_OWNED_FIELDS = frozenset(
    {
        "interpretation",
        "selected_evidence",
        "assumptions",
        "setup_recipe",
        "runtime_bindings",
        "prerequisites",
        "observation_claim",
        "required_observations",
        "semantic_judge",
        "unresolved_requirements",
        "setup",
        "bindings",
        "evidence",
        "observation",
        "observation_requirements",
        "claim_level",
        "judge",
    }
)


def _call2_root_field_findings(value: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for field_name in sorted(set(value) - _CALL2_METADATA_FIELDS):
        findings.append(
            Finding(
                "plan_conflict" if field_name in _CALL2_PLAN_OWNED_FIELDS else "unexpected_field",
                (
                    f"Call 2 cannot resubmit plan-owned field: {field_name}"
                    if field_name in _CALL2_PLAN_OWNED_FIELDS
                    else f"unexpected Call 2 metadata field: {field_name}"
                ),
                f"call2.{field_name}",
            )
        )
    for field_name in sorted(_CALL2_METADATA_FIELDS - set(value)):
        findings.append(
            Finding(
                "missing_field",
                f"Call 2 metadata missing field: {field_name}",
                f"call2.{field_name}",
            )
        )
    return findings


def _call2_stimulus_findings(stimulus: Any) -> list[Finding]:
    if not isinstance(stimulus, dict):
        return [Finding("type_error", "stimulus must be an object", "stimulus")]
    required = {"user_text", "history", "slots", "delivery"}
    findings = [
        Finding("missing_field", f"stimulus missing field: {name}", f"stimulus.{name}")
        for name in sorted(required - set(stimulus))
    ]
    findings.extend(
        Finding("unexpected_field", f"unexpected stimulus field: {name}", f"stimulus.{name}")
        for name in sorted(set(stimulus) - required)
    )
    for name, valid, noun in (
        ("user_text", isinstance(stimulus.get("user_text"), str), "a string"),
        ("delivery", isinstance(stimulus.get("delivery"), str), "a string"),
        ("history", isinstance(stimulus.get("history"), list), "a list"),
        ("slots", _is_string_list(stimulus.get("slots")), "a list of strings"),
    ):
        if not valid:
            findings.append(
                Finding("type_error", f"stimulus.{name} must be {noun}", f"stimulus.{name}")
            )
    return findings


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _call2_semantic_judge_spec_findings(spec: Any) -> list[Finding]:
    if spec is None:
        return []
    if not isinstance(spec, dict):
        return [
            Finding(
                "type_error",
                "semantic_judge_spec must be an object or null",
                "semantic_judge_spec",
            )
        ]
    fields = ("question", "criteria", "fact_refs")
    findings = [
        Finding(
            "unexpected_field",
            f"unexpected semantic_judge_spec field: {name}",
            f"semantic_judge_spec.{name}",
        )
        for name in sorted(set(spec) - set(fields))
    ]
    findings.extend(
        Finding(
            "missing_field",
            f"semantic_judge_spec missing field: {name}",
            f"semantic_judge_spec.{name}",
        )
        for name in fields
        if name not in spec
    )
    for name, valid, noun in (
        ("question", isinstance(spec.get("question"), str), "a string"),
        ("criteria", isinstance(spec.get("criteria"), str), "a string"),
        ("fact_refs", _is_string_list(spec.get("fact_refs")), "a list of strings"),
    ):
        if name in spec and not valid:
            findings.append(
                Finding(
                    "type_error",
                    f"semantic_judge_spec.{name} must be {noun}",
                    f"semantic_judge_spec.{name}",
                )
            )
    return findings


def _call2_examples_findings(examples: Any) -> list[Finding]:
    if not isinstance(examples, dict):
        return [Finding("type_error", "examples must be an object", "examples")]
    findings: list[Finding] = []
    for label in sorted(set(examples) - {"unsafe", "safe", "inconclusive"}):
        findings.append(
            Finding(
                "unexpected_field",
                f"unexpected examples field: {label}",
                f"examples.{label}",
            )
        )
    for label in ("unsafe", "safe", "inconclusive"):
        findings.extend(_call2_example_findings(label, examples.get(label)))
    return findings


def _call2_example_findings(label: str, item: Any) -> list[Finding]:
    findings = _example_item_findings(label, item)
    if findings or "capture" not in item:
        return findings
    return capture_shape_findings(label, item["capture"])


def _example_item_findings(label: str, item: Any) -> list[Finding]:
    if item is None:
        return [
            Finding(
                "missing_field",
                f"examples missing field: {label}",
                f"examples.{label}",
            )
        ]
    if not isinstance(item, dict) or item.get("label") != "author-proposed":
        return [
            Finding(
                "example_shape",
                f"example {label} must be author-proposed",
                f"examples.{label}",
            )
        ]
    allowed = {"label", "description"} | ({"capture"} if label in CAPTURED_EXAMPLES else set())
    extra = sorted(set(item) - allowed)
    if extra:
        return [
            Finding(
                "unexpected_field",
                f"unexpected example field: {field_name}",
                f"examples.{label}.{field_name}",
            )
            for field_name in extra
        ]
    if not isinstance(item.get("description"), str):
        return [
            Finding(
                "type_error",
                f"example {label} description must be a string",
                f"examples.{label}.description",
            )
        ]
    return []


def collect_plan_findings_v2(
    plan: Any,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    provenance_ids: Collection[str] = frozenset(),
    condition: Mapping[str, Any] | None = None,
    transformations: list[dict[str, Any]] | None = None,
) -> list[Finding]:
    """Return every structural finding for a v2 plan and normalize valid bindings.

    The v2 root fields (``assumptions`` and ``required_observations``) are
    checked here; the remaining fields go through ``collect_plan_findings``,
    the shared field validator, which skips prerequisite contents.
    Prerequisites are then checked once, against the closed v2 form,
    followed by the omission-trigger, stimulus-slot, and established-trigger
    cross-checks.

    ``provenance_ids`` are scenario lineage or attack-tree node IDs from the
    supplied handoff. They are valid in ``interpretation.source_refs`` and
    ``assumptions[].ref``; every other reference field stays limited to
    supplied inventory references. ``condition`` is the handoff's
    discriminating condition; its not_called comparisons add the omission
    trigger check.
    """

    if not isinstance(plan, dict):
        return [Finding("response_type_error", "plan must be an object", "response", stage="plan")]
    findings = _v2_root_field_findings(plan, _call1_contract_v2()["schema"]["required"])
    findings.extend(_plan_assumptions_findings(plan, inventory, provenance_ids))
    if not isinstance(plan.get("required_observations"), dict) and (
        "required_observations" in plan
    ):
        findings.append(
            Finding(
                "type_error",
                "required_observations must be an object",
                "required_observations",
            )
        )
    shared_plan = dict(plan)
    shared_plan.pop("assumptions", None)
    shared_plan.pop("required_observations", None)
    findings.extend(
        _normalized_plan_findings(
            shared_plan,
            inventory,
            runtime_contract,
            provenance_ids,
            transformations,
            report_root_presence=False,
        )
    )
    findings.extend(_unobtainable_requirement_findings(plan))
    findings.extend(_plan_canonical_prerequisite_findings(plan, inventory))
    findings.extend(_omission_trigger_findings(plan, inventory, condition))
    findings.extend(_plan_stimulus_slot_findings(plan, inventory))
    findings.extend(_established_trigger_findings(plan, inventory))
    return staged_findings(findings, "plan")


def _established_trigger_findings(
    plan: dict[str, Any], inventory: dict[str, Any]
) -> list[Finding]:
    """Require each established_trigger item to cite a supplied result observation."""

    selected = plan.get("selected_evidence")
    observations = _supplied_result_observations(inventory)
    findings: list[Finding] = []
    for index, item in enumerate(selected if isinstance(selected, list) else []):
        if not isinstance(item, dict) or item.get("role") != ESTABLISHED_TRIGGER_ROLE:
            continue
        ref = item.get("ref")
        if isinstance(ref, str) and ref in observations:
            continue
        findings.append(_established_trigger_finding(index, ref, observations))
    return findings


def _supplied_result_observations(inventory: dict[str, Any]) -> dict[str, str]:
    """Map each supplied fact that records a tool result to that tool's name."""

    facts = inventory.get("facts")
    return {
        item["ref"]: item["provenance"]["tool_name"]
        for item in (facts if isinstance(facts, list) else [])
        if isinstance(item, dict)
        and isinstance(item.get("ref"), str)
        and "value" in item
        and isinstance(item.get("provenance"), dict)
        and isinstance(item["provenance"].get("tool_name"), str)
    }


def _established_trigger_finding(index: int, ref: Any, observations: dict[str, str]) -> Finding:
    """Report an established trigger whose ref is not a supplied result observation."""

    operation = (
        ref.removeprefix("operation:")
        if isinstance(ref, str) and ref.startswith("operation:")
        else None
    )
    candidates = [
        name for name, tool in observations.items() if operation is None or tool == operation
    ]
    options = (
        f"; supplied observations{' of ' + repr(operation) if operation else ''}: "
        f"{', '.join(candidates)}"
        if candidates
        else "; the inventory supplies no such observation, so use role trigger"
    )
    return Finding(
        "established_trigger_not_observation",
        (
            f"selected_evidence[{index}] has role {ESTABLISHED_TRIGGER_ROLE!r} but its "
            f"ref {ref!r} is not a supplied result observation. An established "
            "trigger cites the observation that already shows the triggering result "
            f"before the run{options}"
        ),
        f"selected_evidence[{index}].role",
    )


def _plan_stimulus_slot_findings(
    plan: dict[str, Any],
    inventory: dict[str, Any],
) -> list[Finding]:
    """Cross-check request slots and user-text consumers while the plan can still change.

    The artifact stage applies the same user-text consumer rule to the authored
    stimulus, where the accepted plan is frozen and the binding cannot be
    repaired. A request slot whose binding lists other consumers stays valid:
    downstream fills slots from any declared binding.
    """

    approach = plan.get("stimulus_approach")
    bindings = plan.get("runtime_bindings")
    if not isinstance(approach, dict) or not isinstance(bindings, list):
        return []
    request = approach.get("request")
    if not isinstance(request, str):
        return []
    declared = _declared_binding_names(bindings)
    findings = [
        Finding(
            "undeclared_slot",
            (
                f"stimulus_approach.request uses {{{{{slot}}}}}, but no runtime binding is "
                f"named {slot!r}; declared binding names: "
                f"{', '.join(sorted(declared)) or '(none)'}. Declare the binding or "
                "remove the slot."
            ),
            f"stimulus_approach.request:{slot}",
        )
        for slot in _slot_names_in_order(request)
        if slot not in declared
    ]
    values = supplied_binding_values(bindings, inventory)
    for mismatch in find_stimulus_user_text_consumer_mismatches(
        bindings, request, resolved_values=values
    ):
        name = mismatch["binding_name"]
        findings.append(
            Finding(
                "consumer_mismatch",
                (
                    f"binding {name!r} declares stimulus.user_text, but "
                    "stimulus_approach.request contains neither its {{" + name + "}} slot "
                    "nor its supplied value; the artifact stage rejects that consumer "
                    "and cannot change the plan. Either write {{" + name + "}} where the "
                    "request uses the value, or remove stimulus.user_text from its "
                    "consumers. A slot must name the binding whose value it carries; "
                    "session prerequisites and judge-only values do not belong in "
                    "stimulus.user_text."
                ),
                f"runtime_bindings[{mismatch['binding_index']}]"
                f".consumers[{mismatch['consumer_index']}]",
            )
        )
    return findings


def _omission_trigger_findings(
    plan: dict[str, Any],
    inventory: dict[str, Any],
    condition: Mapping[str, Any] | None,
) -> list[Finding]:
    """Require an omission plan to cite the supplied result of each trigger it names.

    The cited observation is the evidence that the trigger fired; without it a
    missing call cannot be attributed to the omission.
    """

    return [
        Finding(
            "omission_trigger_observation_uncited",
            (
                f"the plan names {name!r} as the trigger of a not_called omission, "
                f"and the inventory supplies its result as {', '.join(refs)}, but "
                "the plan cites none of them; cite the observation that records the "
                "triggering result in selected_evidence (or bind it from supplied "
                "input), not only the operation ref"
            ),
            "selected_evidence",
        )
        for name, refs in uncited_trigger_observations(plan, inventory, condition).items()
    ]


def collect_artifact_findings_v2(
    metadata: dict[str, Any],
    plan: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    transformations: list[dict[str, Any]] | None = None,
    condition: Mapping[str, Any] | None = None,
) -> list[Finding]:
    """Normalize the plan-owned context and the stimulus slots, then validate v2 metadata.

    ``condition`` is the handoff's bound tool-call condition. A command-attempt
    artifact whose example captures pass the structural checks is also scored
    by it: its unsafe capture must be detected and its safe capture must not be.
    """

    findings = _validate_call2_metadata_shape(metadata)
    if findings or not isinstance(metadata, dict):
        return staged_findings(findings, "artifact")
    _normalize_artifact_context(metadata, plan, inventory, transformations=transformations)
    findings.extend(_artifact_findings(metadata, plan, inventory, runtime_contract, condition))
    return staged_findings(findings, "artifact")


def _normalize_artifact_context(
    metadata: dict[str, Any],
    plan: dict[str, Any],
    inventory: dict[str, Any],
    *,
    transformations: list[dict[str, Any]] | None,
) -> None:
    """Canonicalize the plan bindings and their consumers, then derive the stimulus slots."""

    runtime_bindings = plan.get("runtime_bindings")
    if isinstance(runtime_bindings, list):
        normalize_binding_declarations(
            runtime_bindings,
            inventory=inventory,
            transformations=transformations,
        )
        _normalize_prerequisite_binding_consumers(
            plan.get("prerequisites"),
            runtime_bindings,
            transformations=transformations,
        )
    stimulus = metadata.get("stimulus")
    if isinstance(stimulus, dict):
        _normalize_stimulus_slots(
            stimulus,
            _declared_binding_names(runtime_bindings),
            transformations=transformations,
        )


def _artifact_findings(
    metadata: dict[str, Any],
    plan: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    condition: Mapping[str, Any] | None,
) -> list[Finding]:
    findings = _semantic_judge_question_findings(metadata.get("semantic_judge_spec"))
    runtime_bindings = plan.get("runtime_bindings")
    stimulus = metadata.get("stimulus")
    if isinstance(stimulus, dict):
        findings.extend(
            _artifact_stimulus_findings(
                stimulus,
                plan,
                inventory,
                runtime_contract,
                runtime_bindings,
            )
        )
    findings.extend(_artifact_example_findings(metadata, plan, inventory, condition))
    judge_spec = metadata.get("semantic_judge_spec")
    findings.extend(_semantic_judge_decision_findings(plan, judge_spec))
    if isinstance(judge_spec, dict):
        findings.extend(_semantic_judge_fact_ref_findings(judge_spec, inventory))
    if isinstance(plan, dict) and isinstance(plan.get("prerequisites"), list):
        declared_bindings = _declared_binding_names(plan.get("runtime_bindings"))
        findings.extend(
            _canonical_prerequisite_findings(
                plan["prerequisites"],
                _inventory_references(inventory),
                declared_bindings,
                runtime_bindings,
                safe_behavior=_plan_safe_behavior(plan),
            )
        )
    return findings


def _artifact_example_findings(
    metadata: dict[str, Any],
    plan: dict[str, Any],
    inventory: dict[str, Any],
    condition: Mapping[str, Any] | None,
) -> list[Finding]:
    """Check the example captures against the claim level, the inventory, and the condition."""

    level = _plan_claim_level(plan)
    if level not in (ClaimLevel.COMMAND_ATTEMPT, ClaimLevel.REPLY):
        return []
    findings = example_capture_findings(
        metadata["examples"],
        command_attempt=level == ClaimLevel.COMMAND_ATTEMPT,
        inventory=inventory,
    )
    if findings or level != ClaimLevel.COMMAND_ATTEMPT:
        return findings
    return oracle_self_test_findings(metadata["examples"], condition)


def _semantic_judge_question_findings(spec: Any) -> list[Finding]:
    if (
        isinstance(spec, dict)
        and isinstance(spec.get("question"), str)
        and not spec["question"].rstrip().endswith("?")
    ):
        return [
            Finding(
                "semantic_judge_question_form",
                (
                    "semantic_judge_spec.question must be one yes/no question ending with "
                    "a question mark, not an instruction. "
                    + _SEMANTIC_JUDGE_SPEC_RULES["question"]
                ),
                "semantic_judge_spec.question",
            )
        ]
    return []


def _artifact_stimulus_findings(
    stimulus: dict[str, Any],
    plan: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    runtime_bindings: Any,
) -> list[Finding]:
    findings = _stimulus_delivery_findings(stimulus.get("delivery"), plan, runtime_contract)
    findings.extend(_stimulus_history_findings(stimulus.get("history")))
    user_text = stimulus.get("user_text")
    findings.extend(artifact_placeholder_findings(stimulus))
    findings.extend(_stimulus_slot_findings(stimulus, runtime_bindings))
    if isinstance(plan, dict) and isinstance(plan.get("runtime_bindings"), list):
        findings.extend(
            _stimulus_consumer_findings(
                runtime_bindings,
                inventory,
                user_text if isinstance(user_text, str) else "",
            )
        )
    return findings


def _stimulus_delivery_findings(
    delivery: Any,
    plan: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    if delivery != plan.get("stimulus_approach", {}).get("delivery"):
        findings.append(
            Finding(
                "plan_conflict",
                "stimulus delivery differs from accepted plan",
                "stimulus.delivery",
            )
        )
    if delivery not in runtime_contract.get("delivery", []):
        findings.append(
            Finding(
                "closed_value_error",
                f"undocumented delivery capability: {delivery}",
                "stimulus.delivery",
            )
        )
    return findings


def _stimulus_history_findings(history: Any) -> list[Finding]:
    if not isinstance(history, list):
        return []
    return [
        Finding(
            "non_user_history",
            "stimulus history may contain user messages only",
            f"stimulus.history[{index}]",
        )
        for index, item in enumerate(history)
        if (
            not isinstance(item, dict)
            or item.get("role") != "user"
            or not isinstance(item.get("content"), str)
            or set(item) - {"role", "content"}
        )
    ]


def _stimulus_slot_findings(stimulus: dict[str, Any], runtime_bindings: Any) -> list[Finding]:
    """Check slots that ``_normalize_stimulus_slots`` already derived where it could."""

    slots = stimulus.get("slots")
    user_text = stimulus.get("user_text")
    if not (isinstance(slots, list) and isinstance(user_text, str)):
        return []
    findings: list[Finding] = []
    rendered_slots = _slot_names_in_order(user_text)
    declared = _declared_binding_names(runtime_bindings)
    undeclared = [slot for slot in rendered_slots if slot not in declared]
    if slots != rendered_slots:
        findings.append(
            Finding("slot_mismatch", "stimulus slots do not match user_text", "stimulus.slots")
        )
    for slot in rendered_slots:
        if slot not in declared:
            findings.append(
                Finding(
                    "undeclared_slot",
                    (
                        f"stimulus contains undeclared binding placeholder(s): "
                        f"{', '.join(sorted(set(undeclared)))}; declared binding "
                        f"names: {', '.join(sorted(declared)) or '(none)'}"
                    ),
                    f"stimulus.user_text:{slot}",
                )
            )
    return findings


def _stimulus_consumer_findings(
    runtime_bindings: list[Any],
    inventory: dict[str, Any],
    user_text: str,
) -> list[Finding]:
    findings: list[Finding] = []
    values = supplied_binding_values(runtime_bindings, inventory)
    for mismatch in find_stimulus_user_text_consumer_mismatches(
        runtime_bindings,
        user_text,
        resolved_values=values,
    ):
        binding_index = mismatch["binding_index"]
        consumer_index = mismatch["consumer_index"]
        binding_name = mismatch["binding_name"]
        if mismatch["value_available"]:
            detail = (
                f"binding {binding_name!r} declares stimulus.user_text, but its "
                "supplied value does not occur in authored stimulus.user_text; "
                f"remove that consumer or use {{{{{binding_name}}}}} in the "
                "user text. Session prerequisites and judge inputs do not "
                "belong in stimulus.user_text."
            )
        else:
            detail = (
                f"binding {binding_name!r} declares stimulus.user_text, but its "
                "resolved value is not present in authored stimulus.user_text; "
                f"remove that consumer or use {{{{{binding_name}}}}} in the "
                "user text. A setup-derived value must use a declared slot."
            )
        findings.append(
            Finding(
                "consumer_mismatch",
                detail,
                f"runtime_bindings[{binding_index}].consumers[{consumer_index}]",
            )
        )
    return findings


def _semantic_judge_decision_findings(plan: dict[str, Any], judge_spec: Any) -> list[Finding]:
    if judge_spec is None and _plan_claim_level(plan) == ClaimLevel.REPLY:
        return [
            Finding(
                "semantic_judge_spec_required",
                "the accepted plan claims reply, and a reply package is scored only by "
                "the semantic judge; return a semantic_judge_spec object with question, "
                "criteria, and fact_refs",
                "semantic_judge_spec",
            )
        ]
    needed = (
        plan.get("semantic_judge", {}).get("needed")
        if isinstance(plan.get("semantic_judge"), dict)
        else None
    )
    if isinstance(needed, bool) and needed != (judge_spec is not None):
        return [
            Finding(
                "plan_conflict",
                "semantic judge specification differs from accepted plan decision",
                "semantic_judge_spec",
            )
        ]
    return []


def _semantic_judge_fact_ref_findings(
    judge_spec: dict[str, Any],
    inventory: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    refs = judge_spec.get("fact_refs")
    facts = _inventory_fact_map(inventory)
    if not isinstance(refs, list):
        return findings
    for index, ref in enumerate(refs):
        path = f"semantic_judge_spec.fact_refs[{index}]"
        if not isinstance(ref, str) or ref not in facts:
            findings.append(
                Finding(
                    "unknown_reference",
                    f"unknown_reference: {ref}",
                    path,
                )
            )
        elif "value" not in facts[ref]:
            findings.append(
                Finding(
                    "unresolved_fact",
                    f"static fact has no supplied value: {ref}",
                    path,
                )
            )
    return findings


def _v2_root_field_findings(plan: dict[str, Any], required: list[str]) -> list[Finding]:
    """Report root fields outside the contract, then required root fields that are absent."""

    findings = [
        Finding("unexpected_field", f"unexpected plan field: {field_name}", field_name)
        for field_name in sorted(set(plan) - set(required))
    ]
    findings.extend(
        Finding("plan_validation", f"missing plan field: {field_name}", field_name)
        for field_name in required
        if field_name not in plan
    )
    return findings


def _plan_assumptions_findings(
    plan: dict[str, Any],
    inventory: dict[str, Any],
    provenance_ids: Collection[str],
) -> list[Finding]:
    """Validate the v2 assumptions list and each assumption's shape and reference."""

    assumptions = plan.get("assumptions")
    if not isinstance(assumptions, list):
        if "assumptions" in plan:
            return [Finding("type_error", "assumptions must be a list", "assumptions")]
        return []
    references = _inventory_references(inventory)
    findings: list[Finding] = []
    for index, assumption in enumerate(assumptions):
        findings.extend(
            _assumption_findings(f"assumptions[{index}]", assumption, references, provenance_ids)
        )
    return findings


def _assumption_findings(
    path: str,
    assumption: Any,
    references: Collection[str],
    provenance_ids: Collection[str],
) -> list[Finding]:
    if not isinstance(assumption, dict):
        return [Finding("shape_error", "assumption must be an object", path)]
    findings = [
        Finding(
            "unexpected_field",
            f"unexpected assumption field: {field_name}",
            f"{path}.{field_name}",
        )
        for field_name in sorted(set(assumption) - {"ref", "reason"})
    ]
    if not isinstance(assumption.get("ref"), str):
        findings.append(Finding("type_error", "assumption.ref must be a string", f"{path}.ref"))
    elif assumption["ref"] not in references and assumption["ref"] not in provenance_ids:
        findings.append(
            Finding(
                "unknown_reference",
                f"unknown_reference: {assumption['ref']}",
                f"{path}.ref",
            )
        )
    if not isinstance(assumption.get("reason"), str):
        findings.append(
            Finding("type_error", "assumption.reason must be a string", f"{path}.reason")
        )
    return findings


def _unobtainable_requirement_findings(plan: dict[str, Any]) -> list[Finding]:
    """Report essential setup-output requirements that the plan marks as unobtainable."""

    unresolved = plan.get("unresolved_requirements")
    if not isinstance(unresolved, list):
        return []
    return [
        Finding(
            "unobtainable_essential_requirement",
            (
                "an essential requirement that cannot be obtained blocks "
                "the plan; a requirement that is not needed for the "
                "experiment is not essential"
            ),
            f"unresolved_requirements[{index}]",
        )
        for index, item in enumerate(unresolved)
        if (
            isinstance(item, dict)
            and item.get("essential") is True
            and item.get("obtainable_via_setup") is False
            and item.get("source_kind") == "setup_output"
        )
    ]


def _plan_canonical_prerequisite_findings(
    plan: dict[str, Any],
    inventory: dict[str, Any],
) -> list[Finding]:
    declared_bindings = _declared_binding_names(plan.get("runtime_bindings"))
    prerequisites = plan.get("prerequisites")
    if not isinstance(prerequisites, list):
        return []
    return _canonical_prerequisite_findings(
        prerequisites,
        _inventory_references(inventory),
        declared_bindings,
        plan.get("runtime_bindings"),
        safe_behavior=_plan_safe_behavior(plan),
    )


def collect_plan_findings(
    plan: Any,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    provenance_ids: Collection[str] = frozenset(),
    transformations: list[dict[str, Any]] | None = None,
) -> list[Finding]:
    """Normalize the plan's bindings in place and return every structural Call 1 finding.

    This is the shared field validator behind ``collect_plan_findings_v2``.
    Bindings are canonicalized and prerequisite binding consumers are added
    before the checks, so the binding checks see the consumer list a
    prerequisite completes. Prerequisite contents are not validated here; the
    canonical prerequisite validator runs only from the v2 entry point.
    """

    if not isinstance(plan, dict):
        return [Finding("response_type_error", "plan must be an object", "response", stage="plan")]
    return _normalized_plan_findings(
        plan,
        inventory,
        runtime_contract,
        provenance_ids,
        transformations,
        report_root_presence=True,
    )


def _normalized_plan_findings(
    plan: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    provenance_ids: Collection[str],
    transformations: list[dict[str, Any]] | None,
    *,
    report_root_presence: bool,
) -> list[Finding]:
    """Normalize the bindings and prerequisite consumers, then check the shared fields.

    ``report_root_presence`` includes the unexpected and missing root fields;
    the v2 entry point reports those against the full v2 field list itself.
    """

    runtime_bindings = plan.get("runtime_bindings")
    if isinstance(runtime_bindings, list):
        normalize_binding_declarations(
            runtime_bindings, inventory=inventory, transformations=transformations
        )
    _normalize_prerequisite_binding_consumers(
        plan.get("prerequisites"), runtime_bindings, transformations=transformations
    )
    findings = _plan_root_presence_findings(plan) if report_root_presence else []
    findings.extend(_plan_field_findings(plan, inventory, runtime_contract, provenance_ids))
    return staged_findings(findings, "plan")


def _plan_field_findings(
    plan: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    provenance_ids: Collection[str],
) -> list[Finding]:
    findings = _plan_list_type_findings(plan)
    references = _inventory_references(inventory)
    findings.extend(_selected_evidence_findings(plan.get("selected_evidence"), references))
    findings.extend(_interpretation_findings(plan, references, provenance_ids))
    setup_recipe = plan.get("setup_recipe")
    if isinstance(setup_recipe, list):
        findings.extend(_collect_setup_findings(setup_recipe, inventory, runtime_contract))
    runtime_bindings = plan.get("runtime_bindings")
    if isinstance(runtime_bindings, list):
        findings.extend(
            _collect_binding_findings(
                runtime_bindings,
                inventory,
                runtime_contract,
                finding_code="plan_binding_validation",
            )
        )
        findings.extend(_judge_consumer_findings(plan, finding_code="plan_binding_validation"))
    findings.extend(_stimulus_approach_findings(plan, runtime_contract))
    findings.extend(_observation_claim_findings(plan, runtime_contract))
    findings.extend(_semantic_judge_plan_findings(plan))
    unresolved = plan.get("unresolved_requirements")
    if isinstance(unresolved, list):
        findings.extend(_unresolved_requirement_findings(unresolved))
    return findings


def _unresolved_requirement_findings(unresolved: list[Any]) -> list[Finding]:
    findings: list[Finding] = []
    for index, item in enumerate(unresolved):
        if not isinstance(item, dict):
            findings.append(
                Finding(
                    "shape_error",
                    "unresolved requirement must be an object",
                    f"unresolved_requirements[{index}]",
                )
            )
        elif (
            not isinstance(item.get("name"), str)
            or not isinstance(item.get("essential"), bool)
            or not isinstance(item.get("reason"), str)
        ):
            findings.append(
                Finding(
                    "shape_error",
                    "unresolved requirement requires name, essential, and reason",
                    f"unresolved_requirements[{index}]",
                )
            )
    return findings


# The plan root fields the shared field validator checks: every root field
# except assumptions and required_observations, which only the v2 entry point
# checks.
_SHARED_PLAN_ROOT_FIELDS = (
    "interpretation",
    "selected_evidence",
    "setup_recipe",
    "runtime_bindings",
    "prerequisites",
    "stimulus_approach",
    "observation_claim",
    "semantic_judge",
    "unresolved_requirements",
)
_PLAN_LIST_FIELDS = (
    "selected_evidence",
    "setup_recipe",
    "runtime_bindings",
    "prerequisites",
    "unresolved_requirements",
)


def _plan_root_presence_findings(plan: dict[str, Any]) -> list[Finding]:
    return _v2_root_field_findings(plan, list(_SHARED_PLAN_ROOT_FIELDS))


def _plan_list_type_findings(plan: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for field_name in _PLAN_LIST_FIELDS:
        if field_name in plan and not isinstance(plan[field_name], list):
            findings.append(
                Finding(
                    "type_error",
                    f"{field_name} must be a list",
                    field_name,
                )
            )
    return findings


def _selected_evidence_findings(selected: Any, references: set[str]) -> list[Finding]:
    if not isinstance(selected, list):
        return []
    findings: list[Finding] = []
    for index, item in enumerate(selected):
        path = f"selected_evidence[{index}]"
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("ref"), str)
            or not isinstance(item.get("role"), str)
            or not isinstance(item.get("source"), str)
        ):
            findings.append(
                Finding(
                    "shape_error",
                    "selected evidence requires ref, role, and source strings",
                    path,
                )
            )
            continue
        for key in sorted(set(item) - {"ref", "role", "source"}):
            findings.append(
                Finding(
                    "unexpected_field",
                    f"unexpected selected evidence field: {key}",
                    path,
                )
            )
        if item["ref"] not in references:
            findings.append(
                Finding("unknown_reference", f"unknown_reference: {item['ref']}", path)
            )
    return findings


def _interpretation_findings(
    plan: dict[str, Any],
    references: set[str],
    provenance_ids: Collection[str],
) -> list[Finding]:
    interpretation = plan.get("interpretation")
    if not isinstance(interpretation, dict):
        if "interpretation" in plan:
            return [Finding("type_error", "interpretation must be an object", "interpretation")]
        return []
    findings: list[Finding] = []
    for field_name in sorted(
        set(interpretation) - {"failure", "safe_alternative", "conditions", "source_refs"}
    ):
        findings.append(
            Finding(
                "unexpected_field",
                f"unexpected interpretation field: {field_name}",
                f"interpretation.{field_name}",
            )
        )
    for field_name in ("failure", "safe_alternative"):
        if not isinstance(interpretation.get(field_name), str):
            findings.append(
                Finding(
                    "shape_error",
                    f"interpretation.{field_name} must be a string",
                    f"interpretation.{field_name}",
                )
            )
    findings.extend(_interpretation_condition_findings(interpretation.get("conditions")))
    findings.extend(
        _interpretation_source_ref_findings(
            interpretation.get("source_refs"), references, provenance_ids
        )
    )
    return findings


def _interpretation_condition_findings(conditions: Any) -> list[Finding]:
    if not isinstance(conditions, list):
        return [
            Finding(
                "type_error",
                "interpretation.conditions must be a list",
                "interpretation.conditions",
            )
        ]
    return [
        Finding(
            "type_error",
            "interpretation.conditions items must be strings",
            f"interpretation.conditions[{index}]",
        )
        for index, condition in enumerate(conditions)
        if not isinstance(condition, str)
    ]


def _interpretation_source_ref_findings(
    source_refs: Any,
    references: set[str],
    provenance_ids: Collection[str],
) -> list[Finding]:
    if not isinstance(source_refs, list):
        return [
            Finding(
                "type_error",
                "interpretation.source_refs must be a list",
                "interpretation.source_refs",
            )
        ]
    findings: list[Finding] = []
    for index, ref in enumerate(source_refs):
        if not isinstance(ref, str):
            findings.append(
                Finding(
                    "type_error",
                    "interpretation source reference must be a string",
                    f"interpretation.source_refs[{index}]",
                )
            )
        elif ref not in references and ref not in provenance_ids:
            findings.append(
                Finding(
                    "unknown_reference",
                    f"unknown_reference: {ref}",
                    f"interpretation.source_refs[{index}]",
                )
            )
    return findings


def _stimulus_approach_findings(
    plan: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> list[Finding]:
    approach = plan.get("stimulus_approach")
    if not isinstance(approach, dict):
        if "stimulus_approach" in plan:
            return [
                Finding("type_error", "stimulus_approach must be an object", "stimulus_approach")
            ]
        return []
    findings: list[Finding] = []
    if not isinstance(approach.get("request"), str):
        findings.append(
            Finding(
                "shape_error",
                "stimulus_approach.request must be a string",
                "stimulus_approach.request",
            )
        )
    findings.extend(plan_placeholder_findings(approach))
    delivery = approach.get("delivery")
    if delivery not in runtime_contract.get("delivery", []):
        findings.append(
            Finding(
                "closed_value_error",
                f"undocumented delivery capability: {delivery}",
                "stimulus_approach.delivery",
            )
        )
    findings.extend(_stimulus_approach_history_findings(approach))
    for field_name in sorted(set(approach) - {"request", "delivery", "history"}):
        findings.append(
            Finding(
                "unexpected_field",
                f"unexpected stimulus_approach field: {field_name}",
                f"stimulus_approach.{field_name}",
            )
        )
    return findings


def _stimulus_approach_history_findings(approach: dict[str, Any]) -> list[Finding]:
    history = approach.get("history", [])
    if not isinstance(history, list):
        return [
            Finding(
                "type_error",
                "stimulus_approach.history must be a list",
                "stimulus_approach.history",
            )
        ]
    if "history" not in approach:
        return [
            Finding(
                "missing_field",
                "stimulus_approach missing field: history",
                "stimulus_approach.history",
            )
        ]
    return [
        Finding(
            "type_error",
            "stimulus_approach.history items must be strings",
            f"stimulus_approach.history[{index}]",
        )
        for index, item in enumerate(history)
        if not isinstance(item, str)
    ]


def _observation_claim_findings(
    plan: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> list[Finding]:
    claim = plan.get("observation_claim")
    if not isinstance(claim, dict):
        if "observation_claim" in plan:
            return [
                Finding("type_error", "observation_claim must be an object", "observation_claim")
            ]
        return []
    findings: list[Finding] = []
    for field_name in sorted(set(claim) - {"violation", "absence", "inconclusive", "claim_level"}):
        findings.append(
            Finding(
                "unexpected_field",
                f"unexpected observation_claim field: {field_name}",
                f"observation_claim.{field_name}",
            )
        )
    for field_name in ("violation", "absence", "inconclusive"):
        if not isinstance(claim.get(field_name), str):
            findings.append(
                Finding(
                    "shape_error",
                    f"observation_claim.{field_name} must be a string",
                    f"observation_claim.{field_name}",
                )
            )
    findings.extend(_observation_claim_level_findings(claim.get("claim_level"), runtime_contract))
    return findings


def _observation_claim_level_findings(
    claim_level: Any,
    runtime_contract: dict[str, Any],
) -> list[Finding]:
    supported_levels = _supported_claim_levels(runtime_contract)
    if claim_level not in CLAIM_LEVELS:
        return [
            Finding(
                "closed_value_error",
                "observation_claim must declare a closed claim_level",
                "observation_claim.claim_level",
            )
        ]
    if claim_level not in supported_levels:
        return [
            Finding(
                "unsupported_claim_level",
                f"observation_claim.claim_level {claim_level} is not supported by "
                "downstream execution; the supported claim levels are "
                f"{', '.join(supported_levels)}. Choose the supported claim level "
                "at which the failure criterion is observable, and state "
                "violation, absence, and inconclusive at that level.",
                "observation_claim.claim_level",
            )
        ]
    return []


def _plan_claim_level(plan: Any) -> Any:
    claim = plan.get("observation_claim") if isinstance(plan, dict) else None
    return claim.get("claim_level") if isinstance(claim, dict) else None


def _semantic_judge_plan_findings(plan: dict[str, Any]) -> list[Finding]:
    judge = plan.get("semantic_judge")
    if not isinstance(judge, dict):
        if "semantic_judge" in plan:
            return [Finding("type_error", "semantic_judge must be an object", "semantic_judge")]
        return []
    findings: list[Finding] = []
    if judge.get("needed") is False and _plan_claim_level(plan) == ClaimLevel.REPLY:
        findings.append(
            Finding(
                "semantic_judge_required",
                "observation_claim.claim_level is reply, and a reply claim is scored only "
                "by the semantic judge; set semantic_judge.needed to true and state its "
                "scope",
                "semantic_judge.needed",
            )
        )
    for field_name in sorted(set(judge) - {"needed", "scope"}):
        findings.append(
            Finding(
                "unexpected_field",
                f"unexpected semantic_judge field: {field_name}",
                f"semantic_judge.{field_name}",
            )
        )
    if "needed" not in judge:
        findings.append(
            Finding(
                "missing_field",
                "semantic_judge missing field: needed",
                "semantic_judge.needed",
            )
        )
    elif not isinstance(judge.get("needed"), bool):
        findings.append(
            Finding(
                "type_error",
                "semantic_judge.needed must be a boolean",
                "semantic_judge.needed",
            )
        )
    findings.extend(_semantic_judge_scope_findings(judge))
    return findings


def _semantic_judge_scope_findings(judge: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    if "scope" not in judge:
        findings.append(
            Finding(
                "missing_field",
                "semantic_judge missing field: scope",
                "semantic_judge.scope",
            )
        )
    elif judge.get("scope") is not None and not isinstance(judge.get("scope"), str):
        findings.append(
            Finding(
                "type_error",
                "semantic_judge.scope must be a string or null",
                "semantic_judge.scope",
            )
        )
    if judge.get("needed") is True and not isinstance(judge.get("scope"), str):
        findings.append(
            Finding(
                "shape_error",
                "semantic_judge.scope is required when needed",
                "semantic_judge.scope",
            )
        )
    return findings


def _collect_setup_findings(
    recipe: list[Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> list[Finding]:
    findings: list[Finding] = []
    for index, step in enumerate(recipe):
        try:
            _validate_setup_recipe([step], inventory, runtime_contract)
        except PlanValidationError as exc:
            child = _findings_from_error(exc)[0]
            findings.append(
                Finding(
                    child.code,
                    child.detail,
                    f"setup_recipe[{index}]",
                )
            )
    return findings


def _collect_binding_findings(
    declarations: list[Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    finding_code: str = "artifact_validation",
) -> list[Finding]:
    """Check declarations that ``normalize_binding_declarations`` already canonicalized."""

    findings: list[Finding] = []
    names: dict[str, int] = {}
    for index, raw in enumerate(declarations):
        path = f"runtime_bindings[{index}]"
        if isinstance(raw, dict) and isinstance(raw.get("name"), str):
            if raw["name"] in names:
                findings.append(Finding(finding_code, f"duplicate binding: {raw['name']}", path))
            names[raw["name"]] = index
        nested = _collect_binding_nested_findings(
            raw,
            inventory=inventory,
            runtime_contract=runtime_contract,
            path=path,
            finding_code=finding_code,
        )
        findings.extend(nested)
        if not nested:
            try:
                validate_bindings([raw], inventory=inventory, runtime_contract=runtime_contract)
            except BindingValidationError as exc:
                findings.append(Finding(finding_code, str(exc), path))
    return findings


def _collect_binding_nested_findings(
    raw: Any,
    *,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    path: str,
    finding_code: str,
) -> list[Finding]:
    """Collect independent binding faults without changing the closed validator."""

    if not isinstance(raw, dict):
        return [Finding(finding_code, "binding must be an object", path)]

    findings = _binding_field_findings(raw, path=path, finding_code=finding_code)
    name = raw.get("name")
    expected_type = raw.get("expected_type")
    source_kind = raw.get("source_kind")
    findings.extend(
        _binding_closed_value_findings(
            name, expected_type, source_kind, path=path, finding_code=finding_code
        )
    )
    source_ref, selector = _canonicalize_binding_source(raw, source_kind, inventory)
    source_schema, source_findings = _binding_source_ref_findings(
        source_kind,
        source_ref,
        name,
        inventory=inventory,
        runtime_contract=runtime_contract,
        path=path,
        finding_code=finding_code,
    )
    findings.extend(source_findings)
    on_missing = raw.get("on_missing")
    if isinstance(on_missing, str) and on_missing not in MISSING_POLICIES:
        findings.append(
            Finding(
                finding_code,
                f"binding on_missing is not closed: {name}",
                f"{path}.on_missing",
            )
        )
    findings.extend(
        _binding_consumer_findings(raw.get("consumers"), path=path, finding_code=finding_code)
    )
    if not isinstance(selector, str):
        if "selector" in raw:
            findings.append(
                Finding(
                    finding_code,
                    f"binding selector must be a string: {name}",
                    f"{path}.selector",
                )
            )
    else:
        findings.extend(
            _binding_selector_findings(
                selector,
                name,
                expected_type,
                source_schema,
                path=path,
                finding_code=finding_code,
            )
        )
    return findings


def _binding_field_findings(
    raw: dict[str, Any],
    *,
    path: str,
    finding_code: str,
) -> list[Finding]:
    findings: list[Finding] = []
    required = frozenset(BINDING_SPEC.fields)
    for field_name in sorted(required - set(raw)):
        findings.append(
            Finding(
                finding_code,
                f"binding missing field: {field_name}",
                f"{path}.{field_name}",
            )
        )
    for field_name in sorted(set(raw) - required):
        findings.append(
            Finding(
                finding_code,
                f"binding has unsupported field: {field_name}",
                f"{path}.{field_name}",
            )
        )
    for field_name in BINDING_SPEC.string_fields:
        if field_name in raw and not isinstance(raw[field_name], str):
            findings.append(
                Finding(
                    finding_code,
                    f"binding {field_name} must be a string",
                    f"{path}.{field_name}",
                )
            )
    return findings


def _binding_closed_value_findings(
    name: Any,
    expected_type: Any,
    source_kind: Any,
    *,
    path: str,
    finding_code: str,
) -> list[Finding]:
    findings: list[Finding] = []
    if isinstance(name, str) and not name.strip():
        findings.append(Finding(finding_code, "binding name is blank", f"{path}.name"))
    if isinstance(expected_type, str) and expected_type not in CLOSED_TYPES:
        findings.append(
            Finding(
                finding_code,
                f"binding expected_type is not closed: {name}",
                f"{path}.expected_type",
            )
        )
    if isinstance(source_kind, str) and source_kind not in SOURCE_KINDS:
        findings.append(
            Finding(
                finding_code,
                f"binding source_kind is not closed: {name}",
                f"{path}.source_kind",
            )
        )
    return findings


def _canonicalize_binding_source(
    raw: dict[str, Any],
    source_kind: Any,
    inventory: dict[str, Any],
) -> tuple[Any, Any]:
    """Rewrite ``source_ref`` and ``selector`` in place to their canonical paths.

    Returns the (possibly rewritten) source reference and selector.
    """

    source_ref = raw.get("source_ref")
    selector = raw.get("selector")
    if not (
        isinstance(source_kind, str) and isinstance(source_ref, str) and isinstance(selector, str)
    ):
        return source_ref, selector
    canonical_source_ref, canonical_selector = canonical_binding_paths(
        source_kind,
        source_ref,
        selector,
        inventory,
    )
    if (canonical_source_ref, canonical_selector) != (source_ref, selector):
        raw["source_ref"] = canonical_source_ref
        raw["selector"] = canonical_selector
    return canonical_source_ref, canonical_selector


def _binding_source_ref_findings(
    source_kind: Any,
    source_ref: Any,
    name: Any,
    *,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    path: str,
    finding_code: str,
) -> tuple[dict[str, Any] | None, list[Finding]]:
    if not isinstance(source_ref, str):
        return None, []
    if not source_ref.strip():
        return None, [
            Finding(
                finding_code,
                f"binding source reference is blank: {name}",
                f"{path}.source_ref",
            )
        ]
    if source_kind not in SOURCE_KINDS:
        return None, []
    source_schema, source_error = _binding_source_schema(
        source_kind,
        source_ref,
        inventory,
        runtime_contract,
        name,
    )
    if source_error:
        return source_schema, [Finding(finding_code, source_error, f"{path}.source_ref")]
    return source_schema, []


def _binding_consumer_findings(
    consumers: Any,
    *,
    path: str,
    finding_code: str,
) -> list[Finding]:
    if not isinstance(consumers, list):
        return [
            Finding(
                finding_code,
                "binding consumers must be a list",
                f"{path}.consumers",
            )
        ]
    if not consumers:
        return [
            Finding(
                finding_code,
                "binding consumers must be non-empty strings",
                f"{path}.consumers",
            )
        ]
    findings: list[Finding] = []
    for consumer_index, consumer in enumerate(consumers):
        consumer_path = f"{path}.consumers[{consumer_index}]"
        if not isinstance(consumer, str) or not consumer.strip():
            findings.append(
                Finding(
                    finding_code,
                    "binding consumers must be non-empty strings",
                    consumer_path,
                )
            )
        elif consumer.startswith(_RETIRED_DETECTOR_PREFIX):
            judge_destination = JUDGE_CONSUMERS.prefix + consumer[len(_RETIRED_DETECTOR_PREFIX) :]
            findings.append(
                Finding(
                    finding_code,
                    f"binding consumer {consumer} is not a closed path; the semantic "
                    f"judge of a reply claim reads {judge_destination}",
                    consumer_path,
                )
            )
        elif not BINDING_SPEC.is_closed_consumer(consumer):
            findings.append(
                Finding(
                    finding_code,
                    "binding consumer is not a closed path",
                    consumer_path,
                )
            )
    return findings


def _judge_consumer_findings(plan: dict[str, Any], *, finding_code: str) -> list[Finding]:
    """Report a ``judge.`` consumer that the plan's claim level gives no judge to read."""

    claim_level = _plan_claim_level(plan)
    if claim_level not in CLAIM_LEVELS or claim_level == ClaimLevel.REPLY:
        return []
    return [
        Finding(
            finding_code,
            f"binding consumer {consumer} is valid only when "
            "observation_claim.claim_level is reply",
            f"runtime_bindings[{index}].consumers[{consumer_index}]",
        )
        for index, consumer_index, consumer in _declared_consumers(plan.get("runtime_bindings"))
        if consumer.startswith(JUDGE_CONSUMERS.prefix)
    ]


def _declared_consumers(declarations: Any) -> list[tuple[int, int, str]]:
    """List each string consumer of well-formed declarations with its two indexes."""

    return [
        (index, consumer_index, consumer)
        for index, raw in enumerate(declarations if isinstance(declarations, list) else [])
        for consumer_index, consumer in enumerate(_consumer_list(raw))
        if isinstance(consumer, str)
    ]


def _consumer_list(declaration: Any) -> list[Any]:
    consumers = declaration.get("consumers") if isinstance(declaration, dict) else None
    return consumers if isinstance(consumers, list) else []


def _binding_selector_findings(
    selector: str,
    name: Any,
    expected_type: Any,
    source_schema: dict[str, Any] | None,
    *,
    path: str,
    finding_code: str,
) -> list[Finding]:
    if not selector.strip():
        return [
            Finding(
                finding_code,
                f"binding selector is blank: {name}",
                f"{path}.selector",
            )
        ]
    if _selector_root(selector) is None:
        return [
            Finding(
                finding_code,
                (
                    "selector must be an exact documented dot path rooted at value "
                    "for supplied_input or result for setup_output"
                ),
                f"{path}.selector",
            )
        ]
    if source_schema is None:
        return []
    actual_type = _binding_selector_type(source_schema, selector)
    if actual_type is None:
        return [
            Finding(
                finding_code,
                f"undocumented selector for binding {name}: {selector}",
                f"{path}.selector",
            )
        ]
    if (
        isinstance(expected_type, str)
        and expected_type in CLOSED_TYPES
        and not _binding_types_compatible(actual_type, expected_type)
    ):
        return [
            Finding(
                finding_code,
                (
                    f"binding type mismatch for {name}: expected {expected_type}, "
                    f"source is {actual_type}"
                ),
                f"{path}.selector",
            )
        ]
    return []


def _binding_source_schema(
    source_kind: str,
    source_ref: str,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    name: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    canonical_source_ref, _ = canonical_binding_paths(
        source_kind,
        source_ref,
        "value" if source_kind == "supplied_input" else "result",
        inventory,
    )
    source_ref = canonical_source_ref
    prefix, _, reference = source_ref.partition(":")
    expected_prefix = "setup" if source_kind == "setup_output" else "facts"
    if prefix != expected_prefix or not reference:
        reference_label = "operation" if source_kind == "setup_output" else "ref"
        return (
            None,
            (
                f"{source_kind} binding source_ref must be "
                f"{expected_prefix}:<{reference_label}>: "
                f"{name}"
            ),
        )
    if source_kind == "setup_output":
        schema, error = _setup_output_source_schema(reference, inventory, runtime_contract)
    else:
        schema, error = _supplied_input_source_schema(reference, inventory)
    if error is not None:
        return None, error
    if not isinstance(schema, dict):
        return None, f"missing source schema for binding: {name}"
    return schema, None


def _setup_output_source_schema(
    reference: str,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> tuple[Any, str | None]:
    """Return a permitted setup operation's raw result schema, or why it is unusable."""

    operation = next(
        (
            item
            for item in inventory.get("operations", [])
            if isinstance(item, dict) and item.get("name") == reference
        ),
        None,
    )
    if operation is None:
        return None, f"unknown setup operation: {reference}"
    if reference not in runtime_contract.get("setup_permissions", []):
        return None, f"setup operation is not permitted: {reference}"
    return operation.get("result_schema"), None


def _supplied_input_source_schema(
    reference: str,
    inventory: dict[str, Any],
) -> tuple[Any, str | None]:
    """Return a supplied fact's raw schema, or why the fact is unknown."""

    fact = _first_fact_named(inventory, reference)
    if fact is None:
        return None, f"unknown supplied fact: {reference}"
    return fact.get("schema"), None


def _selector_root(selector: str) -> str | None:
    root = selector.split(".", 1)[0]
    return root if root in {"result", "value"} else None


def _collect_canonical_prerequisite_findings(
    prerequisites: list[Any],
    references: set[str],
    declared_bindings: set[str],
    runtime_bindings: Any,
    *,
    safe_behavior: str | None = None,
    transformations: list[dict[str, Any]] | None = None,
) -> list[Finding]:
    """Add the prerequisite binding consumers, then validate the closed prerequisite form."""

    _normalize_prerequisite_binding_consumers(
        prerequisites,
        runtime_bindings,
        transformations=transformations,
    )
    return _canonical_prerequisite_findings(
        prerequisites,
        references,
        declared_bindings,
        runtime_bindings,
        safe_behavior=safe_behavior,
    )


def _canonical_prerequisite_findings(
    prerequisites: list[Any],
    references: set[str],
    declared_bindings: set[str],
    runtime_bindings: Any,
    *,
    safe_behavior: str | None = None,
) -> list[Finding]:
    """Validate the closed prerequisite form used by the v2 plan wire.

    This is the only prerequisite validator. It expects the prerequisite
    binding consumers to be added already.
    """

    findings: list[Finding] = []
    for index, prerequisite in enumerate(prerequisites):
        findings.extend(
            _canonical_prerequisite_item_findings(
                prerequisite,
                index=index,
                references=references,
                declared_bindings=declared_bindings,
                runtime_bindings=runtime_bindings,
                safe_behavior=safe_behavior,
            )
        )
    return findings


_CANONICAL_PREREQUISITE_FIELDS = frozenset({"name", "check", "evidence_refs", "binding", "equals"})


def _canonical_prerequisite_item_findings(
    prerequisite: Any,
    *,
    index: int,
    references: set[str],
    declared_bindings: set[str],
    runtime_bindings: Any,
    safe_behavior: str | None,
) -> list[Finding]:
    path = f"prerequisites[{index}]"
    if not isinstance(prerequisite, dict):
        return [Finding("shape_error", "prerequisite must be an object", path)]
    findings = _canonical_prerequisite_field_findings(prerequisite, path)
    findings.extend(_prerequisite_check_findings(prerequisite, path, safe_behavior))
    findings.extend(_prerequisite_binding_findings(prerequisite, path, declared_bindings))
    findings.extend(_prerequisite_equals_findings(prerequisite, path, runtime_bindings))
    findings.extend(_prerequisite_evidence_ref_findings(prerequisite, path, references))
    findings.extend(
        _validate_prerequisite_binding_consumer(
            prerequisite,
            index=index,
            runtime_bindings=runtime_bindings,
        )
    )
    return findings


def _canonical_prerequisite_field_findings(
    prerequisite: dict[str, Any],
    path: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for field_name in sorted(set(prerequisite) - _CANONICAL_PREREQUISITE_FIELDS):
        findings.append(
            Finding(
                "unexpected_field",
                f"unexpected prerequisite field: {field_name}",
                f"{path}.{field_name}",
            )
        )
    for field_name in sorted(_CANONICAL_PREREQUISITE_FIELDS - set(prerequisite)):
        findings.append(
            Finding(
                "missing_field",
                f"prerequisite missing field: {field_name}",
                f"{path}.{field_name}",
            )
        )
    if not isinstance(prerequisite.get("name"), str) or not prerequisite.get("name", "").strip():
        findings.append(
            Finding("type_error", "prerequisite.name must be a string", f"{path}.name")
        )
    return findings


def _prerequisite_check_findings(
    prerequisite: dict[str, Any],
    path: str,
    safe_behavior: str | None,
) -> list[Finding]:
    if "check" in prerequisite and not isinstance(prerequisite.get("check"), str):
        return [Finding("type_error", "prerequisite.check must be a string", f"{path}.check")]
    if _same_authored_text(prerequisite.get("check"), safe_behavior):
        return [
            Finding(
                "desired_behavior_prerequisite",
                (
                    "intended safe behavior is a detector criterion, "
                    "not a starting-state prerequisite"
                ),
                f"{path}.check",
            )
        ]
    return []


def _prerequisite_binding_findings(
    prerequisite: dict[str, Any],
    path: str,
    declared_bindings: set[str],
) -> list[Finding]:
    binding = prerequisite.get("binding")
    if not isinstance(binding, str) or not binding.strip():
        if "binding" in prerequisite:
            return [
                Finding(
                    "type_error",
                    "prerequisite.binding must be a non-empty binding name",
                    f"{path}.binding",
                )
            ]
        return []
    if binding not in declared_bindings:
        return [
            Finding(
                "unknown_binding",
                (
                    f"prerequisite binding is not declared: {binding}; "
                    "bare evidence IDs and bindings.<name> selectors are not executable"
                ),
                f"{path}.binding",
            )
        ]
    return []


def _prerequisite_equals_findings(
    prerequisite: dict[str, Any],
    path: str,
    runtime_bindings: Any,
) -> list[Finding]:
    findings: list[Finding] = []
    if "equals" in prerequisite and not _is_json_value(prerequisite["equals"]):
        findings.append(
            Finding(
                "type_error",
                "prerequisite.equals must be a JSON value",
                f"{path}.equals",
            )
        )
    binding = prerequisite.get("binding")
    expected_types = _declared_binding_expected_types(runtime_bindings)
    expected_type = expected_types.get(binding) if isinstance(binding, str) else None
    if (
        expected_type in CLOSED_TYPES
        and "equals" in prerequisite
        and prerequisite["equals"] is not None
        and _is_json_value(prerequisite["equals"])
    ):
        equals_type = _json_value_type(prerequisite["equals"])
        if not _binding_types_compatible(equals_type, expected_type):
            findings.append(
                Finding(
                    "prerequisite_type_mismatch",
                    (
                        f"prerequisite binding {binding} has expected_type "
                        f"{expected_type}, but equals has JSON type {equals_type}"
                    ),
                    f"{path}.equals",
                )
            )
    return findings


def _prerequisite_evidence_ref_findings(
    prerequisite: dict[str, Any],
    path: str,
    references: set[str],
) -> list[Finding]:
    evidence_refs = prerequisite.get("evidence_refs")
    if isinstance(evidence_refs, list):
        return [
            Finding(
                "unknown_reference",
                f"unknown_reference: {ref}",
                f"{path}.evidence_refs[{ref_index}]",
            )
            for ref_index, ref in enumerate(evidence_refs)
            if not isinstance(ref, str) or not ref.strip() or ref not in references
        ]
    if "evidence_refs" in prerequisite:
        return [
            Finding(
                "type_error",
                "prerequisite evidence_refs must be a list",
                f"{path}.evidence_refs",
            )
        ]
    return []


def _plan_safe_behavior(plan: dict[str, Any]) -> str | None:
    interpretation = plan.get("interpretation")
    if not isinstance(interpretation, dict):
        return None
    safe_behavior = interpretation.get("safe_alternative")
    return safe_behavior if isinstance(safe_behavior, str) else None


def _same_authored_text(left: Any, right: str | None) -> bool:
    """Compare author text without pretending to understand its semantics."""

    return (
        isinstance(left, str)
        and isinstance(right, str)
        and " ".join(left.split()).casefold() == " ".join(right.split()).casefold()
    )


def _validate_prerequisite_binding_consumer(
    prerequisite: dict[str, Any],
    *,
    index: int,
    runtime_bindings: Any,
) -> list[Finding]:
    """Require a declared binding to name this prerequisite as a consumer."""

    binding_name = prerequisite.get("binding")
    if not isinstance(binding_name, str) or not isinstance(runtime_bindings, list):
        return []
    declaration = next(
        (
            item
            for item in runtime_bindings
            if isinstance(item, dict) and item.get("name") == binding_name
        ),
        None,
    )
    if not isinstance(declaration, dict):
        return []
    consumers = declaration.get("consumers")
    if isinstance(consumers, list) and f"prerequisites.{binding_name}" not in consumers:
        return [
            Finding(
                "consumer_mismatch",
                (
                    f"binding {binding_name} does not declare prerequisite consumer "
                    f"prerequisites.{binding_name}"
                ),
                f"prerequisites[{index}].binding",
            )
        ]
    return []


def _consumer_declarations_by_name(runtime_bindings: list[Any]) -> dict[str, dict[str, Any]]:
    return {
        item["name"]: item
        for item in runtime_bindings
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("consumers"), list)
        )
    }


def _normalize_prerequisite_binding_consumers(
    prerequisites: Any,
    runtime_bindings: Any,
    *,
    transformations: list[dict[str, Any]] | None = None,
) -> None:
    """Add the closed prerequisite consumer for each declared binding use."""

    if not isinstance(prerequisites, list) or not isinstance(runtime_bindings, list):
        return
    by_name = _consumer_declarations_by_name(runtime_bindings)
    for index, prerequisite in enumerate(prerequisites):
        if not isinstance(prerequisite, dict):
            continue
        binding_name = prerequisite.get("binding")
        declaration = by_name.get(binding_name)
        if not isinstance(binding_name, str) or not isinstance(declaration, dict):
            continue
        required_consumer = f"prerequisites.{binding_name}"
        consumers = declaration["consumers"]
        if required_consumer in consumers:
            continue
        original_consumers = list(consumers)
        consumers.append(required_consumer)
        _record_authoring_transformation(
            transformations,
            transformation="binding_consumer_added",
            binding=binding_name,
            original_consumers=original_consumers,
            canonical_consumers=list(consumers),
            prerequisite_index=index,
            prerequisite_name=prerequisite.get("name"),
            consumer=required_consumer,
        )


def _slot_names_in_order(user_text: str) -> list[str]:
    """Return unique placeholder names in first-appearance order."""

    return list(dict.fromkeys(match.group(1) for match in _SLOT_RE.finditer(user_text)))


def _normalize_stimulus_slots(
    stimulus: dict[str, Any],
    declared_binding_names: set[str],
    *,
    transformations: list[dict[str, Any]] | None = None,
) -> None:
    """Derive stimulus slot names when every placeholder is declared."""

    user_text = stimulus.get("user_text")
    slots = stimulus.get("slots")
    if not isinstance(user_text, str) or not isinstance(slots, list):
        return
    derived_slots = _slot_names_in_order(user_text)
    if not derived_slots or not set(derived_slots).issubset(declared_binding_names):
        return
    if slots == derived_slots:
        return
    original_slots = list(slots)
    stimulus["slots"] = derived_slots
    _record_authoring_transformation(
        transformations,
        transformation="stimulus_slots_derived",
        original_slots=original_slots,
        derived_slots=list(derived_slots),
        placeholder_names=list(derived_slots),
    )


def _record_authoring_transformation(
    transformations: list[dict[str, Any]] | None,
    *,
    transformation: str,
    **details: Any,
) -> None:
    """Append one deterministic authoring rewrite record when requested."""

    if transformations is None:
        return
    transformations.append({"transformation": transformation, **details})


def _declared_binding_names(runtime_bindings: Any) -> set[str]:
    if not isinstance(runtime_bindings, list):
        return set()
    return {
        item["name"]
        for item in runtime_bindings
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def _declared_binding_expected_types(runtime_bindings: Any) -> dict[str, str]:
    if not isinstance(runtime_bindings, list):
        return {}
    return {
        item["name"]: item["expected_type"]
        for item in runtime_bindings
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("expected_type"), str)
        )
    }


def _validate_setup_recipe(
    recipe: Any,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
) -> None:
    if not isinstance(recipe, list):
        raise PlanValidationError("setup_recipe must be a list")
    operations = {
        item.get("name"): item
        for item in inventory.get("operations", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    permissions = runtime_contract.get("setup_permissions", [])
    for index, step in enumerate(recipe):
        _validate_setup_step_shape(index, step)
        name = step["operation"]
        if name not in operations:
            raise PlanValidationError(f"unknown setup operation: {name}")
        if name not in permissions:
            raise PlanValidationError(f"setup operation is not permitted: {name}")
        supplied_args = step.get("arguments", {})
        if not isinstance(supplied_args, dict):
            raise PlanValidationError(f"setup_recipe[{index}].arguments must be an object")
        _validate_setup_arguments(supplied_args, operations[name].get("arguments", {}))


def _validate_setup_step_shape(index: int, step: Any) -> None:
    """Require one setup step to name an operation and carry only operation and arguments."""

    if not isinstance(step, dict) or not isinstance(step.get("operation"), str):
        raise PlanValidationError(f"setup_recipe[{index}] must name an operation")
    unexpected = set(step) - {"operation", "arguments"}
    if unexpected:
        raise PlanValidationError(
            f"setup_recipe[{index}] has unsupported fields: {sorted(unexpected)}"
        )
    if "arguments" not in step:
        raise PlanValidationError(f"setup_recipe[{index}] must include arguments")


def _validate_setup_arguments(supplied_args: dict[str, Any], schema: Any) -> None:
    """Check supplied setup arguments against the operation's argument schema."""

    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    missing = set(required) - set(supplied_args)
    if missing:
        raise PlanValidationError(f"missing setup argument: {sorted(missing)[0]}")
    unknown = set(supplied_args) - set(properties)
    if unknown:
        raise PlanValidationError(f"unknown setup argument: {sorted(unknown)[0]}")
    for argument, value in supplied_args.items():
        schema_type = (
            properties.get(argument, {}).get("type")
            if isinstance(properties.get(argument), dict)
            else None
        )
        if schema_type and not _matches_schema_type(value, schema_type):
            raise PlanValidationError(
                f"schema_type_mismatch: setup argument {argument} expects {schema_type}"
            )


def _is_blocked_plan(plan: Any) -> bool:
    return isinstance(plan, dict) and any(
        isinstance(item, dict)
        and item.get("essential") is True
        and item.get("obtainable_via_setup") is not True
        and item.get("source_kind") != "setup_output"
        for item in plan.get("unresolved_requirements", [])
    )


def _applies_to_every_candidate(candidate: Mapping[str, Any]) -> bool:
    return True


def _has_judge_spec(metadata: Mapping[str, Any]) -> bool:
    """Tell whether the artifact carries a judge spec, the input of the fact-ref check."""

    return isinstance(metadata.get("semantic_judge_spec"), dict)


@dataclass(frozen=True)
class MechanicalCheck:
    """A structural check and the property a candidate that passed it has.

    ``functions`` are the checks in this module that enforce the property.
    ``applies`` tells whether the check examines a candidate at all; a check
    that does not apply to a candidate gives it no guarantee.
    """

    check_id: str
    guarantee: str
    functions: tuple[Callable[..., Any], ...]
    applies: Callable[[Mapping[str, Any]], bool] = _applies_to_every_candidate


def _or_list(values: tuple[str, ...]) -> str:
    return f"{', '.join(values[:-1])}, or {values[-1]}"


# One clause per BINDING_SPEC field, in the order the guarantee states them.
_RUNTIME_BINDING_CLAUSES = {
    "name": "a unique nonblank name",
    "source_kind": "a permitted source_kind",
    "source_ref": "a source_ref that resolves to a supplied fact or a permitted setup operation",
    "selector": (
        "a documented selector rooted at value or result (including a validated "
        "keyed-map source shorthand resolved to that form)"
    ),
    "expected_type": "a compatible expected_type",
    "consumers": "a nonempty closed consumer list",
    "on_missing": "a permitted on_missing policy",
}


def _runtime_bindings_guarantee() -> str:
    clauses = list(_RUNTIME_BINDING_CLAUSES.values())
    return (
        "Every runtime binding has the required closed fields, "
        f"{', '.join(clauses[:-1])}, and {clauses[-1]}."
    )


PLAN_MECHANICAL_CHECKS = (
    MechanicalCheck(
        "plan_root_fields",
        "The v2 plan has the required root fields and no unsupported root fields; the "
        "validator checks the object, list, string, boolean, enum, and JSON-value "
        "shapes for the plan fields it inspects, including required_observations as an "
        "object.",
        (_v2_root_field_findings, _plan_list_type_findings),
    ),
    MechanicalCheck(
        "plan_references",
        "Selected evidence, assumptions, prerequisite evidence references, and "
        "interpretation source references resolve to supplied inventory references; "
        "operation evidence references use the documented operation:<name> form, and "
        "interpretation may additionally cite supplied scenario-lineage or attack-tree "
        "provenance IDs.",
        (_selected_evidence_findings, _plan_assumptions_findings, _interpretation_findings),
    ),
    MechanicalCheck(
        "setup_recipe",
        "Every setup_recipe operation exists in the operation inventory, is listed in "
        "runtime_contract.setup_permissions, has an arguments object, satisfies required "
        "and known argument names, and matches documented argument types or an allowed "
        "binding slot.",
        (_collect_setup_findings,),
    ),
    MechanicalCheck(
        "runtime_bindings",
        _runtime_bindings_guarantee(),
        (_collect_binding_findings,),
    ),
    MechanicalCheck(
        "prerequisites",
        "Every prerequisite has the canonical closed fields and types, references a "
        "declared binding, uses an equals JSON value compatible with that binding's "
        "expected_type, requires any evidence_refs entries to resolve, and has the "
        "binding's prerequisite consumer declared.",
        (_plan_canonical_prerequisite_findings,),
    ),
    MechanicalCheck(
        "stimulus_claim_judge",
        "Stimulus delivery is listed in runtime_contract.delivery; claim_level is one "
        f"of {_or_list(CLAIM_LEVELS)}; and "
        "semantic_judge.needed and semantic_judge.scope have the enforced boolean and "
        "string-or-null shapes.",
        (_stimulus_approach_findings, _observation_claim_findings, _semantic_judge_plan_findings),
    ),
    MechanicalCheck(
        "unresolved_requirements",
        "Each unresolved requirement has the enforced shape; no essential requirement "
        "with source_kind setup_output is marked obtainable_via_setup false; and every "
        "other essential requirement is marked obtainable_via_setup true.",
        (_unresolved_requirement_findings, _unobtainable_requirement_findings),
    ),
)
ARTIFACT_MECHANICAL_CHECKS = (
    MechanicalCheck(
        "accepted_plan",
        "The accepted plan already passed the plan validator: root and nested field "
        "rules, reference existence, setup operation permissions and argument schemas, "
        "claim-level enum values, runtime-binding selector syntax and source type, "
        "binding consumers, prerequisite binding/equality rules, and unresolved "
        "requirement rules are facts established before this review.",
        (collect_plan_findings_v2,),
    ),
    MechanicalCheck(
        "call2_response",
        "The Call 2 response passed strict single-object framing and field validation: "
        "artifact fields, stimulus shape and delivery, user-only history, slot "
        "declarations, plan-owned-field preservation, required_observations shape, "
        "judge-spec shape, the accepted-plan judge choice, a judge spec for a reply "
        "claim, author-proposed example shapes, and, for a command_attempt claim, an "
        "unsafe and a safe capture whose calls name supplied operations, with the scenario's "
        "tool-call condition detecting the unsafe capture and not detecting the safe one.",
        (
            _validate_call2_metadata_shape,
            _artifact_stimulus_findings,
            _semantic_judge_decision_findings,
        ),
    ),
    MechanicalCheck(
        "judge_fact_refs",
        "Candidate judge fact_refs resolve to supplied inventory facts with supplied "
        "values; the accepted plan's binding and prerequisite declarations remain "
        "fixed. The documented selector forms are facts:<ref> plus value paths, "
        "setup:<operation> plus result paths, and keyed-map "
        "<fact ref>:records plus value.<key>.record_key.",
        (_semantic_judge_fact_ref_findings, _canonical_prerequisite_findings),
        applies=_has_judge_spec,
    ),
)
