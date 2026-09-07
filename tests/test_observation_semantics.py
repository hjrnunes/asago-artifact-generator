"""Runtime adapter choices cannot change what the producer says is observed."""

from asago_artifact_generator.planning.bind import bind_and_plan
from tests.test_stpa_consumer_core import _bindings, _capabilities, _execution_case, _intent


def test_internal_message_cannot_use_a_final_answer_observer():
    from asago_artifact_generator.models.execution_case import BoundExecutionCase
    from asago_artifact_generator.models.execution_classification import (
        ExecutionTargetProfile,
        SemanticExecutionContract,
        SimulationBehavior,
        TargetProfileOperation,
        TargetProfileResource,
    )
    from asago_artifact_generator.planning.resolve_case import resolve_execution_case
    from tests.test_execution_case import (
        _agent_requirement,
        _direct_contract,
        _intent_with_contract,
        _parameterized_intent,
    )

    contract = SemanticExecutionContract(
        requested_environment_basis="simulation_profile",
        delivery=_direct_contract().delivery,
        action_kind="agent_message",
        resource_requirements=(_agent_requirement(),),
    )
    profile = ExecutionTargetProfile(
        target_id="neutral-channel",
        authorization_scope_id="offline-test",
        basis="simulation",
        semantic_authority="reviewed",
        inventory_completeness="unknown",
        source_protocol="simulation",
        resources=(
            TargetProfileResource(
                resource_id="channel-1",
                resource_kind="agent_channel",
                role_ids=("agent_channel",),
                structural_refs=("agent",),
                surfaces=("agent_message",),
                operations=(
                    TargetProfileOperation(
                        operation_id="send_message", semantic_operation="send_message"
                    ),
                ),
                simulation_behavior=SimulationBehavior(
                    inputs={"text": "settings"},
                    outputs={"text": "settings"},
                    observation_points=("messages",),
                ),
                evidence_refs=("neutral-channel-contract",),
            ),
        ),
    )
    intent = _parameterized_intent(contract)
    intent = _intent_with_contract(contract, intent.execution_classification, profile=profile)
    case = resolve_execution_case(intent, profile)
    assert isinstance(case, BoundExecutionCase), case.diagnostics
    capabilities = _capabilities().model_copy(
        update={
            "writable_surfaces": ("user_turn", "agent_message"),
            "supports_multi_agent": True,
        }
    )
    result = bind_and_plan(case, _bindings(intent, target_surface="agent_message"), capabilities)
    assert result.plan is None
    assert {"action_semantics_mismatch", "observation_boundary_mismatch"} <= {
        item.code for item in result.diagnostics
    }


def test_model_answer_cannot_be_rebound_as_a_tool_argument():
    intent = _intent()
    result = bind_and_plan(
        _execution_case(intent),
        _bindings(intent, adapter_operation="tool_call", target_surface="tool_call"),
        _capabilities(),
    )
    assert result.plan is None
    assert "action_semantics_mismatch" in {item.code for item in result.diagnostics}


def test_binding_cannot_reverse_the_producer_comparison():
    from asago_artifact_generator.models.runtime_binding import RuntimeBindingSet

    intent = _intent()
    original = _bindings(intent)
    payload = original.model_dump(mode="json")
    payload.pop("semantic_digest")
    payload.pop("schema_version")
    payload["observation_bindings"][0]["comparison"] = "not_equals"
    bindings = RuntimeBindingSet.create(**payload)
    result = bind_and_plan(_execution_case(intent), bindings, _capabilities())
    assert result.plan is None
    assert "observer_comparison_mismatch" in {item.code for item in result.diagnostics}


def test_tool_observer_cannot_read_an_unrelated_argument():
    from asago_artifact_generator.garak.capabilities import garak_capabilities
    from asago_artifact_generator.garak.default_bindings import complete_garak_runtime_bindings
    from asago_artifact_generator.models.runtime_binding import ObservationBinding
    from asago_artifact_generator.planning.resolve_case import resolve_execution_case
    from tests.test_target_profile_consumer import (
        _exact_contract,
        _intent_with_contract,
        _mcp_profile,
    )

    profile = _mcp_profile()
    case = resolve_execution_case(
        _intent_with_contract(_exact_contract(), profile=profile), profile
    )
    bindings = complete_garak_runtime_bindings(case, target_profile=profile)
    outcome = case.intent.unsafe_outcome
    observer = ObservationBinding(
        condition_ref=outcome.outcome_id,
        observer_kind="tool_argument",
        event_source="trace.tool_calls",
        semantic_property=outcome.condition.property,
        field_path="arguments.unrelated",
        comparison=outcome.condition.operator,
        expected_from="projection",
    )
    bindings = bindings.model_copy(
        update={"observation_bindings": (observer,)}
    ).with_computed_digest()
    result = bind_and_plan(case, bindings, garak_capabilities())
    assert result.plan is None
    assert "observer_field_path_mismatch" in {item.code for item in result.diagnostics}
