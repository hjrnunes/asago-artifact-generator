"""Stage-local correction context and the rendered correction prompt."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .binding_repair import (
    _binding_repair_options_for_correction,
    _reference_repair_options_for_correction,
)
from .contracts import _call1_contract_v2, _call2_contract_v2
from .core import (
    CORRECTION_PROMPT_VERSION_V29,
    CORRECTION_PROMPT_VERSION_V30,
    Call2FramingError,
    Finding,
    PromptPacket,
    _canonical_json,
)
from .prompt_context import (
    _OWNER_SCOPE_SECTION_TITLE,
    _scenario_design_prompt_view,
    _semantic_judge_fact_ref_guidance,
)
from .prompt_packets import _artifact_response_contract_for_prompt
from .response_decode import _decode_call2_json_response, _readable_response


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
    accepted_plan = correction_context.get("original_context", {}).get("accepted_plan")
    artifact = correction_context.get("stage") == "artifact"
    return _CorrectionView(
        context=correction_context,
        artifact=artifact,
        original_context=original_context,
        fact_ref_guidance=fact_ref_guidance,
        plan_field_meanings=plan_field_meanings,
        neutral_outcome_example=neutral_outcome_example,
        owner_scope=owner_scope,
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
        "This decision is fixed; correct the artifact within it.",
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


def _plan_field_meanings_section(view: _CorrectionView) -> tuple[str, Any] | None:
    if not isinstance(view.plan_field_meanings, str):
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
    _plan_field_meanings_section,
    _neutral_outcome_example_section,
    _fact_ref_guidance_section,
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
            CORRECTION_PROMPT_VERSION_V30 if view.artifact else CORRECTION_PROMPT_VERSION_V29
        ),
        system=_CORRECTION_SYSTEM,
        user=_render_correction_sections(tuple(sections)),
        payload=payload,
    )
    return packet


def _correction_findings_view(value: Any) -> Any:
    """Drop the review details that the finding detail already states."""

    if not isinstance(value, list):
        return value
    result: list[Any] = []
    for item in value:
        if not isinstance(item, dict):
            result.append(item)
            continue
        if item.get("code") == "semantic_review" and "details" in item:
            # The review location and required change already appear in detail.
            item = {key: value for key, value in item.items() if key != "details"}
        result.append(item)
    return result


def _correction_current_output_view(value: Any, *, artifact: bool) -> Any:
    """Canonicalize an artifact object while retaining its exact values."""

    if not artifact or not isinstance(value, str):
        return value
    try:
        decoded, _ = _decode_call2_json_response(value.encode("utf-8"))
    except (Call2FramingError, UnicodeDecodeError, ValueError):
        return value
    return _canonical_json(decoded)


def _correction_prompt_context(context: dict[str, Any]) -> dict[str, Any]:
    """Keep correction context authoritative without replaying authoring payloads."""

    result = deepcopy(context)
    if "scenario_design" in result:
        result["scenario_design"] = _scenario_design_prompt_view(result["scenario_design"])
    authoritative = result.get("authoritative_context")
    if isinstance(authoritative, dict) and isinstance(result.get("runtime_contract"), dict):
        _scope_correction_authoritative_context(
            authoritative,
            result.get("accepted_plan"),
        )
    response_contract = result.get("response_contract")
    if isinstance(response_contract, dict):
        response_contract.pop("neutral_example", None)
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
    "Keep the accepted plan fixed. Correct the artifact using the substantiated "
    "deterministic and review findings, and preserve working content beyond them. "
    "Return a complete replacement artifact as one JSON object in the unchanged "
    "response format. Keep an accepted no-judge decision as null judge metadata, "
    "and return a semantic_judge_spec object when the accepted plan claims reply; "
    "repair the candidate rather than changing the plan to fit it. Use the accepted "
    "plan's runtime binding for execution identities; the neutral example uses "
    "substitute values."
)
_CURRENT_ARTIFACT_CORRECTION_GUIDANCE = (
    "Runtime bindings come from the accepted plan, and artifact authoring cannot add, "
    "rename, or change one, so a correction cannot declare a new binding. If a "
    "binding lists stimulus.user_text, keep that consumer only when its exact "
    "resolved value occurs in the authored text or the text contains its "
    "{{binding_name}} slot; otherwise remove the consumer."
)


def build_correction_context(
    *,
    failed_stage: str,
    original_context: dict[str, Any],
    current_output: bytes | str,
    findings: list[dict[str, Any]] | tuple[dict[str, Any], ...] | list[Finding],
    prior_unresolved_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a stage-aware correction context without competing formats."""

    stage = _correction_stage(failed_stage)
    if isinstance(current_output, bytes):
        output_text, output_encoding = _readable_response(current_output)
    else:
        output_text, output_encoding = current_output, "text-input"
    normalized_findings = [
        finding.to_dict() if isinstance(finding, Finding) else deepcopy(finding)
        for finding in findings
    ]
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
                    "Return one complete artifact replacement as one bare JSON object "
                    "or exactly one lowercase ```json fenced JSON object."
                ),
                "response_contract": _call2_contract_v2(
                    original_context.get("accepted_plan")
                    if isinstance(original_context, dict)
                    else None,
                ),
            }
        )
        context["instruction"] = (
            instruction + " Call 2 uses one bare JSON object or exactly one lowercase ```json "
            "fenced JSON object. " + _ARTIFACT_CORRECTION_GUIDANCE
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


_CORRECTION_SYSTEM = (
    "Correct the current output for the named authoring stage. Return a complete "
    "replacement in that stage's required format and address every substantiated "
    "finding together. Verify criticism against the original scenario and supplied "
    "evidence, preserve supported meaning, and retain an essential unsupported "
    "requirement as unresolved instead of inventing facts. Plan correction may revise "
    "the plan within the supplied context. Artifact correction keeps the accepted plan "
    "fixed and cannot rewrite setup, bindings, prerequisites, or observation level. "
    "Do not add target access, setup, judge calls, retries, or self-approval."
)
