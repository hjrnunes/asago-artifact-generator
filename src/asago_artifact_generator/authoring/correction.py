"""Stage-local correction context and the rendered correction prompt."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .binding_repair import (
    CorrectionRepairInputs,
    _binding_repair_options_for_correction,
    _reference_repair_options_for_correction,
)
from .contracts import _call2_contract_v2
from .core import (
    CORRECTION_PROMPT_VERSION_V42,
    CORRECTION_PROMPT_VERSION_V44,
    Finding,
    FindingTarget,
    PromptPacket,
    _canonical_json,
)
from .planted_item import planted_text_limit_in_context
from .prompt_context import (
    _render_sections,
    _scenario_design_prompt_view,
    _semantic_judge_fact_ref_guidance,
)
from .prompt_packets import _artifact_response_contract_for_prompt
from .response_decode import _readable_response
from .sequential_turns import (
    delivery_in_context,
    plan_response_contract,
    records_in_context,
    turns_in_context,
)
from .templates import prose


@dataclass
class _CorrectionView:
    """Values the correction packet sections read, derived once per packet."""

    context: dict[str, Any]
    artifact: bool
    original_context: dict[str, Any]
    fact_ref_guidance: dict[str, Any] | None
    plan_field_meanings: Any
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
    original_context = _correction_prompt_context(
        correction_context["original_context"],
        correction_context["current_output"],
        correction_context["findings"],
    )
    plan_field_meanings = original_context.pop("plan_field_meanings", None)
    original_context.pop("semantic_judge_fact_ref_guidance", None)
    accepted_plan = correction_context.get("original_context", {}).get("accepted_plan")
    artifact = correction_context.get("stage") == "artifact"
    return _CorrectionView(
        context=correction_context,
        artifact=artifact,
        original_context=original_context,
        fact_ref_guidance=fact_ref_guidance,
        plan_field_meanings=plan_field_meanings,
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
        prose("correction/fixed_plan_decision"),
    )


def _original_stage_context_section(view: _CorrectionView) -> tuple[str, Any] | None:
    return ("ORIGINAL STAGE CONTEXT", view.original_context)


def _plan_field_meanings_section(view: _CorrectionView) -> tuple[str, Any] | None:
    if not isinstance(view.plan_field_meanings, str):
        return None
    return ("PLAN FIELD MEANINGS", view.plan_field_meanings)


def _targets_a_fact_ref(path: Any) -> bool:
    """Return whether a finding path targets one ``semantic_judge_spec.fact_refs`` entry."""

    target = FindingTarget.parse(str(path))
    return (
        target is not None
        and target.parts[:2] == ("semantic_judge_spec", "fact_refs")
        and len(target.parts) > 2
        and isinstance(target.parts[2], int)
    )


def _fact_ref_guidance_section(view: _CorrectionView) -> tuple[str, Any] | None:
    unknown_fact_ref_findings = [
        finding
        for finding in view.context.get("findings", [])
        if isinstance(finding, dict)
        and finding.get("code") == "unknown_reference"
        and _targets_a_fact_ref(finding.get("path", ""))
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
    return ("CURRENT OUTPUT", view.context["current_output"])


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


# The packet's section order.  The binding repair options are computed after
# the context sections and the reference repair options after the output
# sections, so the three groups also fix the order of those computations.
_CORRECTION_CONTEXT_SECTIONS: tuple[_CorrectionSection, ...] = (
    _failed_stage_section,
    _fixed_plan_decision_section,
    _original_stage_context_section,
    _plan_field_meanings_section,
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
)


def _rendered_sections(
    view: _CorrectionView, renderers: tuple[_CorrectionSection, ...]
) -> list[tuple[str, Any]]:
    return [section for render in renderers if (section := render(view)) is not None]


def _render_correction_packet(
    correction_context: dict[str, Any],
    repair_inputs: CorrectionRepairInputs,
) -> PromptPacket:
    """Render one shared correction prompt for every artifact caller."""

    view = _correction_view(correction_context)
    sections = _rendered_sections(view, _CORRECTION_CONTEXT_SECTIONS)
    if correction_context.get("stage") == "plan":
        view.binding_repair_options = _binding_repair_options_for_correction(
            correction_context["current_output"], correction_context["findings"], repair_inputs
        )
    sections += _rendered_sections(view, _CORRECTION_OUTPUT_SECTIONS)
    view.reference_repair_options = _reference_repair_options_for_correction(
        correction_context["findings"], repair_inputs
    )
    sections += _rendered_sections(view, _CORRECTION_REPAIR_SECTIONS)
    payload = deepcopy(correction_context)
    if view.binding_repair_options is not None:
        payload["binding_repair_options"] = view.binding_repair_options
    if view.reference_repair_options is not None:
        payload["reference_repair_options"] = view.reference_repair_options
    packet = PromptPacket(
        stage="correction",
        version=(
            CORRECTION_PROMPT_VERSION_V44 if view.artifact else CORRECTION_PROMPT_VERSION_V42
        ),
        system=_CORRECTION_SYSTEM,
        user=_render_sections(
            tuple(sections), compact_titles=frozenset(title for title, _ in sections)
        ),
        payload=payload,
    )
    return packet


def _correction_findings_view(value: Any) -> Any:
    """Return an independent copy without shortening owner or reviewer text."""

    return deepcopy(value)


def _correction_prompt_context(
    context: dict[str, Any], current_output: Any, findings: Any
) -> dict[str, Any]:
    """Keep stage rules and relevant facts without replaying the original request."""

    result = deepcopy(context)
    if "scenario_design" in result:
        result["scenario_design"] = _scenario_design_prompt_view(result["scenario_design"])
    scope_text = f"{current_output}\n{_canonical_json(findings)}"
    source_context = result.get("source_context")
    if isinstance(source_context, dict):
        _scope_plan_correction_source_context(source_context, scope_text)
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
    result.pop("neutral_outcome_example", None)
    return result


def _scope_plan_correction_source_context(source_context: dict[str, Any], scope_text: str) -> None:
    """Replace the full Call 1 inventory with referenced detail and compact catalogs.

    Binding and reference findings get deterministic repair options elsewhere in
    the correction prompt.  This view therefore keeps full operation detail only
    when the rejected output or findings name it, while retaining the identifiers
    of every alternative the author may choose.
    """

    facts = source_context.get("facts")
    operations = source_context.get("operations")
    source_handles = source_context.get("source_handles")
    source_context["facts"] = (
        [
            summary
            for item in facts
            if isinstance(item, dict)
            if (
                summary := _correction_fact_summary(
                    item,
                    referenced=isinstance(item.get("ref"), str) and item["ref"] in scope_text,
                )
            )
            is not None
        ]
        if isinstance(facts, list)
        else []
    )
    source_context["operations"] = [
        deepcopy(item)
        for item in operations or []
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"] in scope_text
    ]
    source_context["source_handles"] = [
        deepcopy(item)
        for item in source_handles or []
        if isinstance(item, dict)
        and isinstance(item.get("ref"), str)
        and item["ref"] in scope_text
    ]
    source_context["available_operation_names"] = sorted(
        {
            item["name"]
            for item in operations or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
    )
    source_context["available_source_refs"] = sorted(
        {
            item[key]
            for values, key in ((facts or [], "ref"), (source_handles or [], "ref"))
            for item in values
            if isinstance(item, dict) and isinstance(item.get(key), str)
        }
    )
    source_context["scope"] = (
        "Facts are compact summaries. Operations and source handles carry full detail only "
        "when the rejected output or a finding names them; the available identifier lists "
        "show the remaining documented choices. Finding-specific source and selector detail "
        "appears in the repair-options sections."
    )


def _correction_fact_summary(fact: dict[str, Any], *, referenced: bool) -> dict[str, Any] | None:
    """Return the small part of one supplied fact a correction needs for orientation."""

    reference = fact.get("ref")
    if not isinstance(reference, str):
        return None
    summary: dict[str, Any] = {"ref": reference}
    schema = fact.get("schema")
    if isinstance(schema, dict) and isinstance(schema.get("type"), str):
        summary["schema_type"] = schema["type"]
    value = fact.get("value")
    if referenced and (isinstance(value, (str, int, float, bool)) or value is None):
        if len(_canonical_json(value).encode("utf-8")) <= 512:
            summary["value"] = deepcopy(value)
    return summary


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


_PLAN_CORRECTION_GUIDANCE = prose("correction/plan_correction_guidance")
_ARTIFACT_CORRECTION_GUIDANCE = prose("correction/artifact_correction_guidance")
_CURRENT_ARTIFACT_CORRECTION_GUIDANCE = prose("correction/current_artifact_correction_guidance")


def build_correction_context(
    *,
    failed_stage: str,
    original_context: dict[str, Any],
    current_output: bytes,
    findings: Sequence[Finding],
) -> dict[str, Any]:
    """Build the correction context of a failed ``call1`` or ``call2`` response."""

    if failed_stage not in _CORRECTION_STAGES:
        raise ValueError(f"unsupported correction stage: {failed_stage}")
    stage = _CORRECTION_STAGES[failed_stage]
    output_text, output_encoding = _readable_response(current_output)
    instruction = _correction_instruction(findings)
    context: dict[str, Any] = {
        "stage": stage,
        "failed_stage": failed_stage,
        "original_context": deepcopy(original_context),
        "current_output": output_text,
        "current_output_encoding": output_encoding,
        "findings": [finding.to_dict() for finding in findings],
        "instruction": instruction,
    }
    if stage == "plan":
        context.update(
            {
                "accepted_plan_fixed": False,
                "format": prose("correction/plan_format"),
                "response_contract": plan_response_contract(
                    turns_in_context(original_context),
                    delivery_in_context(original_context),
                    records_in_context(original_context),
                ),
            }
        )
        context["instruction"] = (
            instruction + " " + prose("correction/plan_framing") + " " + _PLAN_CORRECTION_GUIDANCE
        )
    else:
        context.update(
            {
                "accepted_plan_fixed": True,
                "format": prose("correction/artifact_format"),
                "response_contract": _call2_contract_v2(
                    original_context.get("accepted_plan"),
                    planted_max_chars=planted_text_limit_in_context(original_context),
                ),
            }
        )
        context["instruction"] = (
            instruction
            + " "
            + prose("correction/artifact_framing")
            + " "
            + _ARTIFACT_CORRECTION_GUIDANCE
        )
    return context


_CORRECTION_STAGES = {"call1": "plan", "call2": "artifact"}


def _correction_instruction(findings: Sequence[Finding]) -> str:
    """Return the shared instruction, scoped to review findings when a review failed."""

    instruction = prose("correction/instruction")
    if any(finding.code == "semantic_review" for finding in findings):
        instruction += " " + prose("correction/semantic_review_scope")
    return instruction


_CORRECTION_SYSTEM = prose("correction/correction_system")
