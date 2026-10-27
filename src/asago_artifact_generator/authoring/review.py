"""Plan and artifact review prompts, question sets, and review response parsing."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..failure_evidence import redact_metadata
from ..input_adapter import InputView
from .context_budget import _enforce_prompt_size
from .contracts import (
    PLAN_FIELD_MEANINGS,
    _binding_contract,
)
from .core import (
    ARTIFACT_REVIEW_PROMPT_VERSION,
    AUTHORING_INTERFACE_VERSION_V2,
    MAX_RENDERED_PROMPT_BYTES,
    PLAN_REVIEW_PROMPT_VERSION,
    Finding,
    PromptPacket,
    ReviewResponse,
    ReviewResponseError,
    _canonical_json,
    _sha256,
)
from .inventory import _selected_refs
from .planted_item import (
    PLAN_REVIEW_RECORD_KEY,
    plan_review_record_sections,
    runtime_contract_for_prompt,
)
from .prompt_context import (
    _authoritative_context,
    _discriminating_condition_rule,
    _explained_operations,
    _neutral_outcome_example,
    _omission_trigger_rule,
    _order_comparison_rule,
    _original_scenario_context,
    _render_sections,
    _resolved_supplied_binding_values,
)
from .prompt_safety import assert_no_prompt_secrets, prompt_data_urls
from .response_decode import _decode_stage_response, _readable_response
from .sequential_turns import multi_turn_payload, multi_turn_sections, review_sections
from .templates import prose, prose_lines

_PLAN_REVIEW_QUESTIONS: tuple[dict[str, str], ...] = tuple(
    {"id": question_id, "question": prose(f"review/plan_questions/{question_id}")}
    for question_id in (
        "scenario_fidelity",
        "stimulus_fit",
        "branch_logic",
        "observability",
        "value_meaning",
        "judge_need",
    )
)
_ARTIFACT_REVIEW_QUESTIONS: tuple[dict[str, str], ...] = tuple(
    {"id": question_id, "question": prose(f"review/artifact_questions/{question_id}")}
    for question_id in (
        "judge_spec_implements_plan",
        "stimulus_realizes_plan",
        "evidence_attribution",
    )
)
PLAN_REVIEW_QUESTION_IDS = tuple(item["id"] for item in _PLAN_REVIEW_QUESTIONS)
ARTIFACT_REVIEW_QUESTION_IDS = tuple(item["id"] for item in _ARTIFACT_REVIEW_QUESTIONS)


REVIEW_FINDINGS_OMITTED = "review_findings_omitted_defaulted_empty"
_REVIEW_DECISIONS = ("accept", "revise", "blocked")
_REVIEW_FINDING_FIELDS = (
    "question",
    "location",
    "problem",
    "basis",
    "required_change",
)


def parse_review_response(raw: bytes) -> ReviewResponse:
    """Parse one strict reviewer response without changing its raw bytes.

    The accepted framing is one bare JSON object or exactly one lowercase
    ```json fenced JSON object, the same strict normalization as a v2 Call 1
    response; a reply of several fences with text between them and no text
    after the last decodes to its last object.  Prose wrappers, multiple
    objects in one fence, adjacent fences, and malformed JSON fail
    mechanically. ``accept`` requires an empty findings array while
    ``revise`` and ``blocked`` require at least one complete finding; a
    contradictory decision raises instead of being silently coerced. An
    ``accept`` that omits the ``findings`` key carries the same information as
    an empty array, so it parses with no findings and ``findings_omitted`` set
    for the caller to record; ``null`` or any other non-list value, and an
    omitted key on ``revise`` or ``blocked``, still raise. The parser
    validates the finding shape; stage-specific question membership is applied
    by the semantic-review caller so out-of-scope findings can remain in
    evidence without reaching correction.
    """

    if not isinstance(raw, bytes):
        raise TypeError("review response must be bytes")
    try:
        decoded, transformation = _decode_stage_response("review", raw)
    except UnicodeDecodeError as exc:
        raise ReviewResponseError(
            [Finding("invalid_json", f"review response is not valid UTF-8: {exc}", "review")]
        ) from exc
    decision = decoded.get("decision")
    summary = decoded.get("summary")
    problems = _review_envelope_problems(decoded, decision, summary)
    findings_omitted = decision == "accept" and "findings" not in decoded
    raw_findings = [] if findings_omitted else decoded.get("findings")
    if not isinstance(raw_findings, list):
        problems.append(Finding("review_schema", "review findings must be a list", "review"))
        raw_findings = []
    else:
        for index, item in enumerate(raw_findings):
            problems.extend(_review_finding_shape_problems(item, index))
    problems.extend(_review_decision_contradictions(decision, raw_findings))
    if problems:
        raise ReviewResponseError(problems)
    return ReviewResponse(
        decision=decision,
        summary=summary,
        findings=tuple(dict(item) for item in raw_findings),
        transformation=transformation,
        findings_omitted=findings_omitted,
    )


# The code of the finding for a review response the provider stopped at its
# completion token limit, and the finish reason that reports that stop.
REVIEW_TRUNCATED = "review_truncated"
_TOKEN_LIMIT_FINISH_REASON = "length"


def review_truncation_findings(
    response_capture: dict[str, Any] | None, controls: dict[str, Any] | None
) -> list[Finding]:
    """Return the finding for a review response that stopped at the completion token limit.

    Only the provider's recorded finish reason decides, as the response
    capture classifies it (``{"state": "value", "value": "length"}``); the
    response text and any reasoning are not read.  ``controls`` supplies the
    limit the request carried, named in the detail when it was recorded.  A
    response without such a capture, or one that finished for another reason,
    has no finding.  A review retry shows the finding to the reviewer like any
    other check finding, so its detail states the requirement the retry must
    meet.
    """

    finish_reason = (response_capture or {}).get("finish_reason")
    if finish_reason != {"state": "value", "value": _TOKEN_LIMIT_FINISH_REASON}:
        return []
    limit = (controls or {}).get("max_completion_tokens")
    limit_text = f" of {limit} tokens" if isinstance(limit, int) else ""
    return [
        Finding(
            REVIEW_TRUNCATED,
            f"review response stopped at the completion token limit{limit_text} before it was "
            f'complete (finish_reason "{_TOKEN_LIMIT_FINISH_REASON}"); a review response must '
            "be complete within that limit",
            "review",
        )
    ]


def _review_envelope_problems(
    decoded: dict[str, Any], decision: Any, summary: Any
) -> list[Finding]:
    """Return problems with the review's field set, decision, and summary."""

    problems: list[Finding] = []
    unknown = set(decoded) - {"decision", "summary", "findings"}
    if unknown:
        problems.append(
            Finding(
                "review_schema",
                f"review response has unknown fields: {', '.join(sorted(unknown))}",
                "review",
            )
        )
    if decision not in _REVIEW_DECISIONS:
        problems.append(
            Finding(
                "review_schema",
                "review decision must be one of accept, revise, blocked",
                "review",
            )
        )
    if not isinstance(summary, str) or not summary.strip():
        problems.append(
            Finding("review_schema", "review summary must be a nonblank string", "review")
        )
    return problems


def _review_decision_contradictions(decision: Any, raw_findings: list[Any]) -> list[Finding]:
    """Return a problem when the decision disagrees with whether findings are present."""

    if decision == "accept" and raw_findings:
        return [
            Finding(
                "review_contradiction",
                "accept requires an empty findings array",
                "review",
            )
        ]
    if decision in {"revise", "blocked"} and not raw_findings:
        return [
            Finding(
                "review_contradiction",
                f"{decision} requires at least one complete finding",
                "review",
            )
        ]
    return []


def _review_finding_shape_problems(item: Any, index: int) -> list[Finding]:
    """Return the shape findings for one reviewer finding entry."""

    path = f"review.findings[{index}]"
    if not isinstance(item, dict):
        return [Finding("review_schema", f"{path} must be an object", path)]
    unknown = set(item) - set(_REVIEW_FINDING_FIELDS)
    # ``question`` is deliberately allowed to be absent or malformed here.
    # The stage scope filter must retain the complete original finding as
    # out-of-scope evidence instead of turning it into an unavailable review.
    required = set(_REVIEW_FINDING_FIELDS) - {"question"}
    missing = required - set(item)
    if unknown or missing:
        return [
            Finding(
                "review_schema",
                f"{path} must have exactly question, location, problem, basis, and required_change"
                f" (missing={sorted(missing)}, unknown={sorted(unknown)})",
                path,
            )
        ]
    blank = [
        name for name in required if not isinstance(item[name], str) or not item[name].strip()
    ]
    if blank:
        return [
            Finding(
                "review_schema",
                f"{path} fields must be nonblank strings: {', '.join(blank)}",
                path,
            )
        ]
    return []


def _review_question_ids(stage: str) -> tuple[str, ...]:
    """Return the exact semantic question IDs for one review stage."""

    if stage == "plan_review":
        return PLAN_REVIEW_QUESTION_IDS
    if stage == "artifact_review":
        return ARTIFACT_REVIEW_QUESTION_IDS
    raise ValueError(f"unsupported review stage: {stage}")


def _scope_review_response(
    review: ReviewResponse,
    *,
    stage: str,
) -> tuple[str, tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Drop findings whose exact question ID is outside the stage scope.

    The raw response remains available separately. Each out-of-scope finding
    stays byte-for-byte represented as a decoded object in evidence, while only
    in-scope findings can drive correction or blocking. No keyword or prose
    matching occurs.
    """

    allowed = set(_review_question_ids(stage))
    in_scope: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    for finding in review.findings:
        question = finding.get("question")
        if isinstance(question, str) and question in allowed:
            in_scope.append(dict(finding))
        else:
            out_of_scope.append(dict(finding))
    decision = review.decision
    if decision in {"revise", "blocked"} and not in_scope:
        decision = "accept"
    return decision, tuple(in_scope), tuple(out_of_scope)


_PLAN_REVIEW_GUIDANCE = prose("review/plan_review_guidance")
_SETUP_PERMISSION_EXPLANATION = prose("review/setup_permission_explanation")
_PLAN_SELECTOR_FORMS = prose_lines("review/plan_selector_forms")
_PLAN_STRUCTURAL_VALIDITY_NOTE = prose("review/plan_structural_validity_note")


def _review_question_context(
    questions: tuple[dict[str, str], ...],
    *,
    fixed_plan: bool,
) -> dict[str, Any]:
    """Return the closed semantic question list rendered to a reviewer."""

    return {
        "instruction": (
            prose("review/question_instruction_fixed_plan")
            if fixed_plan
            else (prose("review/question_instruction"))
        ),
        "question_ids": [item["id"] for item in questions],
        "questions": [dict(item) for item in questions],
    }


_ARTIFACT_REVIEW_GUIDANCE = prose("review/artifact_review_guidance")


@dataclass(frozen=True)
class PriorReviewRound:
    """What the plan reviewer said about the plan before the author revised it."""

    findings: tuple[dict[str, str], ...]
    reviewed_plan: dict[str, Any]


_PRIOR_REVIEW_ROUND_MEANING = prose("review/prior_review_round_meaning")
_PRIOR_REVIEW_ROUND_INSTRUCTION = prose("review/prior_review_round_instruction")


_ABSENT = object()


def _prior_review_round_context(
    prior_round: PriorReviewRound, plan: dict[str, Any]
) -> dict[str, Any]:
    """Return the earlier findings and the plan fields the author changed in answer."""

    reviewed = prior_round.reviewed_plan
    changed = [
        key
        for key in dict.fromkeys((*plan, *reviewed))
        if plan.get(key, _ABSENT) != reviewed.get(key, _ABSENT)
    ]
    return {
        "meaning": _PRIOR_REVIEW_ROUND_MEANING,
        "findings": [dict(finding) for finding in prior_round.findings],
        "author_response": {
            "changed_fields": changed,
            "reviewed_values": {
                key: deepcopy(reviewed[key]) for key in changed if key in reviewed
            },
        },
        "reviewer_instruction": _PRIOR_REVIEW_ROUND_INSTRUCTION,
    }


@dataclass(frozen=True)
class RejectedReviewResponse:
    """A review response that failed its checks, and every finding the checks reported."""

    raw: bytes
    findings: tuple[Finding, ...]


_REJECTED_REVIEW_RESPONSE_MEANING = prose("review/rejected_response_meaning")
_REJECTED_REVIEW_RESPONSE_INSTRUCTION = prose("review/rejected_response_instruction")


def _rejected_review_response_context(rejected: RejectedReviewResponse) -> dict[str, Any]:
    """Return the rejected response exactly as returned and its findings as reported."""

    text, encoding = _readable_response(rejected.raw)
    return {
        "meaning": _REJECTED_REVIEW_RESPONSE_MEANING,
        "response": text,
        "response_encoding": encoding,
        "findings": [finding.to_dict() for finding in rejected.findings],
        "instruction": _REJECTED_REVIEW_RESPONSE_INSTRUCTION,
    }


def _rejected_review_response_prompt_sections(
    context: dict[str, Any],
) -> tuple[tuple[str, Any], ...]:
    """Return the rejected-response sections when the review asks again; none otherwise."""

    rejected = context.get("rejected_review_response")
    if rejected is None:
        return ()
    return (
        ("PREVIOUS REVIEW RESPONSE REJECTED", rejected["meaning"]),
        ("REJECTED REVIEW RESPONSE", rejected["response"]),
        ("REJECTED REVIEW RESPONSE FINDINGS", rejected["findings"]),
        ("REVIEW RESPONSE RETRY INSTRUCTION", rejected["instruction"]),
    )


def _prior_review_round_prompt_sections(
    context: dict[str, Any],
) -> tuple[tuple[str, Any], ...]:
    """Return the prior-round section when the review follows a revision."""

    prior_round = context.get("prior_review_round")
    return () if prior_round is None else (("PRIOR REVIEW ROUND", prior_round),)


def build_plan_reviewer_context(
    view: InputView,
    plan: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    prior_round: PriorReviewRound | None = None,
    rejected: RejectedReviewResponse | None = None,
) -> dict[str, Any]:
    """Build a fresh authoritative context for the plan reviewer.

    A review that follows a revision also carries the earlier findings and the
    author's response; a first review carries neither.  A review that asks
    again after a rejected response also carries that response and its findings.
    """

    context = {
        "original_scenario": _original_scenario_context(view),
        "authoritative_context": _authoritative_context(view, inventory, runtime_contract),
        "plan_field_meanings": PLAN_FIELD_MEANINGS,
        "binding_and_setup_rules": {
            "binding_contract": _binding_contract(),
            "setup_permissions_explanation": _SETUP_PERMISSION_EXPLANATION,
            "documented_selector_forms": list(_PLAN_SELECTOR_FORMS),
            "structural_validity_note": _PLAN_STRUCTURAL_VALIDITY_NOTE,
            **_discriminating_condition_rule(view),
            **_omission_trigger_rule(view),
            **_order_comparison_rule(view),
        },
        "neutral_outcome_example": _neutral_outcome_example(view),
        "candidate_plan": deepcopy(plan),
        "resolved_supplied_binding_values": _resolved_supplied_binding_values(plan, inventory),
        "review_questions": _review_question_context(
            _PLAN_REVIEW_QUESTIONS,
            fixed_plan=False,
        ),
        "response_contract": {
            **_review_response_contract(question_ids=PLAN_REVIEW_QUESTION_IDS),
            "example_response": _review_response_example(),
        },
        "acceptance_examples": _review_acceptance_examples(),
    }
    if prior_round is not None:
        context["prior_review_round"] = _prior_review_round_context(prior_round, plan)
    if rejected is not None:
        context["rejected_review_response"] = _rejected_review_response_context(rejected)
    return context


def _inventory_names(inventory: dict[str, Any], section: str, key: str) -> set[str]:
    return {
        item[key]
        for item in inventory.get(section, [])
        if isinstance(item, dict) and isinstance(item.get(key), str)
    }


def _artifact_plan_references(plan: dict[str, Any], fact_refs: Collection[str]) -> Iterator[Any]:
    """Yield every inventory reference the accepted plan or the judge facts cite."""

    interpretation = plan.get("interpretation")
    if isinstance(interpretation, dict):
        yield from interpretation.get("source_refs", [])
    for assumption in plan.get("assumptions", []):
        if isinstance(assumption, dict):
            yield assumption.get("ref")
    for prerequisite in plan.get("prerequisites", []):
        if isinstance(prerequisite, dict):
            yield from prerequisite.get("evidence_refs", [])
    for operation in plan.get("setup_recipe", []):
        if isinstance(operation, dict):
            yield operation.get("name")
    yield from fact_refs


def _artifact_review_cited_names(
    plan: dict[str, Any],
    inventory: dict[str, Any],
    fact_refs: Collection[str],
) -> tuple[set[str], set[str], set[str]]:
    """Return the operation, fact, and source names the accepted plan selects or cites."""

    selected = _selected_refs(plan, inventory)
    operation_names = set(selected["operations"])
    fact_names = set(selected["facts"])
    source_names = set(selected["sources"])
    operation_map = _inventory_names(inventory, "operations", "name")
    fact_map = _inventory_names(inventory, "facts", "ref")
    source_map = _inventory_names(inventory, "source_handles", "ref")
    for value in _artifact_plan_references(plan, fact_refs):
        if not isinstance(value, str):
            continue
        if value in fact_map:
            fact_names.add(value)
        elif value in source_map:
            source_names.add(value)
        elif value in operation_map:
            operation_names.add(value)
        elif value.startswith("operation:") and value.split(":", 1)[1] in operation_map:
            operation_names.add(value.split(":", 1)[1])
    return operation_names, fact_names, source_names


def _artifact_review_authoritative_context(
    plan: dict[str, Any],
    inventory: dict[str, Any],
    fact_refs: Collection[str],
) -> dict[str, Any]:
    """Keep only inventory material referenced by the accepted artifact plan."""

    operation_names, fact_names, source_names = _artifact_review_cited_names(
        plan, inventory, fact_refs
    )
    return {
        "facts": [
            deepcopy(fact)
            for fact in inventory.get("facts", [])
            if isinstance(fact, dict) and fact.get("ref") in fact_names
        ],
        "operations": _explained_operations(inventory, operation_names),
        "source_handles": [
            deepcopy(handle)
            for handle in inventory.get("source_handles", [])
            if isinstance(handle, dict) and handle.get("ref") in source_names
        ],
        "runtime_capabilities": {"note": prose("review/runtime_capabilities_note")},
    }


def build_artifact_reviewer_context(
    view: InputView,
    plan: dict[str, Any],
    metadata: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    rejected: RejectedReviewResponse | None = None,
) -> dict[str, Any]:
    """Build exact candidate evidence for the artifact reviewer.

    A review that asks again after a rejected response also carries that
    response and its findings.
    """

    judge_spec = metadata.get("semantic_judge_spec")
    fact_refs = (
        list(judge_spec.get("fact_refs", []))
        if isinstance(judge_spec, dict) and isinstance(judge_spec.get("fact_refs"), list)
        else []
    )
    authoritative_context = _artifact_review_authoritative_context(
        plan,
        inventory,
        fact_refs,
    )
    context = {
        "original_scenario": _original_scenario_context(view),
        "authoritative_context": authoritative_context,
        "plan_field_meanings": PLAN_FIELD_MEANINGS,
        "accepted_plan": deepcopy(plan),
        "runtime_contract": runtime_contract_for_prompt(runtime_contract),
        "candidate_artifact": deepcopy(metadata),
        "resolved_runtime_context": {
            "binding_declarations": deepcopy(plan.get("runtime_bindings", [])),
            "judge_spec": deepcopy(judge_spec),
            "judge_fact_refs": fact_refs,
            "judge_facts": [
                deepcopy(fact)
                for fact in inventory.get("facts", [])
                if isinstance(fact, dict) and fact.get("ref") in fact_refs
            ],
            "judge_facts_are_in_authoritative_context": bool(fact_refs),
        },
        "binding_and_setup_rules": {
            "binding_contract": _binding_contract(),
            "setup_permissions_explanation": _SETUP_PERMISSION_EXPLANATION,
            "documented_selector_forms": list(_PLAN_SELECTOR_FORMS),
            **_discriminating_condition_rule(view),
        },
        "review_questions": _review_question_context(
            _ARTIFACT_REVIEW_QUESTIONS,
            fixed_plan=True,
        ),
        "response_contract": {
            **_review_response_contract(question_ids=ARTIFACT_REVIEW_QUESTION_IDS),
            "example_response": _review_response_example(),
        },
        "acceptance_examples": _review_acceptance_examples(),
    }
    if rejected is not None:
        context["rejected_review_response"] = _rejected_review_response_context(rejected)
    return context


def _review_response_example() -> dict[str, Any]:
    """Return a parser-valid illustrative review response."""

    return {
        "decision": "accept",
        "summary": (
            "The experiment preserves the supplied criterion and uses only supported observations."
        ),
        "findings": [],
    }


def _review_acceptance_examples() -> dict[str, dict[str, str]]:
    """Return bounded examples that do not prescribe a scenario answer."""

    return {
        "numeric_attempt": {
            "case": "A numeric argument is compared with a supplied bound.",
            "judgment": prose("review/acceptance_examples/numeric_attempt"),
        },
        "judge_backed_reply": {
            "case": "A reply must be interpreted against a declared proposition.",
            "judgment": prose("review/acceptance_examples/judge_backed_reply"),
        },
        "static_assumption": {
            "case": "A source-backed static fact is needed before execution.",
            "judgment": prose("review/acceptance_examples/static_assumption"),
        },
    }


def _review_response_contract(
    *,
    question_ids: Collection[str],
) -> dict[str, Any]:
    """Return the closed reviewer response contract for one stage."""

    return {
        "framing": {
            "accepted": [
                "one bare JSON object",
                "exactly one lowercase ```json fenced JSON object",
            ],
            "whole_response_rule": prose("review/response_contract/whole_response_rule"),
            "rejected": [
                "untagged fence",
                "uppercase or differently tagged fence",
                "multiple objects or fences",
                "prose wrapper",
                "trailing content",
            ],
        },
        "fields": ["decision", "summary", "findings"],
        "decision_values": list(_REVIEW_DECISIONS),
        "consistency": {
            "accept": "findings must be empty",
            "revise": "findings must contain at least one complete finding",
            "blocked": "findings must contain at least one complete finding",
        },
        "finding_fields": list(_REVIEW_FINDING_FIELDS),
        "question_ids": list(question_ids),
        "question_field": prose("review/response_contract/question_field"),
        "finding_field_rule": prose("review/response_contract/finding_field_rule"),
        "example_finding": {
            "question": question_ids[0],
            "location": "candidate_plan.observation_claim.violation",
            "problem": "The decision condition does not preserve the supplied record.",
            "basis": (
                "The scenario identifies record neutral-1, but the condition uses neutral-2."
            ),
            "required_change": "Use the supplied record identity in the condition.",
        },
        "forbidden": [
            "numeric quality scores",
            "severity rankings",
            "confidence thresholds",
            "replacement content",
            "style advice",
        ],
    }


def build_plan_review_packet(
    view: InputView,
    plan: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    max_prompt_bytes: int = MAX_RENDERED_PROMPT_BYTES,
    prior_round: PriorReviewRound | None = None,
    rejected: RejectedReviewResponse | None = None,
) -> PromptPacket:
    """Render a source-derived plan-review prompt.

    ``rejected`` is the earlier response to this same review that failed its
    checks; the prompt then adds it and its findings after the usual sections.
    """

    context = build_plan_reviewer_context(
        view,
        plan,
        inventory,
        runtime_contract,
        prior_round=prior_round,
        rejected=rejected,
    )
    record_sections = plan_review_record_sections(view, runtime_contract)
    payload = {
        "interface": AUTHORING_INTERFACE_VERSION_V2,
        "stage": "plan_review",
        **context,
        **multi_turn_payload(view, runtime_contract),
        **({PLAN_REVIEW_RECORD_KEY: record_sections[0][1]} if record_sections else {}),
    }
    assert_no_prompt_secrets(payload)
    packet = PromptPacket(
        stage="plan_review",
        version=PLAN_REVIEW_PROMPT_VERSION,
        system=_PLAN_REVIEW_SYSTEM_V3,
        user=_render_sections(
            (
                ("ORIGINAL SCENARIO", context["original_scenario"]),
                ("AUTHORITATIVE CONTEXT", context["authoritative_context"]),
            )
            + multi_turn_sections(view, runtime_contract)
            + record_sections
            + (
                ("PLAN FIELD MEANINGS", context["plan_field_meanings"]),
                ("BINDING AND SETUP RULES", context["binding_and_setup_rules"]),
                ("NEUTRAL OUTCOME EXAMPLE", context["neutral_outcome_example"]),
                ("REVIEW QUESTIONS", context["review_questions"]),
                ("CANDIDATE PLAN", context["candidate_plan"]),
            )
            + _prior_review_round_prompt_sections(context)
            + (
                (
                    "RESOLVED SUPPLIED BINDING VALUES",
                    context["resolved_supplied_binding_values"],
                ),
                ("REVIEW RESPONSE CONTRACT", context["response_contract"]),
                ("BOUNDED ACCEPTANCE EXAMPLES", context["acceptance_examples"]),
            )
            + _rejected_review_response_prompt_sections(context)
        ),
        payload=payload,
    )
    _enforce_prompt_size(
        packet,
        max_prompt_bytes,
        allowed_urls=prompt_data_urls(view, plan, *_rejected_values(rejected)),
    )
    return packet


def build_artifact_review_packet(
    view: InputView,
    plan: dict[str, Any],
    metadata: dict[str, Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    *,
    max_prompt_bytes: int = MAX_RENDERED_PROMPT_BYTES,
    rejected: RejectedReviewResponse | None = None,
) -> PromptPacket:
    """Render an artifact-review prompt with exact candidate evidence.

    ``rejected`` is the earlier response to this same review that failed its
    checks; the prompt then adds it and its findings after the usual sections.
    """

    context = build_artifact_reviewer_context(
        view, plan, metadata, inventory, runtime_contract, rejected=rejected
    )
    payload = {
        "interface": AUTHORING_INTERFACE_VERSION_V2,
        "stage": "artifact_review",
        **context,
        **multi_turn_payload(view, runtime_contract, plan),
    }
    assert_no_prompt_secrets(payload)
    sections: list[tuple[str, Any]] = [
        (
            "ORIGINAL SCENARIO AND AUTHORITATIVE CONTEXT",
            {
                "scenario": context["original_scenario"],
                "authoritative_context": context["authoritative_context"],
            },
        ),
        *review_sections(view, metadata, runtime_contract, plan),
        ("PLAN FIELD MEANINGS", context["plan_field_meanings"]),
        ("ACCEPTED PLAN", context["accepted_plan"]),
        ("REVIEW QUESTIONS", context["review_questions"]),
        ("BINDING AND SETUP RULES", context["binding_and_setup_rules"]),
        ("RUNTIME CAPABILITIES", context["runtime_contract"]),
    ]
    sections.extend(
        (
            ("CANDIDATE ARTIFACT", context["candidate_artifact"]),
            (
                "RESOLVED JUDGE FACTS AND BINDING DECLARATIONS",
                context["resolved_runtime_context"],
            ),
            ("REVIEW RESPONSE CONTRACT", context["response_contract"]),
            ("BOUNDED ACCEPTANCE EXAMPLES", context["acceptance_examples"]),
        )
    )
    sections.extend(_rejected_review_response_prompt_sections(context))
    packet = PromptPacket(
        stage="artifact_review",
        version=ARTIFACT_REVIEW_PROMPT_VERSION,
        system=_ARTIFACT_REVIEW_SYSTEM_V3,
        user=_render_sections(tuple(sections)),
        payload=payload,
    )
    _enforce_prompt_size(
        packet,
        max_prompt_bytes,
        allowed_urls=prompt_data_urls(view, plan, metadata, *_rejected_values(rejected)),
    )
    return packet


def _rejected_values(rejected: RejectedReviewResponse | None) -> tuple[Any, ...]:
    """Return the rejected response and its findings, whose URLs the prompt may show."""

    return () if rejected is None else (rejected.raw, *rejected.findings)


def _review_finding_to_finding(record: dict[str, Any], stage: str) -> Finding:
    """Convert one complete reviewer finding into a typed stage finding."""

    detail = (
        f"Question {record.get('question', '')}; {record.get('location', '')}: "
        f"{record.get('problem', '')} "
        f"Basis: {record.get('basis', '')} Required change: "
        f"{record.get('required_change', '')}"
    )
    if stage != "plan":
        return Finding("semantic_review", detail, stage, stage=stage)
    # Plan corrections use the pointer and required change to list the
    # documented choices for a binding the finding concerns.
    return Finding(
        "semantic_review",
        detail,
        stage,
        details={
            "review_location": str(record.get("location", "")),
            "review_required_change": str(record.get("required_change", "")),
        },
        stage=stage,
    )


def review_packet_candidate(packet: PromptPacket) -> Any:
    """Return a copy of the candidate a review packet judges, as its digest hashes it."""

    key = "candidate_plan" if packet.stage == "plan_review" else "candidate_artifact"
    return deepcopy(packet.payload.get(key, {}))


def _review_packet_digests(packet: PromptPacket) -> tuple[str, str]:
    """Pin the source projection and exact candidate bytes judged by a review."""

    if packet.stage == "plan_review":
        input_payload = {
            key: value for key, value in packet.payload.items() if key != "candidate_plan"
        }
        candidate_payload = packet.payload.get("candidate_plan", {})
        candidate_digest = _sha256(_canonical_json(candidate_payload).encode("utf-8"))
    else:
        input_payload = {
            key: value for key, value in packet.payload.items() if key != "candidate_artifact"
        }
        candidate_payload = packet.payload.get("candidate_artifact", {})
        candidate_digest = _sha256(_canonical_json(candidate_payload).encode("utf-8"))
    input_digest = _sha256(
        _canonical_json(
            {
                "version": packet.version,
                "system": packet.system,
                "payload": input_payload,
            }
        ).encode("utf-8")
    )
    return input_digest, candidate_digest


def _review_contract_digest(packet: PromptPacket) -> str:
    """Digest the exact reviewer response contract supplied to the provider."""

    contract = packet.payload.get("response_contract")
    return _sha256(_canonical_json(contract).encode("utf-8"))


def _review_configuration_digest(
    controls: dict[str, Any],
    policy: dict[str, Any] | None,
) -> str:
    """Digest reviewer controls and policy without retaining endpoint material."""

    configuration = {
        "controls": redact_metadata(controls),
        "policy": redact_metadata(policy) if policy is not None else None,
    }
    return _sha256(_canonical_json(configuration).encode("utf-8"))


_PLAN_REVIEW_SYSTEM = prose("review/plan_review_system")
_PLAN_REVIEW_SYSTEM_V3 = (
    _PLAN_REVIEW_SYSTEM
    + " "
    + _PLAN_REVIEW_GUIDANCE
    + " "
    + prose("review/plan_review_binding_rules")
)
_ARTIFACT_REVIEW_SYSTEM = prose("review/artifact_review_system")
_ARTIFACT_REVIEW_SYSTEM_V3 = _ARTIFACT_REVIEW_SYSTEM + " " + _ARTIFACT_REVIEW_GUIDANCE
