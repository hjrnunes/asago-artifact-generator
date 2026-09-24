from __future__ import annotations

from asago_artifact_generator.authoring import (
    PLAN_REVIEW_PROMPT_VERSION_V2,
    PLAN_REVIEW_PROMPT_VERSION_V4,
    _binding_contract,
    build_plan_review_packet,
    build_plan_reviewer_context,
)
from tests.test_versioned_prompt_roles import (
    _inventory,
    _plan,
    _runtime_contract,
    _view,
)


def test_plan_reviewer_v3_renders_shared_binding_rules_and_mechanical_checks() -> None:
    view = _view()
    inventory = _inventory()
    runtime = _runtime_contract()
    plan = _plan()

    context = build_plan_reviewer_context(view, plan, inventory, runtime)
    packet = build_plan_review_packet(view, plan, inventory, runtime)

    assert packet.version == PLAN_REVIEW_PROMPT_VERSION_V4
    assert (
        "Apply BINDING AND SETUP RULES when interpreting runtime_bindings, "
        "setup_recipe, and setup_permissions"
    ) in packet.system
    assert "MECHANICAL CHECK SUMMARY lists structural properties already verified by code." in (
        packet.system
    )
    assert packet.user.index("PLAN FIELD MEANINGS") < packet.user.index("BINDING AND SETUP RULES")
    assert packet.user.index("BINDING AND SETUP RULES") < packet.user.index(
        "NEUTRAL OUTCOME EXAMPLE"
    )
    assert context["binding_and_setup_rules"]["binding_contract"] == _binding_contract()
    assert context["binding_and_setup_rules"]["binding_contract"]["source_kind_meanings"] == {
        "supplied_input": (
            "The value comes from a supplied environment inventory fact named by "
            "source_ref facts:<ref>; selector paths start at value. supplied_input "
            "does not mean the user message, the stimulus, or the scenario input payload."
        ),
        "setup_output": (
            "The value comes from the result of a setup operation named by source_ref "
            "setup:<operation>, which must be listed in runtime_contract.setup_permissions; "
            "selector paths start at result."
        ),
    }
    assert context["binding_and_setup_rules"]["binding_contract"]["applicability"] == (
        "runtime_bindings is [] (an empty list) only when no consumer needs a bound "
        "value: no stimulus placeholder, prerequisite, detector value, or setup argument "
        "uses one. Every prerequisite needs a declared binding. Filling a "
        "{{binding_name}} stimulus placeholder from a declared binding is a valid "
        "substitution, not circular; do not add a binding that only copies concrete "
        "stimulus text back into the stimulus."
    )
    assert context["binding_and_setup_rules"]["setup_permissions_explanation"] in packet.user
    summary = context["mechanical_check_summary"]
    assert summary["status"] == "passed"
    assert summary["checks"]
    assert summary["reviewer_instruction"] in packet.user
    assert "wrong record, field, actor, or value" in summary["reviewer_instruction"]
    assert '"checks": [' in packet.user


def test_sealed_plan_reviewer_v2_uses_legacy_prompt_shape() -> None:
    packet = build_plan_review_packet(
        _view(),
        _plan(),
        _inventory(),
        _runtime_contract(),
        sealed_version=PLAN_REVIEW_PROMPT_VERSION_V2,
    )

    assert packet.version == PLAN_REVIEW_PROMPT_VERSION_V2
    assert "BINDING AND SETUP RULES" not in packet.user
    assert '"checks": [' not in packet.user
    assert "Structural validation passed." in packet.user
