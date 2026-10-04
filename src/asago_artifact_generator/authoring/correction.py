"""Stage-local correction context and the rendered correction prompt."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..detector_controls import (
    DetectorControlFeedback,
    build_detector_feedback_prompt_context,
    describe_input_shapes,
)
from .binding_repair import (
    _binding_repair_options_for_correction,
    _reference_repair_options_for_correction,
)
from .checks import parse_call2_response
from .contracts import _call1_contract_v2, _call2_contract_v2, _render_evidence_packet_interface
from .core import (
    CORRECTION_PROMPT_VERSION_V25,
    CORRECTION_PROMPT_VERSION_V27,
    Call2FramingError,
    Finding,
    PromptPacket,
    _canonical_json,
)
from .prompt_context import (
    _OWNER_SCOPE_SECTION_TITLE,
    _context_has_not_called,
    _matching_runtime_observations,
    _plan_claim_level,
    _plan_semantic_judge_needed,
    _required_observation_keys,
    _scenario_design_prompt_view,
    _semantic_judge_fact_ref_guidance,
    artifact_observation_guide,
)
from .prompt_packets import _artifact_response_contract_for_prompt
from .response_decode import _readable_response


@dataclass
class _CorrectionView:
    """Values the correction packet sections read, derived once per packet."""

    context: dict[str, Any]
    artifact: bool
    original_context: dict[str, Any]
    fact_ref_guidance: dict[str, Any] | None
    plan_field_meanings: Any
    neutral_outcome_example: Any
    owner_scope: Any
    observation_guide: Any
    accepted_plan: Any
    binding_repair_options: dict[str, Any] | None = None
    reference_repair_options: dict[str, Any] | None = None


_CorrectionSection = Callable[[_CorrectionView], "tuple[str, Any] | None"]


def _correction_fact_ref_guidance(source_original_context: Any) -> dict[str, Any] | None:
    if not isinstance(source_original_context, dict):
        return None
    guidance = source_original_context.get("semantic_judge_fact_ref_guidance")
    if isinstance(guidance, dict):
        return deepcopy(guidance)
    authoritative = source_original_context.get("authoritative_context")
    facts = authoritative.get("facts") if isinstance(authoritative, dict) else None
    if isinstance(facts, list):
        return _semantic_judge_fact_ref_guidance({"facts": facts})
    return None


def _correction_view(correction_context: dict[str, Any]) -> _CorrectionView:
    fact_ref_guidance = _correction_fact_ref_guidance(correction_context.get("original_context"))
    original_context = _correction_prompt_context(correction_context["original_context"])
    plan_field_meanings = original_context.pop("plan_field_meanings", None)
    neutral_outcome_example = original_context.pop("neutral_outcome_example", None)
    original_context.pop("semantic_judge_fact_ref_guidance", None)
    owner_scope = original_context.pop("owner_scope", None)
    observation_guide = original_context.pop(
        "observation_guide", correction_context.get("observation_guide")
    )
    accepted_plan = correction_context.get("original_context", {}).get("accepted_plan")
    runtime_evidence_interface = correction_context.get("original_context", {}).get(
        "runtime_evidence_interface"
    )
    runtime_contract = (
        runtime_evidence_interface.get("runtime_contract")
        if isinstance(runtime_evidence_interface, dict)
        else None
    )
    artifact = correction_context.get("stage") == "artifact"
    if artifact and isinstance(runtime_contract, dict):
        observation = runtime_contract.get("observation")
        if isinstance(observation, dict):
            required_keys = _required_observation_keys(accepted_plan)
            runtime_observation = _matching_runtime_observations(
                required_keys,
                observation,
            )
            original_context["runtime_contract"] = {
                "observation": runtime_observation,
            }
    if artifact and not isinstance(observation_guide, dict):
        observation_guide = artifact_observation_guide(
            accepted_plan if isinstance(accepted_plan, dict) else {},
            runtime_contract if isinstance(runtime_contract, dict) else None,
            omission=_context_has_not_called(correction_context.get("original_context")),
        )
    return _CorrectionView(
        context=correction_context,
        artifact=artifact,
        original_context=original_context,
        fact_ref_guidance=fact_ref_guidance,
        plan_field_meanings=plan_field_meanings,
        neutral_outcome_example=neutral_outcome_example,
        owner_scope=owner_scope,
        observation_guide=observation_guide,
        accepted_plan=accepted_plan,
    )


def _failed_stage_section(view: _CorrectionView) -> tuple[str, Any] | None:
    return (
        "FAILED STAGE",
        {
            "stage": view.context["stage"],
            "failed_stage": view.context["failed_stage"],
        },
    )


def _fixed_plan_decision_section(view: _CorrectionView) -> tuple[str, Any] | None:
    plan = view.accepted_plan
    if not (
        isinstance(plan, dict)
        and isinstance(plan.get("semantic_judge"), dict)
        and plan["semantic_judge"].get("needed") is False
    ):
        return None
    return (
        "FIXED PLAN DECISION",
        "The accepted plan requires no semantic judge. semantic_judge_spec must be null. "
        "This decision is fixed; correct the detector within it.",
    )


def _original_stage_context_section(view: _CorrectionView) -> tuple[str, Any] | None:
    return ("ORIGINAL STAGE CONTEXT", view.original_context)


def _owner_scope_section(view: _CorrectionView) -> tuple[str, Any] | None:
    if view.owner_scope is None:
        return None
    return (_OWNER_SCOPE_SECTION_TITLE, view.owner_scope)


def _supplied_stage_context_section(view: _CorrectionView) -> tuple[str, Any] | None:
    supplied_stage_context = view.context.get("supplied_stage_context")
    if supplied_stage_context is None:
        return None
    return ("SUPPLIED STAGE CONTEXT", supplied_stage_context)


def _observation_guide_section(view: _CorrectionView) -> tuple[str, Any] | None:
    if not isinstance(view.observation_guide, dict):
        return None
    return ("OBSERVATION DECISION GUIDE", view.observation_guide)


def _plan_field_meanings_section(view: _CorrectionView) -> tuple[str, Any] | None:
    if not isinstance(view.plan_field_meanings, str):
        return None
    if view.artifact and view.context.get("detector_feedback") is not None:
        return None
    return ("PLAN FIELD MEANINGS", view.plan_field_meanings)


def _neutral_outcome_example_section(view: _CorrectionView) -> tuple[str, Any] | None:
    if not isinstance(view.neutral_outcome_example, str):
        return None
    return ("NEUTRAL OUTCOME EXAMPLE", view.neutral_outcome_example)


def _fact_ref_guidance_section(view: _CorrectionView) -> tuple[str, Any] | None:
    unknown_fact_ref_findings = [
        finding
        for finding in view.context.get("findings", [])
        if isinstance(finding, dict)
        and finding.get("code") == "unknown_reference"
        and str(finding.get("path", "")).startswith("semantic_judge_spec.fact_refs[")
    ]
    if not (
        view.artifact and unknown_fact_ref_findings and isinstance(view.fact_ref_guidance, dict)
    ):
        return None
    return (
        "SEMANTIC JUDGE FACT REFERENCE GUIDANCE",
        {
            **view.fact_ref_guidance,
            "triggered_findings": unknown_fact_ref_findings,
        },
    )


def _runtime_evidence_interface_section(view: _CorrectionView) -> tuple[str, Any] | None:
    if not view.artifact:
        return None
    accepted_plan = view.accepted_plan
    evidence_interface = view.context.get(
        "evidence_packet_interface",
        _render_evidence_packet_interface(
            claim_level=_plan_claim_level(accepted_plan),
            required_observations=(
                accepted_plan.get("required_observations")
                if isinstance(accepted_plan, dict)
                else None
            ),
            semantic_judge_needed=_plan_semantic_judge_needed(accepted_plan),
        ),
    )
    if view.context.get("detector_feedback") and isinstance(evidence_interface, str):
        # Exact control packets already demonstrate the input shape. Keep
        # the path/result contract, without a second unrelated input example.
        interface_view = json.loads(evidence_interface)
        interface_view.pop("full_example", None)
        interface_view.pop("full_example_label", None)
        evidence_interface = _canonical_json(interface_view)
    return ("RUNTIME EVIDENCE INTERFACE", evidence_interface)


def _response_contract_section(view: _CorrectionView) -> tuple[str, Any] | None:
    return (
        "RESPONSE CONTRACT",
        (
            _artifact_response_contract_for_prompt(
                view.context["response_contract"],
                correction=True,
            )
            if view.artifact
            else view.context["response_contract"]
        ),
    )


def _current_output_section(view: _CorrectionView) -> tuple[str, Any] | None:
    return (
        "CURRENT OUTPUT",
        _correction_current_output_view(view.context["current_output"], artifact=view.artifact),
    )


def _current_findings_section(view: _CorrectionView) -> tuple[str, Any] | None:
    return ("CURRENT FINDINGS", _correction_findings_view(view.context["findings"]))


def _reference_repair_options_section(view: _CorrectionView) -> tuple[str, Any] | None:
    if view.reference_repair_options is None:
        return None
    return ("REFERENCE REPAIR OPTIONS", view.reference_repair_options)


def _binding_repair_option_fields_section(view: _CorrectionView) -> tuple[str, Any] | None:
    options = view.binding_repair_options
    if options is None:
        return None
    return (
        "BINDING REPAIR OPTION FIELDS",
        {
            "description": options["description"],
            "field_descriptions": options["field_descriptions"],
        },
    )


def _binding_repair_options_section(view: _CorrectionView) -> tuple[str, Any] | None:
    if view.binding_repair_options is None:
        return None
    return ("BINDING REPAIR OPTIONS", {"options": view.binding_repair_options["options"]})


def _detector_control_feedback_section(view: _CorrectionView) -> tuple[str, Any] | None:
    if view.context.get("detector_feedback") is None:
        return None
    return (
        "DETECTOR CONTROL FEEDBACK",
        _correction_detector_feedback_view(view.context["detector_feedback"]),
    )


def _correction_instructions_section(view: _CorrectionView) -> tuple[str, Any] | None:
    correction_instruction = view.context["instruction"]
    if view.artifact:
        correction_instruction += " " + _CURRENT_ARTIFACT_CORRECTION_GUIDANCE
    return (
        "CORRECTION INSTRUCTIONS",
        {
            "instruction": correction_instruction,
            "format": view.context["format"],
            "accepted_plan_fixed": view.context["accepted_plan_fixed"],
        },
    )


def _prior_unresolved_findings_section(view: _CorrectionView) -> tuple[str, Any] | None:
    if "prior_unresolved_findings" not in view.context:
        return None
    return ("PRIOR UNRESOLVED FINDINGS", view.context["prior_unresolved_findings"])


# The packet's section order.  The binding repair options are computed after
# the context sections and the reference repair options after the output
# sections, so the three groups also fix the order of those computations.
_CORRECTION_CONTEXT_SECTIONS: tuple[_CorrectionSection, ...] = (
    _failed_stage_section,
    _fixed_plan_decision_section,
    _original_stage_context_section,
    _owner_scope_section,
    _supplied_stage_context_section,
    _observation_guide_section,
    _plan_field_meanings_section,
    _neutral_outcome_example_section,
    _fact_ref_guidance_section,
    _runtime_evidence_interface_section,
)
_CORRECTION_OUTPUT_SECTIONS: tuple[_CorrectionSection, ...] = (
    _response_contract_section,
    _current_output_section,
    _current_findings_section,
)
_CORRECTION_REPAIR_SECTIONS: tuple[_CorrectionSection, ...] = (
    _reference_repair_options_section,
    _binding_repair_option_fields_section,
    _binding_repair_options_section,
    _detector_control_feedback_section,
    _correction_instructions_section,
    _prior_unresolved_findings_section,
)


def _rendered_sections(
    view: _CorrectionView, renderers: tuple[_CorrectionSection, ...]
) -> list[tuple[str, Any]]:
    return [section for render in renderers if (section := render(view)) is not None]


def _render_correction_packet(correction_context: dict[str, Any]) -> PromptPacket:
    """Render one shared correction prompt for every artifact caller."""

    view = _correction_view(correction_context)
    sections = _rendered_sections(view, _CORRECTION_CONTEXT_SECTIONS)
    view.binding_repair_options = _binding_repair_options_for_correction(correction_context)
    sections += _rendered_sections(view, _CORRECTION_OUTPUT_SECTIONS)
    view.reference_repair_options = _reference_repair_options_for_correction(correction_context)
    sections += _rendered_sections(view, _CORRECTION_REPAIR_SECTIONS)
    payload = deepcopy(correction_context)
    if view.binding_repair_options is not None:
        payload["binding_repair_options"] = view.binding_repair_options
    if view.reference_repair_options is not None:
        payload["reference_repair_options"] = view.reference_repair_options
    packet = PromptPacket(
        stage="correction",
        version=(
            CORRECTION_PROMPT_VERSION_V25 if view.artifact else CORRECTION_PROMPT_VERSION_V27
        ),
        system=_CORRECTION_SYSTEM_V5,
        user=_render_correction_sections(tuple(sections)),
        payload=payload,
    )
    return packet


def _correction_detector_feedback_view(value: Any) -> Any:
    """Render exact failed control inputs/results without redundant wrappers.

    Each entry carries a code-derived ``input_shapes`` type summary of the
    failed control's inputs.
    """

    if not isinstance(value, dict):
        return value
    failed = value.get("failed_controls")
    if not isinstance(failed, list):
        return value
    rendered = [_failed_control_view(item) for item in failed if isinstance(item, dict)]
    passing = value.get("passing_controls")
    passing_rendered = [
        {"name": item.get("name"), "outcome": item.get("observed_outcome")}
        for item in (passing if isinstance(passing, list) else [])
        if isinstance(item, dict)
    ]
    return {
        "failed_controls": rendered,
        "passing_controls": passing_rendered,
        "correction_guidance": value.get("correction_guidance"),
    }


def _failed_control_view(item: dict[str, Any]) -> dict[str, Any]:
    """Render one failed control's input, expectation, actual return, and explanation."""

    actual = _failed_control_actual(item)
    evidence = item.get("evidence")
    entry: dict[str, Any] = {"name": item.get("name"), "input": evidence}
    entry["input_shapes"] = describe_input_shapes(evidence) if isinstance(evidence, dict) else {}
    entry.update(
        {
            "expected": {
                "outcome": item.get("expected_outcome"),
                "claim_level": item.get("expected_claim_level"),
            },
            "actual": actual,
            "explanation": _compact_feedback_explanation(item),
        }
    )
    return entry


def _failed_control_actual(item: dict[str, Any]) -> Any:
    """Return the control's actual result, or its error keyed by the failure class."""

    actual = item.get("actual_result")
    if actual is not None or not isinstance(item.get("error"), str):
        return actual
    outcome_class = item.get("outcome_class")
    error = item["error"]
    if "timeout" in error.casefold():
        return {"timeout": error}
    if outcome_class == "detector_exception":
        return {"exception": error}
    if outcome_class == "invalid_returned_result":
        return {"invalid_result": error}
    return {"pre_result_failure": error}


def _compact_feedback_explanation(item: dict[str, Any]) -> str:
    """Keep each feedback explanation explicit without repeating result fields."""

    outcome_class = item.get("outcome_class")
    error = item.get("error")
    actual_outcome = _feedback_actual_field(item, "actual_outcome", "outcome")
    expected_outcome = item.get("expected_outcome")
    if outcome_class == "structurally_valid_wrong_outcome":
        return _wrong_outcome_explanation(item, actual_outcome, expected_outcome)
    if isinstance(error, str) and "timeout" in error.casefold():
        return "detector timed out before returning a result"
    if outcome_class == "detector_exception":
        return "detector raised an exception before returning a result"
    if outcome_class == "invalid_returned_result":
        return _invalid_return_explanation(error, actual_outcome, expected_outcome)
    if outcome_class == "container/evaluator_failure_before_result":
        return "container or evaluator failed before exposing a result"
    runtime_explanation = item.get("runtime_contract_explanation")
    if isinstance(runtime_explanation, str) and runtime_explanation:
        return runtime_explanation
    return "returned outcome or claim level differs from the expected control result"


def _invalid_return_explanation(error: Any, actual_outcome: Any, expected_outcome: Any) -> str:
    verdict_note = (
        f" Its outcome {actual_outcome!r} also differs from expected {expected_outcome!r}."
        if actual_outcome is not None and actual_outcome != expected_outcome
        else ""
    )
    return f"Runtime rejected the unvalidated return: {error}." + verdict_note


def _feedback_actual_field(item: dict[str, Any], name: str, result_key: str) -> Any:
    """Return the control's top-level actual field, falling back to its returned result."""

    value = item.get(name)
    actual_result = item.get("actual_result")
    if not isinstance(value, str) and isinstance(actual_result, dict):
        return actual_result.get(result_key)
    return value


def _wrong_outcome_explanation(
    item: dict[str, Any], actual_outcome: Any, expected_outcome: Any
) -> str:
    """Name the first result field where a structurally valid return differs."""

    if actual_outcome != expected_outcome:
        return f"returned outcome {actual_outcome!r}; expected outcome {expected_outcome!r}"
    actual_claim_level = _feedback_actual_field(item, "actual_claim_level", "claim_level")
    expected_claim_level = item.get("expected_claim_level")
    if actual_claim_level != expected_claim_level:
        return (
            f"returned claim level {actual_claim_level!r}; "
            f"expected claim level {expected_claim_level!r}"
        )
    return "returned result differs from the expected control result"


def _correction_findings_view(value: Any) -> Any:
    """Avoid repeating full control details beside exact feedback packets."""

    if not isinstance(value, list):
        return value
    result: list[Any] = []
    for item in value:
        if not isinstance(item, dict):
            result.append(item)
            continue
        path = item.get("path", "")
        if isinstance(path, str) and path.startswith("detector_controls."):
            continue
        if item.get("code") == "semantic_review" and "details" in item:
            # The review location and required change already appear in detail.
            item = {key: value for key, value in item.items() if key != "details"}
        result.append(item)
    return result


def _correction_current_output_view(value: Any, *, artifact: bool) -> Any:
    """Canonicalize artifact framing while retaining exact metadata and source."""

    if not artifact or not isinstance(value, str):
        return value
    try:
        parsed = parse_call2_response(value.encode("utf-8"))
    except (Call2FramingError, UnicodeDecodeError, ValueError):
        return value
    return (
        "```json\n"
        + _canonical_json(parsed.metadata)
        + "\n```\n```python\n"
        + parsed.python_source
        + "```\n"
    )


def _correction_prompt_context(context: dict[str, Any]) -> dict[str, Any]:
    """Keep correction context authoritative without replaying authoring payloads."""

    result = deepcopy(context)
    if "scenario_design" in result:
        result["scenario_design"] = _scenario_design_prompt_view(result["scenario_design"])
    authoritative = result.get("authoritative_context")
    interface = result.get("runtime_evidence_interface")
    if isinstance(authoritative, dict) and isinstance(interface, dict):
        if isinstance(interface.get("runtime_contract"), dict):
            authoritative.pop("runtime_capabilities", None)
        interface.pop("evidence_packet", None)
        _scope_correction_authoritative_context(
            authoritative,
            result.get("accepted_plan"),
        )
    response_contract = result.get("response_contract")
    if isinstance(response_contract, dict):
        response_contract.pop("evidence_packet", None)
        response_contract.pop("neutral_example", None)
    result.pop("evidence_packet_interface", None)
    result.pop("runtime_evidence_interface", None)
    result.pop("response_contract", None)
    result.pop("neutral_example", None)
    return result


def _scope_correction_authoritative_context(
    authoritative: dict[str, Any],
    accepted_plan: Any,
) -> None:
    """Drop source records unrelated to the fixed correction plan."""

    if not isinstance(accepted_plan, dict):
        return
    plan_text = _canonical_json(accepted_plan)
    operations = authoritative.get("operations")
    if isinstance(operations, list):
        authoritative["operations"] = [
            value
            for value in operations
            if isinstance(value, dict)
            and isinstance(value.get("name"), str)
            and value["name"] in plan_text
        ]
    authoritative.pop("facts", None)
    authoritative.pop("source_handles", None)
    authoritative.pop("runtime_capabilities", None)
    operations = authoritative.get("operations")
    if isinstance(operations, list):
        authoritative["operations"] = [
            {
                "name": item.get("name"),
                "description": item.get("description"),
            }
            for item in operations
            if isinstance(item, dict)
        ]


_PLAN_CORRECTION_GUIDANCE = (
    "Evaluate every finding against the source context and PLAN FIELD MEANINGS. "
    "Preserve a correct distinction between evidence requirements and missing-"
    "evidence fallback. Do not make violation, absence, and inconclusive describe "
    "the same situation merely to satisfy a criticism that confuses alternative "
    "branches. Correct real schema, reference, or semantic errors and retain "
    "supported meaning elsewhere. If criticism is not substantiated, preserve the "
    "supported content; do not invent a defect or new field. Return only the complete "
    "replacement plan in the existing response format. Your response will still "
    "undergo the normal checks and review."
)
_ARTIFACT_CORRECTION_GUIDANCE = (
    "Keep the accepted plan fixed. Correct the implementation using the exact "
    "failed-control evidence and substantiated review findings. Do not solve a "
    "missing-evidence failure by assuming the missing value "
    "exists, removing the fallback, or changing the observation level. Use the "
    "plan-derived observation guide to distinguish requested capture inventory from "
    "the evidence needed for each outcome. Correct the supplied candidate against "
    "the fixed accepted "
    "plan and actual evidence interface. Address the listed plan conflicts and "
    "control failures together. Each control includes its exact input, expected "
    "outcome, actual return or exception, and explanation. Preserve working behavior "
    "beyond these examples. Return a complete replacement artifact, including "
    "metadata and Python, in the unchanged response format. Keep an accepted "
    "no-judge decision as null judge metadata; repair the candidate rather than "
    "changing the plan to fit it. Plan source handles and prerequisite citations are "
    "provenance, not runtime evidence paths. Setup binding source_ref and selector "
    "define downstream resolution; read the resolved value at "
    "evidence.bindings.<declared name>. Return evidence_refs that resolve only "
    "against the actual evidence object passed to evaluate. Use the accepted plan's "
    "runtime binding for execution identities; neutral examples and controls use "
    "substitute values."
)
_CURRENT_ARTIFACT_CORRECTION_GUIDANCE = (
    "If a deterministic finding reports an undeclared evidence "
    "root or binding, replace the read with a standard packet root or the declared "
    "evidence.bindings.<binding_name> path. Runtime bindings come from the accepted "
    "plan, and artifact authoring cannot add, rename, or change one, so a correction "
    "cannot declare a new binding. Never read evidence.state or hardcode a "
    "supplied record fact. If a binding lists stimulus.user_text, keep that consumer "
    "only when its exact resolved value occurs in the authored text or the text "
    "contains its {{binding_name}} slot; otherwise remove the consumer."
)
_CURRENT_JUDGE_CORRECTION_GUIDANCE = (
    " For a judge-enabled package, treat runner-normalized "
    "evidence.judge.verdict unresolved with evidence_refs [] as inconclusive. "
    "For supported or contradicted verdicts, cite only judge.evidence_refs as "
    "judge support. Each reference resolves to captured message content or a "
    "non-null tool-call result value; call metadata, arguments, and null results "
    "are unusable. Do not cite or repair judge references in detector code."
)


def build_correction_context(
    *,
    failed_stage: str,
    original_context: dict[str, Any],
    current_output: bytes | str,
    findings: list[dict[str, Any]] | tuple[dict[str, Any], ...] | list[Finding],
    prior_unresolved_findings: list[dict[str, Any]] | None = None,
    detector_feedback: list[DetectorControlFeedback]
    | tuple[DetectorControlFeedback, ...]
    | None = None,
) -> dict[str, Any]:
    """Build a stage-aware correction context without competing formats."""

    stage = _correction_stage(failed_stage)
    if isinstance(current_output, bytes):
        output_text, output_encoding = _readable_response(current_output)
    else:
        output_text, output_encoding = current_output, "text-input"
    normalized_findings = _normalized_correction_findings(findings, detector_feedback)
    instruction = _correction_instruction(failed_stage, findings)
    context: dict[str, Any] = {
        "stage": stage,
        "failed_stage": failed_stage,
        "original_context": deepcopy(original_context),
        "current_output": output_text,
        "current_output_encoding": output_encoding,
        "findings": normalized_findings,
        "instruction": instruction,
    }
    if stage == "artifact":
        context.update(_artifact_correction_interfaces(original_context))
    if detector_feedback:
        context["detector_feedback"] = build_detector_feedback_prompt_context(detector_feedback)
    if prior_unresolved_findings:
        context["prior_unresolved_findings"] = deepcopy(prior_unresolved_findings)
    if stage == "plan":
        context.update(
            {
                "accepted_plan_fixed": False,
                "format": (
                    "Return one complete plan replacement as one bare JSON object or "
                    "exactly one lowercase ```json fenced JSON object."
                ),
                "response_contract": _call1_contract_v2(),
            }
        )
        context["instruction"] = (
            instruction + " Call 1 uses one bare JSON object or exactly one lowercase ```json "
            "fenced JSON object. " + _PLAN_CORRECTION_GUIDANCE
        )
    elif stage == "artifact":
        context.update(
            {
                "accepted_plan_fixed": True,
                "format": (
                    "Return exactly one ```json metadata block followed by one raw "
                    "python block using the artifact response contract."
                ),
                "response_contract": _call2_contract_v2(
                    original_context.get("accepted_plan")
                    if isinstance(original_context, dict)
                    else None,
                ),
            }
        )
        context["instruction"] = (
            instruction + " Call 2 uses exactly one JSON metadata block followed by one raw "
            "Python block. " + _ARTIFACT_CORRECTION_GUIDANCE + _CURRENT_JUDGE_CORRECTION_GUIDANCE
        )
    else:
        raise ValueError(f"unsupported correction stage: {failed_stage}")
    return context


def _correction_stage(failed_stage: str) -> str:
    """Map a failed stage name to the plan or artifact correction it belongs to."""

    if failed_stage in {"plan", "call1", "plan_review"}:
        return "plan"
    if failed_stage in {"artifact", "call2", "artifact_review"}:
        return "artifact"
    return failed_stage


def _normalized_correction_findings(
    findings: list[dict[str, Any]] | tuple[dict[str, Any], ...] | list[Finding],
    detector_feedback: Any,
) -> list[dict[str, Any]]:
    """Copy findings as dicts; with control feedback, summarize control findings last."""

    normalized_findings = [
        finding.to_dict() if isinstance(finding, Finding) else deepcopy(finding)
        for finding in findings
    ]
    if not detector_feedback:
        return normalized_findings

    def is_control(finding: dict[str, Any]) -> bool:
        return str(finding.get("path", "")).startswith("detector_controls.")

    control_summaries = [
        {
            "code": finding.get("code", "detector_control_failure"),
            "detail": "See the shared feedback section for the exact executed case.",
            "path": finding.get("path", "detector_controls"),
        }
        for finding in normalized_findings
        if is_control(finding)
    ]
    return [finding for finding in normalized_findings if not is_control(finding)] + (
        control_summaries
    )


def _correction_instruction(
    failed_stage: str,
    findings: list[dict[str, Any]] | tuple[dict[str, Any], ...] | list[Finding],
) -> str:
    """Return the shared instruction, scoped to review findings when a review failed."""

    instruction = (
        "Address every substantiated finding together. Verify criticism against "
        "the original scenario and supplied evidence, preserve supported meaning, "
        "and retain an essential unsupported requirement as unresolved instead "
        "of inventing facts."
    )
    if failed_stage in {"plan_review", "artifact_review"} or any(
        isinstance(finding, Finding) and finding.code == "semantic_review" for finding in findings
    ):
        instruction += (
            " The CURRENT FINDINGS contain only semantic-review findings whose "
            "question IDs are in the closed scope for this stage. Out-of-scope "
            "review findings are intentionally omitted; do not reconstruct or "
            "address them."
        )
    return instruction


def _artifact_correction_interfaces(original_context: dict[str, Any]) -> dict[str, Any]:
    """Return the observation guide and evidence packet interface for the fixed plan."""

    accepted_plan = original_context.get("accepted_plan")
    runtime_evidence_interface = original_context.get("runtime_evidence_interface")
    runtime_contract = (
        runtime_evidence_interface.get("runtime_contract")
        if isinstance(runtime_evidence_interface, dict)
        else None
    )
    observation_guide = artifact_observation_guide(
        accepted_plan if isinstance(accepted_plan, dict) else {},
        runtime_contract if isinstance(runtime_contract, dict) else None,
        omission=_context_has_not_called(original_context),
    )
    evidence_packet_interface = _render_evidence_packet_interface(
        claim_level=_plan_claim_level(accepted_plan),
        required_observations=(
            accepted_plan.get("required_observations") if isinstance(accepted_plan, dict) else None
        ),
        semantic_judge_needed=_plan_semantic_judge_needed(accepted_plan),
    )
    return {
        "observation_guide": observation_guide,
        "evidence_packet_interface": evidence_packet_interface,
    }


def _render_correction_sections(sections: tuple[tuple[str, Any], ...]) -> str:
    """Render correction sections compactly while preserving each value exactly."""

    rendered: list[str] = []
    for title, value in sections:
        rendered.append(title)
        rendered.append(
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )
        rendered.append("")
    return "\n".join(rendered).rstrip() + "\n"


_CORRECTION_SYSTEM_V3 = (
    "Correct the current output for the named authoring stage. Return a complete "
    "replacement in that stage's required format and address every substantiated "
    "finding together. Verify criticism against the original scenario and supplied "
    "evidence, preserve supported meaning, and retain an essential unsupported "
    "requirement as unresolved instead of inventing facts. Plan correction may revise "
    "the plan within the supplied context. Artifact correction keeps the accepted plan "
    "fixed and cannot rewrite setup, bindings, prerequisites, or observation level. "
    "Do not add target access, setup, judge calls, retries, or self-approval."
)
_CORRECTION_SYSTEM_V4 = _CORRECTION_SYSTEM_V3
_CORRECTION_SYSTEM_V5 = _CORRECTION_SYSTEM_V4
