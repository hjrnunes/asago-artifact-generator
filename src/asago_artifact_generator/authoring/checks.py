"""Closed structural checks for plan and artifact responses.

This module parses the fenced Call 2 response and collects the deterministic
findings that run before semantic review.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Collection, Mapping
from typing import Any

from ..bindings import (
    CLOSED_TYPES,
    MISSING_POLICIES,
    SOURCE_KINDS,
    BindingValidationError,
    canonical_binding_paths,
    find_stimulus_user_text_consumer_mismatches,
    normalize_binding_declarations,
    supplied_binding_values,
    validate_bindings,
)
from ..detector_controls import ESTABLISHED_TRIGGER_ROLE, uncited_trigger_observations
from .contracts import _SEMANTIC_JUDGE_SPEC_RULES, _call1_contract_v1, _call1_contract_v2
from .core import (
    _SLOT_RE,
    Call2FramingError,
    Finding,
    ParsedCall2Response,
    PlanValidationError,
    _claim_levels,
    _findings_from_error,
    _is_json_value,
    _json_value_type,
    _matches_schema_type,
    _supported_claim_levels,
)
from .inventory import _inventory_fact_map, _inventory_references


def parse_call2_response(raw: bytes | str) -> ParsedCall2Response:
    """Parse exactly one JSON block followed by one raw Python block.

    The parser works on bytes until metadata decoding is complete.  It never
    routes Python through JSON, so escapes, quotes, blank lines, and source
    encoding remain exactly as returned by the provider.
    """

    source = raw.encode("utf-8") if isinstance(raw, str) else raw
    if not isinstance(source, bytes):
        raise TypeError("Call 2 response must be bytes or text")
    findings: list[Finding] = []
    lines = source.splitlines(keepends=True)
    _require_json_opening_fence(lines, findings)
    json_lines, json_close_index = _read_fenced_block(lines, 1, "json", findings)
    if json_close_index is None:
        findings.append(
            Finding("truncated_block", "Call 2 JSON block is not closed", "call2.json")
        )
        raise Call2FramingError(findings)
    index = _python_block_start(lines, json_close_index + 1, findings)
    python_lines, python_close_index = _read_fenced_block(lines, index, "python", findings)
    if python_close_index is None:
        findings.append(
            Finding("truncated_block", "Call 2 Python block is not closed", "call2.python")
        )
        raise Call2FramingError(findings)
    _require_no_trailing_content(lines, python_close_index + 1, findings)
    metadata = _decode_call2_metadata(b"".join(json_lines), findings)
    findings.extend(_validate_call2_metadata_shape(metadata))
    python_bytes = b"".join(python_lines)
    tree = _parse_call2_python(python_bytes, findings)
    findings.extend(_evaluate_signature_findings(tree))
    if findings:
        raise Call2FramingError(findings)
    return ParsedCall2Response(metadata=metadata, python_bytes=python_bytes)


_DUPLICATE_BLOCK_FINDINGS = {
    "json": ("duplicate_json_block", "Call 2 contains more than one JSON block"),
    "python": ("duplicate_python_block", "Call 2 contains more than one Python block"),
}


def _duplicate_block_finding(language: str) -> Finding:
    code, message = _DUPLICATE_BLOCK_FINDINGS[language]
    return Finding(code, message, "call2")


def _require_json_opening_fence(lines: list[bytes], findings: list[Finding]) -> None:
    if lines and _is_fence_line(lines[0], "json", opening=True):
        return
    missing_json = bool(lines) and _is_fence_line(lines[0], "python", opening=True)
    findings.append(
        Finding(
            "missing_json_block" if not lines or missing_json else "ambiguous_content",
            "Call 2 must start with one ```json opening fence",
            "call2",
        )
    )
    raise Call2FramingError(findings)


def _read_fenced_block(
    lines: list[bytes],
    index: int,
    language: str,
    findings: list[Finding],
) -> tuple[list[bytes], int | None]:
    """Collect body lines from ``index`` up to the next closing fence.

    Returns the body and the closing fence index, or ``None`` when the block
    is not closed. A nested opening fence of the same language is recorded as
    a duplicate block and kept in the body.
    """

    block_lines: list[bytes] = []
    while index < len(lines):
        line = lines[index]
        if _is_closing_fence(line):
            return block_lines, index
        if _is_fence_line(line, language, opening=True):
            findings.append(_duplicate_block_finding(language))
        block_lines.append(line)
        index += 1
    return block_lines, None


def _python_block_start(lines: list[bytes], index: int, findings: list[Finding]) -> int:
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        findings.append(
            Finding("missing_python_block", "Call 2 must contain one Python block", "call2")
        )
        raise Call2FramingError(findings)
    if _is_fence_line(lines[index], "json", opening=True):
        findings.append(_duplicate_block_finding("json"))
        raise Call2FramingError(findings)
    if not _is_fence_line(lines[index], "python", opening=True):
        findings.append(
            Finding(
                "ambiguous_content",
                "Call 2 must place exactly one ```python block after JSON",
                "call2",
            )
        )
        raise Call2FramingError(findings)
    return index + 1


def _require_no_trailing_content(lines: list[bytes], index: int, findings: list[Finding]) -> None:
    if index == len(lines):
        return
    for language in ("python", "json"):
        if any(_is_fence_line(line, language, opening=True) for line in lines[index:]):
            findings.append(_duplicate_block_finding(language))
    findings.append(
        Finding(
            "closing_fence_in_python",
            "a closing fence line terminates Python before the response ends",
            "call2.python",
        )
    )
    findings.append(
        Finding("extra_content", "Call 2 contains content outside its two blocks", "call2")
    )
    raise Call2FramingError(findings)


def _decode_call2_metadata(metadata_bytes: bytes, findings: list[Finding]) -> Any:
    try:
        return json.loads(metadata_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        findings.append(Finding("invalid_metadata_json", str(exc), "call2.json"))
        raise Call2FramingError(findings) from exc


def _parse_call2_python(python_bytes: bytes, findings: list[Finding]) -> ast.Module:
    try:
        python_source = python_bytes.decode("utf-8")
        return ast.parse(python_source)
    except (UnicodeDecodeError, SyntaxError) as exc:
        findings.append(Finding("invalid_python", str(exc), "call2.python"))
        raise Call2FramingError(findings) from exc


def _evaluate_signature_findings(tree: ast.Module) -> list[Finding]:
    evaluate = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "evaluate"
        ),
        None,
    )
    if evaluate is None:
        return [
            Finding(
                "missing_function",
                "Python block must define evaluate(evidence)",
                "call2.python",
            )
        ]
    if len(evaluate.args.args) != 1 or evaluate.args.args[0].arg != "evidence":
        return [
            Finding(
                "function_signature",
                "evaluate must accept exactly one evidence argument",
                "call2.python.evaluate",
            )
        ]
    return []


def _is_fence_line(line: bytes, language: str, *, opening: bool) -> bool:
    if not opening:
        return _is_closing_fence(line)
    return line in {f"```{language}\n".encode(), f"```{language}\r\n".encode()}


def _is_closing_fence(line: bytes) -> bool:
    return line in {b"```\n", b"```\r\n", b"```"}


def _validate_call2_metadata_shape(value: Any) -> list[Finding]:
    if not isinstance(value, dict):
        return [
            Finding("metadata_type_error", "Call 2 JSON block must be an object", "call2.json")
        ]
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
                f"call2.json.{field_name}",
            )
        )
    for field_name in sorted(_CALL2_METADATA_FIELDS - set(value)):
        findings.append(
            Finding(
                "missing_field",
                f"Call 2 metadata missing field: {field_name}",
                f"call2.json.{field_name}",
            )
        )
    return findings


def _call2_stimulus_findings(stimulus: Any) -> list[Finding]:
    if not isinstance(stimulus, dict):
        return [Finding("type_error", "stimulus must be an object", "stimulus")]
    findings: list[Finding] = []
    required = {"user_text", "history", "slots", "delivery"}
    for field_name in sorted(required - set(stimulus)):
        findings.append(
            Finding(
                "missing_field",
                f"stimulus missing field: {field_name}",
                f"stimulus.{field_name}",
            )
        )
    for field_name in sorted(set(stimulus) - required):
        findings.append(
            Finding(
                "unexpected_field",
                f"unexpected stimulus field: {field_name}",
                f"stimulus.{field_name}",
            )
        )
    if not isinstance(stimulus.get("user_text"), str):
        findings.append(
            Finding("type_error", "stimulus.user_text must be a string", "stimulus.user_text")
        )
    if not isinstance(stimulus.get("delivery"), str):
        findings.append(
            Finding("type_error", "stimulus.delivery must be a string", "stimulus.delivery")
        )
    if not isinstance(stimulus.get("history"), list):
        findings.append(
            Finding("type_error", "stimulus.history must be a list", "stimulus.history")
        )
    if not _is_string_list(stimulus.get("slots")):
        findings.append(
            Finding("type_error", "stimulus.slots must be a list of strings", "stimulus.slots")
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
    findings: list[Finding] = []
    for field_name in sorted(set(spec) - {"question", "criteria", "fact_refs"}):
        findings.append(
            Finding(
                "unexpected_field",
                f"unexpected semantic_judge_spec field: {field_name}",
                f"semantic_judge_spec.{field_name}",
            )
        )
    for field_name in ("question", "criteria", "fact_refs"):
        if field_name not in spec:
            findings.append(
                Finding(
                    "missing_field",
                    f"semantic_judge_spec missing field: {field_name}",
                    f"semantic_judge_spec.{field_name}",
                )
            )
    for field_name in ("question", "criteria"):
        if field_name in spec and not isinstance(spec.get(field_name), str):
            findings.append(
                Finding(
                    "type_error",
                    f"semantic_judge_spec.{field_name} must be a string",
                    f"semantic_judge_spec.{field_name}",
                )
            )
    if "fact_refs" in spec and not _is_string_list(spec.get("fact_refs")):
        findings.append(
            Finding(
                "type_error",
                "semantic_judge_spec.fact_refs must be a list of strings",
                "semantic_judge_spec.fact_refs",
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
    extra = sorted(set(item) - {"label", "description"})
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
    the shared field validator. Prerequisites are then checked against the
    closed v2 form, followed by the omission-trigger, stimulus-slot, and
    established-trigger cross-checks.

    ``provenance_ids`` are scenario lineage or attack-tree node IDs from the
    supplied handoff. They are valid in ``interpretation.source_refs`` and
    ``assumptions[].ref``; every other reference field stays limited to
    supplied inventory references. ``condition`` is the handoff's
    discriminating condition; its not_called comparisons add the omission
    trigger check.
    """

    findings = _collect_plan_findings_with_contract(
        plan,
        inventory,
        runtime_contract,
        _call1_contract_v2(),
        provenance_ids=provenance_ids,
        transformations=transformations,
    )
    if isinstance(plan, dict):
        findings.extend(_omission_trigger_findings(plan, inventory, condition))
        findings.extend(_plan_stimulus_slot_findings(plan, inventory))
        findings.extend(_established_trigger_findings(plan, inventory))
    return findings


def _established_trigger_findings(
    plan: dict[str, Any], inventory: dict[str, Any]
) -> list[Finding]:
    """Require each established_trigger item to cite a supplied result observation."""

    selected = plan.get("selected_evidence")
    facts = inventory.get("facts")
    observations = {
        item["ref"]: item["provenance"]["tool_name"]
        for item in (facts if isinstance(facts, list) else [])
        if isinstance(item, dict)
        and isinstance(item.get("ref"), str)
        and "value" in item
        and isinstance(item.get("provenance"), dict)
        and isinstance(item["provenance"].get("tool_name"), str)
    }
    findings: list[Finding] = []
    for index, item in enumerate(selected if isinstance(selected, list) else []):
        if not isinstance(item, dict) or item.get("role") != ESTABLISHED_TRIGGER_ROLE:
            continue
        ref = item.get("ref")
        if isinstance(ref, str) and ref in observations:
            continue
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
        findings.append(
            Finding(
                "established_trigger_not_observation",
                (
                    f"selected_evidence[{index}] has role {ESTABLISHED_TRIGGER_ROLE!r} but its "
                    f"ref {ref!r} is not a supplied result observation. An established "
                    "trigger cites the observation that already shows the triggering result "
                    f"before the run{options}"
                ),
                f"selected_evidence[{index}].role",
            )
        )
    return findings


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
                    "session prerequisites and detector-only values do not belong in "
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

    Detector controls replay the cited observation as the trigger call; without
    one, the controls that check the missing call cannot run.
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
    response: ParsedCall2Response | dict[str, Any],
    plan: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    transformations: list[dict[str, Any]] | None = None,
) -> list[Finding]:
    """Validate v2 metadata and normalize its plan-owned context."""

    if isinstance(response, ParsedCall2Response):
        findings = _validate_call2_metadata_shape(response.metadata)
        metadata = response.metadata
    else:
        findings = _validate_call2_metadata_shape(response)
        metadata = response
    if findings:
        return findings
    if not isinstance(metadata, dict):
        return findings
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
    findings.extend(_semantic_judge_question_findings(metadata.get("semantic_judge_spec")))
    stimulus = metadata.get("stimulus")
    if isinstance(stimulus, dict):
        findings.extend(
            _artifact_stimulus_findings(
                stimulus,
                plan,
                inventory,
                runtime_contract,
                runtime_bindings,
                transformations=transformations,
            )
        )
    judge_spec = metadata.get("semantic_judge_spec")
    findings.extend(_semantic_judge_decision_findings(plan, judge_spec))
    if isinstance(judge_spec, dict):
        findings.extend(_semantic_judge_fact_ref_findings(judge_spec, inventory))
    if isinstance(plan, dict) and isinstance(plan.get("prerequisites"), list):
        declared_bindings = _declared_binding_names(plan.get("runtime_bindings"))
        findings.extend(
            _collect_canonical_prerequisite_findings(
                plan["prerequisites"],
                _inventory_references(inventory),
                declared_bindings,
                runtime_bindings,
                safe_behavior=_plan_safe_behavior(plan),
                transformations=transformations,
            )
        )
    return findings


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
    *,
    transformations: list[dict[str, Any]] | None,
) -> list[Finding]:
    findings = _stimulus_delivery_findings(stimulus.get("delivery"), plan, runtime_contract)
    findings.extend(_stimulus_history_findings(stimulus.get("history")))
    user_text = stimulus.get("user_text")
    findings.extend(
        _stimulus_slot_findings(stimulus, runtime_bindings, transformations=transformations)
    )
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


def _stimulus_slot_findings(
    stimulus: dict[str, Any],
    runtime_bindings: Any,
    *,
    transformations: list[dict[str, Any]] | None,
) -> list[Finding]:
    slots = stimulus.get("slots")
    user_text = stimulus.get("user_text")
    if not (isinstance(slots, list) and isinstance(user_text, str)):
        return []
    findings: list[Finding] = []
    rendered_slots = _slot_names_in_order(user_text)
    declared = _declared_binding_names(runtime_bindings)
    undeclared = [slot for slot in rendered_slots if slot not in declared]
    if not undeclared:
        _normalize_stimulus_slots(
            stimulus,
            declared,
            transformations=transformations,
        )
        slots = stimulus.get("slots")
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
                "user text. Session prerequisites and detector inputs do not "
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


def _collect_plan_findings_with_contract(
    plan: Any,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    contract: dict[str, Any],
    *,
    provenance_ids: Collection[str] = frozenset(),
    transformations: list[dict[str, Any]] | None = None,
) -> list[Finding]:
    """Run the existing validator with a version-specific root contract."""

    if not isinstance(plan, dict):
        return [Finding("response_type_error", "plan must be an object", "response")]
    required = contract["schema"]["required"]
    findings: list[Finding] = []
    for field_name in sorted(set(plan) - set(required)):
        findings.append(
            Finding("unexpected_field", f"unexpected plan field: {field_name}", field_name)
        )
    for field_name in required:
        if field_name not in plan:
            findings.append(
                Finding("plan_validation", f"missing plan field: {field_name}", field_name)
            )
    assumptions = plan.get("assumptions")
    if not isinstance(assumptions, list):
        if "assumptions" in plan:
            findings.append(Finding("type_error", "assumptions must be a list", "assumptions"))
    else:
        references = _inventory_references(inventory)
        for index, assumption in enumerate(assumptions):
            path = f"assumptions[{index}]"
            if not isinstance(assumption, dict):
                findings.append(Finding("shape_error", "assumption must be an object", path))
                continue
            for field_name in sorted(set(assumption) - {"ref", "reason"}):
                findings.append(
                    Finding(
                        "unexpected_field",
                        f"unexpected assumption field: {field_name}",
                        f"{path}.{field_name}",
                    )
                )
            if not isinstance(assumption.get("ref"), str):
                findings.append(
                    Finding("type_error", "assumption.ref must be a string", f"{path}.ref")
                )
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
    required_observations = plan.get("required_observations")
    if not isinstance(required_observations, dict):
        if "required_observations" in plan:
            findings.append(
                Finding(
                    "type_error",
                    "required_observations must be an object",
                    "required_observations",
                )
            )
    # Validate all legacy plan fields after the v2 root additions.  Removing
    # only the additions keeps the old nested validators and their findings.
    legacy_plan = dict(plan)
    legacy_plan.pop("assumptions", None)
    legacy_plan.pop("required_observations", None)
    findings.extend(
        collect_plan_findings(
            legacy_plan,
            inventory,
            runtime_contract,
            provenance_ids=provenance_ids,
            transformations=transformations,
        )
    )
    findings = [
        finding
        for finding in findings
        if not (
            finding.path
            in {
                "interpretation",
                "selected_evidence",
                "setup_recipe",
                "runtime_bindings",
                "prerequisites",
                "stimulus_approach",
                "observation_claim",
                "semantic_judge",
                "unresolved_requirements",
            }
            and finding.code == "missing_field"
        )
    ]
    unresolved = plan.get("unresolved_requirements")
    if isinstance(unresolved, list):
        for index, item in enumerate(unresolved):
            if (
                isinstance(item, dict)
                and item.get("essential") is True
                and item.get("obtainable_via_setup") is False
                and item.get("source_kind") == "setup_output"
            ):
                findings.append(
                    Finding(
                        "unobtainable_essential_requirement",
                        (
                            "an essential requirement that cannot be obtained blocks "
                            "the plan; a requirement that is not needed for the "
                            "experiment is not essential"
                        ),
                        f"unresolved_requirements[{index}]",
                    )
                )
    declared_bindings = _declared_binding_names(plan.get("runtime_bindings"))
    prerequisites = plan.get("prerequisites")
    if isinstance(prerequisites, list):
        findings.extend(
            _collect_canonical_prerequisite_findings(
                prerequisites,
                _inventory_references(inventory),
                declared_bindings,
                plan.get("runtime_bindings"),
                safe_behavior=_plan_safe_behavior(plan),
                transformations=transformations,
            )
        )
    return findings


def collect_plan_findings(
    plan: Any,
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    provenance_ids: Collection[str] = frozenset(),
    transformations: list[dict[str, Any]] | None = None,
) -> list[Finding]:
    """Return every structural Call 1 finding and normalize valid bindings."""

    findings: list[Finding] = []
    if not isinstance(plan, dict):
        return [Finding("response_type_error", "plan must be an object", "response")]

    findings.extend(_plan_root_field_findings(plan))
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
                transformations=transformations,
            )
        )
    findings.extend(_stimulus_approach_findings(plan, runtime_contract))
    findings.extend(_observation_claim_findings(plan, runtime_contract))
    findings.extend(_semantic_judge_plan_findings(plan))

    prerequisites = plan.get("prerequisites")
    if isinstance(prerequisites, list):
        _normalize_prerequisite_binding_consumers(
            prerequisites,
            runtime_bindings,
            transformations=transformations,
        )
        findings.extend(_collect_prerequisite_findings(prerequisites, references))
    unresolved = plan.get("unresolved_requirements")
    if isinstance(unresolved, list):
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


_PLAN_LIST_FIELDS = (
    "selected_evidence",
    "setup_recipe",
    "runtime_bindings",
    "prerequisites",
    "unresolved_requirements",
)


def _plan_root_field_findings(plan: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    required = _call1_contract_v1()["schema"]["required"]
    allowed = set(required)
    for field_name in sorted(set(plan) - allowed):
        findings.append(
            Finding(
                "unexpected_field",
                f"unexpected plan field: {field_name}",
                field_name,
            )
        )
    for field_name in required:
        if field_name not in plan:
            findings.append(
                Finding("plan_validation", f"missing plan field: {field_name}", field_name)
            )
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
    if claim_level not in _claim_levels():
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


def _semantic_judge_plan_findings(plan: dict[str, Any]) -> list[Finding]:
    judge = plan.get("semantic_judge")
    if not isinstance(judge, dict):
        if "semantic_judge" in plan:
            return [Finding("type_error", "semantic_judge must be an object", "semantic_judge")]
        return []
    findings: list[Finding] = []
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
    transformations: list[dict[str, Any]] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    normalized = normalize_binding_declarations(
        declarations,
        inventory=inventory,
        transformations=transformations,
    )
    names: dict[str, int] = {}
    for index, raw in enumerate(normalized):
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

    required = {
        "name",
        "expected_type",
        "source_kind",
        "source_ref",
        "selector",
        "consumers",
        "on_missing",
    }
    findings: list[Finding] = []
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

    string_fields = (
        "name",
        "expected_type",
        "source_kind",
        "source_ref",
        "selector",
        "on_missing",
    )
    for field_name in string_fields:
        if field_name in raw and not isinstance(raw[field_name], str):
            findings.append(
                Finding(
                    finding_code,
                    f"binding {field_name} must be a string",
                    f"{path}.{field_name}",
                )
            )

    name = raw.get("name")
    if isinstance(name, str) and not name.strip():
        findings.append(Finding(finding_code, "binding name is blank", f"{path}.name"))

    expected_type = raw.get("expected_type")
    if isinstance(expected_type, str) and expected_type not in CLOSED_TYPES:
        findings.append(
            Finding(
                finding_code,
                f"binding expected_type is not closed: {name}",
                f"{path}.expected_type",
            )
        )

    source_kind = raw.get("source_kind")
    if isinstance(source_kind, str) and source_kind not in SOURCE_KINDS:
        findings.append(
            Finding(
                finding_code,
                f"binding source_kind is not closed: {name}",
                f"{path}.source_kind",
            )
        )

    source_ref = raw.get("source_ref")
    selector = raw.get("selector")
    source_schema: dict[str, Any] | None = None
    canonical_source_ref = source_ref
    canonical_selector = selector
    if isinstance(source_kind, str) and isinstance(source_ref, str) and isinstance(selector, str):
        canonical_source_ref, canonical_selector = canonical_binding_paths(
            source_kind,
            source_ref,
            selector,
            inventory,
        )
        if (canonical_source_ref, canonical_selector) != (source_ref, selector):
            raw["source_ref"] = canonical_source_ref
            raw["selector"] = canonical_selector
            source_ref = canonical_source_ref
            selector = canonical_selector
    if isinstance(source_ref, str):
        if not source_ref.strip():
            findings.append(
                Finding(
                    finding_code,
                    f"binding source reference is blank: {name}",
                    f"{path}.source_ref",
                )
            )
        elif source_kind in SOURCE_KINDS:
            source_schema, source_error = _binding_source_schema(
                source_kind,
                canonical_source_ref,
                inventory,
                runtime_contract,
                name,
            )
            if source_error:
                findings.append(Finding(finding_code, source_error, f"{path}.source_ref"))

    on_missing = raw.get("on_missing")
    if isinstance(on_missing, str) and on_missing not in MISSING_POLICIES:
        findings.append(
            Finding(
                finding_code,
                f"binding on_missing is not closed: {name}",
                f"{path}.on_missing",
            )
        )

    consumers = raw.get("consumers")
    if not isinstance(consumers, list):
        findings.append(
            Finding(
                finding_code,
                "binding consumers must be a list",
                f"{path}.consumers",
            )
        )
    elif not consumers:
        findings.append(
            Finding(
                finding_code,
                "binding consumers must be non-empty strings",
                f"{path}.consumers",
            )
        )
    else:
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
            elif not _is_closed_consumer(consumer):
                findings.append(
                    Finding(
                        finding_code,
                        "binding consumer is not a closed path",
                        consumer_path,
                    )
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
    elif not selector.strip():
        findings.append(
            Finding(
                finding_code,
                f"binding selector is blank: {name}",
                f"{path}.selector",
            )
        )
    elif _selector_root(selector) is None:
        findings.append(
            Finding(
                finding_code,
                (
                    "selector must be an exact documented dot path rooted at value "
                    "for supplied_input or result for setup_output"
                ),
                f"{path}.selector",
            )
        )
    elif source_schema is not None:
        actual_type = _binding_selector_type(source_schema, canonical_selector)
        if actual_type is None:
            findings.append(
                Finding(
                    finding_code,
                    f"undocumented selector for binding {name}: {selector}",
                    f"{path}.selector",
                )
            )
        elif (
            isinstance(expected_type, str)
            and expected_type in CLOSED_TYPES
            and not _binding_types_compatible(actual_type, expected_type)
        ):
            findings.append(
                Finding(
                    finding_code,
                    (
                        f"binding type mismatch for {name}: expected {expected_type}, "
                        f"source is {actual_type}"
                    ),
                    f"{path}.selector",
                )
            )
    return findings


def _is_closed_consumer(value: str) -> bool:
    return value in {"stimulus.user_text", "stimulus.history"} or value.startswith(
        ("detector.", "prerequisites.", "setup.arguments.")
    )


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
        schema = operation.get("result_schema")
    else:
        fact = next(
            (
                item
                for item in inventory.get("facts", [])
                if isinstance(item, dict) and item.get("ref") == reference
            ),
            None,
        )
        if fact is None:
            return None, f"unknown supplied fact: {reference}"
        schema = fact.get("schema")
    if not isinstance(schema, dict):
        return None, f"missing source schema for binding: {name}"
    return schema, None


def _selector_root(selector: str) -> str | None:
    root = selector.split(".", 1)[0]
    return root if root in {"result", "value"} else None


def _binding_selector_type(schema: dict[str, Any], selector: str) -> str | None:
    current: Any = schema
    parts = selector.split(".")
    if not parts or any(not part for part in parts):
        return None
    for part in parts[1:]:
        if not isinstance(current, dict):
            return None
        if current.get("type") == "object":
            properties = current.get("properties")
            if not isinstance(properties, dict) or part not in properties:
                return None
            current = properties[part]
        elif current.get("type") == "array" and part == "items":
            current = current.get("items")
        else:
            return None
    return current.get("type") if isinstance(current, dict) else None


def _binding_types_compatible(actual: str, expected: str) -> bool:
    return actual == expected or (actual == "integer" and expected == "number")


def _collect_prerequisite_findings(
    prerequisites: list[Any],
    references: set[str],
) -> list[Finding]:
    findings: list[Finding] = []
    for index, prerequisite in enumerate(prerequisites):
        path = f"prerequisites[{index}]"
        if not isinstance(prerequisite, dict):
            findings.append(Finding("shape_error", "prerequisite must be an object", path))
            continue
        missing = {"name"} - set(prerequisite)
        for field_name in sorted(missing):
            findings.append(
                Finding(
                    "missing_field",
                    f"prerequisite missing field: {field_name}",
                    f"{path}.{field_name}",
                )
            )
        allowed_fields = {
            "name",
            "evidence_refs",
            "check",
            "source",
            "binding",
            "equals",
            "expected",
        }
        for field_name in sorted(set(prerequisite) - allowed_fields):
            findings.append(
                Finding(
                    "unexpected_field",
                    f"unexpected prerequisite field: {field_name}",
                    f"{path}.{field_name}",
                )
            )
        if (
            not isinstance(prerequisite.get("name"), str)
            or not prerequisite.get("name", "").strip()
        ):
            findings.append(
                Finding("type_error", "prerequisite.name must be a string", f"{path}.name")
            )
        if "check" in prerequisite and not isinstance(prerequisite.get("check"), str):
            findings.append(
                Finding("type_error", "prerequisite.check must be a string", f"{path}.check")
            )
        if "source" in prerequisite and (
            not isinstance(prerequisite["source"], str) or not prerequisite["source"].strip()
        ):
            findings.append(
                Finding(
                    "type_error",
                    "prerequisite.source must be a non-empty string",
                    f"{path}.source",
                )
            )
        if "binding" in prerequisite and (
            not isinstance(prerequisite["binding"], str) or not prerequisite["binding"].strip()
        ):
            findings.append(
                Finding(
                    "type_error",
                    "prerequisite.binding must be a non-empty string",
                    f"{path}.binding",
                )
            )
        for field_name in ("equals", "expected"):
            if field_name in prerequisite and not _is_json_value(prerequisite[field_name]):
                findings.append(
                    Finding(
                        "type_error",
                        f"prerequisite.{field_name} must be a JSON value",
                        f"{path}.{field_name}",
                    )
                )
        evidence_refs = prerequisite.get("evidence_refs", [])
        if not isinstance(evidence_refs, list):
            findings.append(
                Finding(
                    "type_error",
                    "prerequisite evidence_refs must be a list",
                    f"{path}.evidence_refs",
                )
            )
            continue
        for ref_index, ref in enumerate(evidence_refs):
            if not isinstance(ref, str) or not ref.strip() or ref not in references:
                findings.append(
                    Finding(
                        "unknown_reference",
                        f"unknown_reference: {ref}",
                        f"{path}.evidence_refs[{ref_index}]",
                    )
                )
    return findings


def _collect_canonical_prerequisite_findings(
    prerequisites: list[Any],
    references: set[str],
    declared_bindings: set[str],
    runtime_bindings: Any,
    *,
    safe_behavior: str | None = None,
    transformations: list[dict[str, Any]] | None = None,
) -> list[Finding]:
    """Validate the closed prerequisite form used by the v2 plan wire."""

    _normalize_prerequisite_binding_consumers(
        prerequisites,
        runtime_bindings,
        transformations=transformations,
    )
    findings: list[Finding] = []
    allowed_fields = {"name", "check", "evidence_refs", "binding", "equals"}
    for index, prerequisite in enumerate(prerequisites):
        path = f"prerequisites[{index}]"
        if not isinstance(prerequisite, dict):
            findings.append(Finding("shape_error", "prerequisite must be an object", path))
            continue
        for field_name in sorted(set(prerequisite) - allowed_fields):
            findings.append(
                Finding(
                    "unexpected_field",
                    f"unexpected prerequisite field: {field_name}",
                    f"{path}.{field_name}",
                )
            )
        for field_name in sorted(allowed_fields - set(prerequisite)):
            findings.append(
                Finding(
                    "missing_field",
                    f"prerequisite missing field: {field_name}",
                    f"{path}.{field_name}",
                )
            )
        if (
            not isinstance(prerequisite.get("name"), str)
            or not prerequisite.get("name", "").strip()
        ):
            findings.append(
                Finding("type_error", "prerequisite.name must be a string", f"{path}.name")
            )
        if "check" in prerequisite and not isinstance(prerequisite.get("check"), str):
            findings.append(
                Finding("type_error", "prerequisite.check must be a string", f"{path}.check")
            )
        elif _same_authored_text(prerequisite.get("check"), safe_behavior):
            findings.append(
                Finding(
                    "desired_behavior_prerequisite",
                    (
                        "intended safe behavior is a detector criterion, "
                        "not a starting-state prerequisite"
                    ),
                    f"{path}.check",
                )
            )
        binding = prerequisite.get("binding")
        if not isinstance(binding, str) or not binding.strip():
            if "binding" in prerequisite:
                findings.append(
                    Finding(
                        "type_error",
                        "prerequisite.binding must be a non-empty binding name",
                        f"{path}.binding",
                    )
                )
        elif binding not in declared_bindings:
            findings.append(
                Finding(
                    "unknown_binding",
                    (
                        f"prerequisite binding is not declared: {binding}; "
                        "bare evidence IDs and bindings.<name> selectors are not executable"
                    ),
                    f"{path}.binding",
                )
            )
        if "equals" in prerequisite and not _is_json_value(prerequisite["equals"]):
            findings.append(
                Finding(
                    "type_error",
                    "prerequisite.equals must be a JSON value",
                    f"{path}.equals",
                )
            )
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
        evidence_refs = prerequisite.get("evidence_refs")
        if isinstance(evidence_refs, list):
            for ref_index, ref in enumerate(evidence_refs):
                if not isinstance(ref, str) or not ref.strip() or ref not in references:
                    findings.append(
                        Finding(
                            "unknown_reference",
                            f"unknown_reference: {ref}",
                            f"{path}.evidence_refs[{ref_index}]",
                        )
                    )
        elif "evidence_refs" in prerequisite:
            findings.append(
                Finding(
                    "type_error",
                    "prerequisite evidence_refs must be a list",
                    f"{path}.evidence_refs",
                )
            )
        findings.extend(
            _validate_prerequisite_binding_consumer(
                prerequisite,
                index=index,
                runtime_bindings=runtime_bindings,
            )
        )
    return findings


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


def _normalize_prerequisite_binding_consumers(
    prerequisites: Any,
    runtime_bindings: Any,
    *,
    transformations: list[dict[str, Any]] | None = None,
) -> None:
    """Add the closed prerequisite consumer for each declared binding use."""

    if not isinstance(prerequisites, list) or not isinstance(runtime_bindings, list):
        return
    by_name = {
        item["name"]: item
        for item in runtime_bindings
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("consumers"), list)
        )
    }
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
        if not isinstance(step, dict) or not isinstance(step.get("operation"), str):
            raise PlanValidationError(f"setup_recipe[{index}] must name an operation")
        unexpected = set(step) - {"operation", "arguments"}
        if unexpected:
            raise PlanValidationError(
                f"setup_recipe[{index}] has unsupported fields: {sorted(unexpected)}"
            )
        if "arguments" not in step:
            raise PlanValidationError(f"setup_recipe[{index}] must include arguments")
        name = step["operation"]
        if name not in operations:
            raise PlanValidationError(f"unknown setup operation: {name}")
        if name not in permissions:
            raise PlanValidationError(f"setup operation is not permitted: {name}")
        supplied_args = step.get("arguments", {})
        if not isinstance(supplied_args, dict):
            raise PlanValidationError(f"setup_recipe[{index}].arguments must be an object")
        schema = operations[name].get("arguments", {})
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
