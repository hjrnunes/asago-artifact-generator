from __future__ import annotations

from asago_artifact_generator.authoring.contracts import _binding_contract
from asago_artifact_generator.authoring.review import (
    build_plan_review_packet,
    build_plan_reviewer_context,
)

from .support import world_builders

_inventory, _plan, _runtime_contract, _view = world_builders(
    "ehr", "inventory", "plan", "runtime_contract", "view"
)


def test_plan_reviewer_v3_renders_the_shared_binding_rules() -> None:
    view = _view()
    inventory = _inventory()
    runtime = _runtime_contract()
    plan = _plan()

    context = build_plan_reviewer_context(view, plan, inventory, runtime)
    packet = build_plan_review_packet(view, plan, inventory, runtime)

    assert (
        "Apply BINDING AND SETUP RULES when interpreting runtime_bindings, "
        "setup_recipe, and setup_permissions"
    ) in packet.system
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
        "value: no stimulus placeholder, prerequisite, setup argument, or value the "
        "semantic judge reads uses one. Every prerequisite needs a declared binding. "
        "Filling a {{binding_name}} stimulus placeholder from a declared binding is a valid "
        "substitution, not circular; do not add a binding that only copies concrete "
        "stimulus text back into the stimulus."
    )
    assert context["binding_and_setup_rules"]["setup_permissions_explanation"] in packet.user
