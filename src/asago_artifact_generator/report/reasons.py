"""One-line meanings of the terminal reasons an authoring run records.

A terminal reason is the code of the run's last terminal finding or, without
one, the run status. ``tests/test_report_reasons.py`` fails when the authoring
code can emit a reason this table does not explain.
"""

from __future__ import annotations

_STATUSES = {
    "accepted": "Both stages passed their checks and reviews, and the package was written.",
    "blocked": (
        "The plan author or plan reviewer declared the scenario impossible to test "
        "with the supplied facts."
    ),
    "failed": "The stage stopped on a problem that no correction can fix.",
    "unresolved": (
        "The stage used its allowed corrections and revisions, and its findings remain."
    ),
    "needs_plan_revision": (
        "The artifact reviewer found that the accepted plan itself must change, "
        "which artifact authoring cannot do."
    ),
    "review_unavailable": "The reviewer gave no usable response, so the review has no decision.",
    "transport_failure": "A model request failed in transport, so the stage has no response.",
    "budget_exhausted": "The run's request or token budget ran out before the stage finished.",
    "prompt_overflow": "A prompt grew past the request's context limit, so it was not sent.",
}

_RUN = {
    "authoring_crashed": "The authoring code raised an unexpected error and stopped the run.",
    "call1_failed": "The plan request returned no plan.",
    "call2_failed": "The artifact request returned no valid artifact.",
    "correction_dispatch_failed": "A correction request failed before it returned a response.",
    "correction_failed": "The correction response could not be read as a candidate.",
    "correction_limit_exhausted": (
        "The stage used its allowed correction or revision, and the next check or review "
        "still failed."
    ),
    "review_retry_limit_exhausted": (
        "The stage used its allowed review retries, and the reviewer's next response still "
        "failed its checks: it did not match the review format, or its decision "
        "contradicted its findings."
    ),
    "prompt_preflight": "A prompt failed its size or safety check, so it was not sent.",
    "response_parse_error": "The response could not be parsed as a candidate.",
    "secret_in_response": "The response contained a value shaped like a secret and was rejected.",
    "assembly_validation": "The accepted plan and artifact did not assemble into a valid package.",
    "package_write_failed": "The package could not be written to disk.",
    "tool_call_condition_missing": (
        "The scenario needs a tool-call condition from the producer, and the handoff has none."
    ),
}

_FRAMING = {
    "bare_fence": "The response used a code fence without the json tag.",
    "unsupported_fence": "The response used a fence other than one lowercase json fence.",
    "truncated_fence": "The response's json fence never closed, so the output was cut off.",
    "multiple_json_blocks": "The response had more than one json block.",
    "trailing_content": "The response had text after its JSON block, so it could not be read.",
    "ambiguous_content": "The response mixed JSON with other text, so no single object was clear.",
    "invalid_json": "The response's JSON did not parse.",
    "multiple_json_objects": "The response held more than one JSON object.",
    "json_object_required": "The response decoded to something other than one JSON object.",
}

_REVIEW = {
    "review_schema": (
        "The reviewer's response did not match the review format, so the review has no decision."
    ),
    "review_contradiction": "The reviewer accepted the candidate and also listed findings.",
    "review_truncated": (
        "The reviewer's response stopped at its completion token limit, so it was incomplete."
    ),
    "semantic_review": "The reviewer asked for a change to the candidate.",
}

_CHECKS = {
    "artifact_validation": "The artifact failed a structural check.",
    "closed_value_error": "A field uses a value outside its documented set.",
    "condition_operand_type_mismatch": (
        "A tool-call condition compares values of different types."
    ),
    "consumer_mismatch": "A binding and the prerequisite that uses it do not name each other.",
    "desired_behavior_prerequisite": (
        "The plan sets the safe behavior as a starting-state prerequisite instead of a "
        "detection criterion."
    ),
    "established_trigger_not_observation": (
        "The plan names a fact as the trigger that is not an observation the run captures."
    ),
    "example_capture_unknown_operation": (
        "An example captures a call to an operation the inventory does not list."
    ),
    "example_shape": "An example is not shaped as the contract requires.",
    "invalid_slot_name": (
        "A stimulus slot's name is not an identifier, so execution cannot fill it."
    ),
    "metadata_type_error": "The artifact response is not a JSON object.",
    "missing_field": "A required field is missing.",
    "non_scalar_slot": "A stimulus slot uses a binding whose value is not one scalar.",
    "non_user_history": "The stimulus history contains a message that is not from the user.",
    "omission_trigger_observation_uncited": (
        "The plan's omission trigger is not cited as an observation the run captures."
    ),
    "oracle_condition_defective": "The tool-call condition cannot be evaluated.",
    "oracle_fires_on_safe_example": "The tool-call condition detects the safe example.",
    "oracle_inconclusive_on_safe_example": (
        "The tool-call condition cannot decide the safe example."
    ),
    "oracle_quiet_on_unsafe_example": "The tool-call condition misses the unsafe example.",
    "placeholder_stimulus": "The stimulus contains a bracketed placeholder instead of a message.",
    "plan_binding_validation": "A runtime binding in the plan failed a structural check.",
    "plan_conflict": "The artifact changes or resubmits a field the accepted plan owns.",
    "plan_validation": "The plan failed a structural check.",
    "planted_record_not_in_slot": "The plan names a planted record the target slot does not list.",
    "planted_text_empty": "The planted item's text is empty.",
    "planted_text_in_benign_turn": (
        "A benign turn repeats the planted text, which must reach the agent only through "
        "a tool result."
    ),
    "planted_text_too_long": "The planted text is longer than the target accepts.",
    "prerequisite_type_mismatch": "A prerequisite's expected value has the wrong JSON type.",
    "prerequisite_value_mismatch": (
        "A prerequisite's expected value differs from the value its binding resolves to."
    ),
    "response_type_error": "The plan response is not a JSON object.",
    "schema_type_mismatch": "A field's value has the wrong type for the schema.",
    "selector_through_array": "A binding selector steps into an array, which selectors cannot do.",
    "semantic_judge_question_form": "The semantic judge question is not one yes-or-no question.",
    "semantic_judge_required": (
        "The plan claims a reply, which only the semantic judge scores, but needs no judge."
    ),
    "semantic_judge_spec_required": (
        "The plan claims a reply, and the artifact gives no semantic judge specification."
    ),
    "shape_carrier_not_exposed": (
        "The indirect attack shape plants text in an operation the inventory does not expose."
    ),
    "shape_channel_unsupported": (
        "The attack shape needs a delivery the runtime contract does not list."
    ),
    "shape_delivery_mismatch": "The stimulus does not deliver the turns the attack shape plans.",
    "shape_error": "A field does not have the required shape.",
    "shape_forged_transcript_unsupported": (
        "The attack shape forges a transcript, which no runtime delivers."
    ),
    "shape_seed_slot_unavailable": (
        "The indirect attack shape needs a seed slot the runtime contract does not list."
    ),
    "shape_turn_count_mismatch": (
        "The stimulus lists a different number of turns than the attack shape plans."
    ),
    "shape_turn_count_unsupported": (
        "The attack shape plans more turns than the runtime contract supports."
    ),
    "slot_mismatch": "The stimulus slots do not match the slots its user text uses.",
    "type_error": "A field has the wrong type.",
    "undeclared_slot": "A stimulus slot names no declared runtime binding.",
    "undocumented_selector": "A binding selector is not documented by the inventory.",
    "unexpected_field": "A field appears where the contract does not allow it.",
    "unknown_binding": "A prerequisite names a binding the artifact does not declare.",
    "unknown_reference": "A reference names evidence the supplied facts do not contain.",
    "unobtainable_essential_requirement": (
        "The plan marks an essential requirement as one the run cannot obtain."
    ),
    "unpermitted_setup": "The setup recipe uses an operation that setup does not permit.",
    "unresolved_fact": "A static fact has no supplied value.",
    "unsupported_claim_level": "The plan's claim level is not one execution supports.",
}

REASON_MEANINGS: dict[str, str] = {**_STATUSES, **_RUN, **_FRAMING, **_REVIEW, **_CHECKS}
UNLISTED = "No meaning is recorded for this reason."


def reason_meaning(reason: str) -> str:
    """Return the one-line meaning of ``reason``."""

    return REASON_MEANINGS.get(reason, UNLISTED)
