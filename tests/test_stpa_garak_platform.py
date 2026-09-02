from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from asago_artifact_generator.authoring import DeterministicPresentationAuthor
from asago_artifact_generator.cli import app
from asago_artifact_generator.garak.capabilities import garak_capabilities
from asago_artifact_generator.garak.compile import (
    GARAK_ARTIFACT_SCHEMA_VERSION,
    _compare_observed_value,
    compile_execution_artifact,
    compile_garak_artifact,
    validate_garak_artifact,
)
from asago_artifact_generator.models._base import compute_framed_digest
from asago_artifact_generator.models.readiness import (
    ExecutionPlanResult,
    ObserverPlan,
    PlanStep,
    ReadyExecutionPlan,
)
from asago_artifact_generator.planning.bind import bind_and_plan
from asago_artifact_generator.platforms.base import ArtifactValidationError
from asago_artifact_generator.trace import ObservationReceipt


def test_garak_capabilities_are_honest_about_supported_observation() -> None:
    capabilities = garak_capabilities()

    assert capabilities.platform == "garak"
    assert {"system_prompt", "user_turn", "tool_result"} <= set(capabilities.writable_surfaces)
    assert {"tool_call", "tool_argument", "output_text"} <= set(capabilities.observer_kinds)
    assert capabilities.supports_real_clock is False
    assert capabilities.supports_persistent_state is False
    assert capabilities.supports_multi_agent is False


def test_real_garak_capabilities_reach_ready_and_compile() -> None:
    from tests.test_stpa_consumer_core import _bindings, _intent

    intent = _intent(placeholder=True)
    result = bind_and_plan(intent, _bindings(intent), garak_capabilities())

    assert result.overall == "ready"
    assert result.plan is not None
    compiled = compile_execution_artifact(
        result.plan,
        DeterministicPresentationAuthor(
            {"content:S-1": "Review the transaction context before authorizing."}
        ),
    )
    assert compiled.artifact["injection_surface"] == "tool_call"


def test_ready_plan_compiles_to_bound_garak_artifact_and_trace() -> None:
    ready = ReadyExecutionPlan(
        bundle_digest="b" * 64,
        projection_semantic_digest="c" * 64,
        scenario_content_sha256="d" * 64,
        projection_content_sha256="e" * 64,
        binding_set_id="BIND-1",
        binding_set_digest="d" * 64,
        run_id="run-1",
        scenario_id="SCN-001",
        candidate_id="EXEC:CL-1:CM-1:INCORRECT",
        ica_slot_id="CL-1:CM-1:INCORRECT",
        ica_id="CL-1:CM-1:INCORRECT:1",
        controller_id="CL-1",
        control_action_id="CM-1",
        uca_type="INCORRECT",
        platform="garak",
        adapter_version="garak-stpa-v1",
        steps=(
            PlanStep(
                plan_step_id="plan-1",
                projection_step_id="S-1",
                order=1,
                kind="CAUSAL_FACTOR",
                factor_id="CF-1",
                structural_source_id="PM-1-1",
                surface="user_turn",
                locator="conversation.input",
                content_slot_id="slot-1",
            ),
            PlanStep(
                plan_step_id="plan-2",
                projection_step_id="S-2",
                order=2,
                kind="UNSAFE_CONTROL_ACTION",
                structural_source_id="CM-1",
                surface="tool_call",
                locator="trace.tool_calls",
                control_action_id="CM-1",
                adapter_operation="tool_call",
                tool_name="authorize_transaction",
                tool_schema={"type": "object", "properties": {"account_id": {"type": "string"}}},
                safe_arguments={"account_id": "TEST-ACCOUNT-001"},
            ),
        ),
        observers=(
            ObserverPlan(
                condition_ref="OUTCOME-1",
                condition_type="action_value",
                observer_kind="tool_argument",
                event_source="trace.tool_calls",
                semantic_property="authorized_destination",
                field_path="arguments.account_id",
                comparison="not_equals",
                expected_from="semantic_binding:SEM-1",
                expected="AUTHORIZED-ACCOUNT-001",
            ),
        ),
        content_slots=("slot-1",),
    )

    compiled = compile_garak_artifact(
        ready,
        prebound_texts={"slot-1": "Review the transaction context before authorizing."},
    )

    artifact = compiled.artifact
    assert artifact["scenario_id"] == "SCN-001"
    assert [turn["source_step_id"] for turn in artifact["turns"] if turn["source_step_id"]] == [
        "S-1",
        "S-2",
    ]
    assert artifact["turns"][-1]["tool_calls"][0]["function"]["name"] == ("authorize_transaction")
    assert artifact["detectors"][0]["condition_ref"] == "OUTCOME-1"
    assert artifact["artifact_trace"]["step_map"][1]["projection_step_id"] == "S-2"
    assert compiled.validation["ok"] is True


def test_public_compiler_rejects_unready_result() -> None:
    result = ExecutionPlanResult(
        source_status="valid",
        semantic_binding_status="incomplete",
        runtime_binding_status="incomplete",
        platform_support_status="supported",
        overall="needs_runtime_binding",
    )

    with pytest.raises(TypeError, match="ReadyExecutionPlan"):
        compile_execution_artifact(result)  # type: ignore[arg-type]


def test_unready_result_makes_no_author_call_or_artifact() -> None:
    result = ExecutionPlanResult(
        source_status="valid",
        semantic_binding_status="incomplete",
        runtime_binding_status="incomplete",
        platform_support_status="supported",
        overall="needs_runtime_binding",
    )
    calls = 0

    class FailingAuthor:
        def author(self, request: object) -> object:
            nonlocal calls
            calls += 1
            raise AssertionError("authoring must not begin before readiness")

    with pytest.raises(TypeError, match="ReadyExecutionPlan"):
        compile_execution_artifact(result, FailingAuthor())  # type: ignore[arg-type]

    assert calls == 0


@pytest.mark.parametrize("texts", [{}, {"slot-1": "ok", "extra": "not allowed"}])
def test_compiler_rejects_missing_or_extra_presentation_slots(texts: dict[str, str]) -> None:
    ready = _ready_plan()

    with pytest.raises((ValueError, ArtifactValidationError), match="slot"):
        compile_garak_artifact(ready, prebound_texts=texts)


def test_compiler_rejects_unbound_tool_argument_placeholder() -> None:
    ready = _ready_plan()
    steps = list(ready.steps)
    steps[-1] = steps[-1].model_copy(
        update={"safe_arguments": {"account_id": {"binding_ref": "UNBOUND"}}}
    )
    mutated = ready.model_copy(update={"steps": tuple(steps)})

    with pytest.raises(ArtifactValidationError, match="unbound argument"):
        compile_garak_artifact(mutated, prebound_texts={"slot-1": "bound text"})


def test_compiler_rejects_tool_arguments_outside_bound_json_schema() -> None:
    ready = _ready_plan()
    steps = list(ready.steps)
    steps[-1] = steps[-1].model_copy(update={"safe_arguments": {"account_id": 42}})
    mutated = ready.model_copy(update={"steps": tuple(steps)})

    with pytest.raises(ArtifactValidationError, match="bound tool schema"):
        compile_garak_artifact(mutated, prebound_texts={"slot-1": "bound text"})


def test_invalid_tool_schema_makes_no_author_call() -> None:
    ready = _ready_plan()
    steps = list(ready.steps)
    steps[-1] = steps[-1].model_copy(
        update={"tool_schema": {"type": "object", "format": "unsupported"}}
    )
    mutated = ready.model_copy(update={"steps": tuple(steps)})
    calls = 0

    class FailingAuthor:
        def author(self, request: object) -> object:
            nonlocal calls
            calls += 1
            raise AssertionError("invalid plans must fail before authoring")

    with pytest.raises(ArtifactValidationError, match="unsupported JSON Schema keyword"):
        compile_garak_artifact(mutated, FailingAuthor())

    assert calls == 0


def test_compiler_rejects_unsupported_bound_tool_schema_keyword() -> None:
    ready = _ready_plan()
    steps = list(ready.steps)
    steps[-1] = steps[-1].model_copy(
        update={
            "tool_schema": {
                "type": "object",
                "patternProperties": {"^account_": {"type": "string"}},
            }
        }
    )
    mutated = ready.model_copy(update={"steps": tuple(steps)})

    with pytest.raises(ArtifactValidationError, match="unsupported JSON Schema keyword"):
        compile_garak_artifact(mutated, prebound_texts={"slot-1": "bound text"})


def test_compiler_rejects_unbound_observer_value() -> None:
    ready = _ready_plan()
    mutated = ready.model_copy(
        update={
            "observers": (
                ready.observers[0].model_copy(update={"expected": {"binding_ref": "UNBOUND"}}),
            )
        }
    )

    with pytest.raises(ArtifactValidationError, match="unbound expected"):
        compile_garak_artifact(mutated, prebound_texts={"slot-1": "bound text"})


def test_compiler_rejects_safe_bound_tool_argument() -> None:
    ready = _ready_plan()
    steps = list(ready.steps)
    steps[-1] = steps[-1].model_copy(
        update={"safe_arguments": {"account_id": "AUTHORIZED-ACCOUNT-001"}}
    )
    mutated = ready.model_copy(update={"steps": tuple(steps)})

    with pytest.raises(ArtifactValidationError, match="safe binding"):
        compile_garak_artifact(mutated, prebound_texts={"slot-1": "bound text"})


@pytest.mark.parametrize(
    ("actual", "operator", "expected", "matches"),
    [
        ("same", "equals", "same", True),
        ("same", "not_equals", "other", True),
        ("abc", "contains", "b", True),
        ("abc", "not_contains", "z", True),
        (3, "greater_than", 2, True),
        (3, "greater_than_or_equal", 3, True),
        (2, "less_than", 3, True),
        (2, "less_than_or_equal", 2, True),
        ("abc", "unknown", "abc", False),
        ("abc", "greater_than", 2, False),
    ],
)
def test_observer_comparison_operators_are_closed(
    actual: object, operator: str, expected: object, matches: bool
) -> None:
    assert _compare_observed_value(actual, operator, expected) is matches


def test_validator_rejects_tool_declaration_and_trace_closure_tampering() -> None:
    compiled = compile_garak_artifact(
        _ready_plan(),
        prebound_texts={"slot-1": "Review the transaction context before authorizing."},
    )
    tampered = json.loads(json.dumps(compiled.artifact))
    tampered["tools"] = []
    tampered["artifact_trace"]["step_map"].reverse()

    errors = validate_garak_artifact(tampered)

    assert any("used but not declared" in error for error in errors)
    assert any("step_map" in error for error in errors)


def test_validator_rejects_rehashed_tool_arguments_outside_bound_schema() -> None:
    compiled = compile_garak_artifact(
        _ready_plan(),
        prebound_texts={"slot-1": "Review the transaction context before authorizing."},
    )
    tampered = json.loads(json.dumps(compiled.artifact))
    tampered["turns"][-1]["tool_calls"][0]["function"]["arguments"]["account_id"] = 42
    tampered["artifact_digest"] = compute_framed_digest(
        GARAK_ARTIFACT_SCHEMA_VERSION,
        {key: value for key, value in tampered.items() if key != "artifact_digest"},
    )

    errors = validate_garak_artifact(tampered)

    assert any("bound JSON schema" in error for error in errors)


def test_validator_closes_rehashed_trace_to_ready_plan_authority() -> None:
    ready = _ready_plan()
    compiled = compile_garak_artifact(
        ready,
        prebound_texts={"slot-1": "Review the transaction context before authorizing."},
    )
    tampered = json.loads(json.dumps(compiled.artifact))
    tampered["candidate_id"] = "EXEC:FORGED"
    tampered["artifact_trace"]["source"]["candidate_id"] = "EXEC:FORGED"
    tampered["binding_set_digest"] = "e" * 64
    tampered["artifact_trace"]["binding"]["semantic_digest"] = "e" * 64
    tampered["adapter_version"] = "garak-forged-v1"
    tampered["artifact_trace"]["compiler"]["adapter_version"] = "garak-forged-v1"
    tampered["tools"][0]["function"]["parameters"]["properties"]["account_id"]["type"] = "integer"
    tampered["turns"][-1]["tool_calls"][0]["function"]["arguments"]["account_id"] = 42
    trace = tampered["artifact_trace"]
    trace["trace_digest"] = compute_framed_digest(
        trace["schema_version"],
        {key: value for key, value in trace.items() if key != "trace_digest"},
    )
    tampered["artifact_digest"] = compute_framed_digest(
        GARAK_ARTIFACT_SCHEMA_VERSION,
        {key: value for key, value in tampered.items() if key != "artifact_digest"},
    )

    errors = validate_garak_artifact(tampered, ready)

    assert any(
        "candidate_id differs from ReadyExecutionPlan authority" in error for error in errors
    )
    assert any(
        "binding_set_digest differs from ReadyExecutionPlan authority" in error for error in errors
    )
    assert any(
        "adapter_version differs from ReadyExecutionPlan authority" in error for error in errors
    )
    assert any(
        "artifact tools differ from ReadyExecutionPlan authority" in error for error in errors
    )
    assert any(
        "tool arguments for plan-2 differ from ReadyExecutionPlan authority" in error
        for error in errors
    )


def test_cli_normal_path_writes_plan_artifact_validation_trace_and_manifest(
    tmp_path, monkeypatch
) -> None:
    ready = _ready_plan()
    readiness = ExecutionPlanResult(
        source_status="valid",
        semantic_binding_status="not_required",
        runtime_binding_status="complete",
        platform_support_status="supported",
        overall="ready",
        plan=ready,
    )
    bundle_entry = SimpleNamespace(scenario_id=ready.scenario_id, intent=object())
    verified = SimpleNamespace(
        entries=(bundle_entry,),
        run_id=ready.run_id,
        bundle_digest=ready.bundle_digest,
    )
    monkeypatch.setattr(
        "asago_artifact_generator.bundle.loader.load_execution_bundle",
        lambda path: verified,
    )
    monkeypatch.setattr(
        "asago_artifact_generator.planning.bind.bind_and_plan",
        lambda intent, bindings, capabilities: readiness,
    )
    monkeypatch.setattr(
        "asago_artifact_generator.cli._LLMPresentationAuthor",
        lambda: DeterministicPresentationAuthor(
            {"slot-1": "Review the transaction context before authorizing."}
        ),
    )

    bundle_path = tmp_path / "execution-bundle.json"
    bundle_path.write_text("{}", encoding="utf-8")
    output_dir = tmp_path / "runs"
    result = CliRunner().invoke(
        app,
        [
            "generate",
            "--bundle",
            str(bundle_path),
            "--output-dir",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0, result.stdout
    entry_dir = output_dir / ready.run_id / ready.scenario_id
    assert (entry_dir / "readiness.json").is_file()
    assert (entry_dir / "execution-plan.json").is_file()
    assert (entry_dir / f"{ready.scenario_id}-garak.json").is_file()
    assert (entry_dir / "validation.json").is_file()
    assert (entry_dir / "artifact-trace.json").is_file()
    manifest = json.loads((output_dir / ready.run_id / "artifact-manifest.json").read_text())
    assert manifest["entries"][0]["artifact_status"] == "generated"


def test_cli_readiness_only_never_constructs_author_or_compiler(tmp_path, monkeypatch) -> None:
    ready = _ready_plan()
    readiness = ExecutionPlanResult(
        source_status="valid",
        semantic_binding_status="not_required",
        runtime_binding_status="complete",
        platform_support_status="supported",
        overall="ready",
        plan=ready,
    )
    bundle_entry = SimpleNamespace(scenario_id=ready.scenario_id, intent=object())
    verified = SimpleNamespace(
        entries=(bundle_entry,),
        run_id=ready.run_id,
        bundle_digest=ready.bundle_digest,
    )
    monkeypatch.setattr(
        "asago_artifact_generator.bundle.loader.load_execution_bundle",
        lambda path: verified,
    )
    monkeypatch.setattr(
        "asago_artifact_generator.planning.bind.bind_and_plan",
        lambda intent, bindings, capabilities: readiness,
    )

    def fail_if_constructed() -> object:
        raise AssertionError("readiness-only must not construct a presentation author")

    def fail_if_compiled(*args: object, **kwargs: object) -> object:
        raise AssertionError("readiness-only must not invoke the compiler")

    monkeypatch.setattr("asago_artifact_generator.cli._LLMPresentationAuthor", fail_if_constructed)
    monkeypatch.setattr(
        "asago_artifact_generator.garak.compile.compile_execution_artifact",
        fail_if_compiled,
    )

    bundle_path = tmp_path / "execution-bundle.json"
    bundle_path.write_text("{}", encoding="utf-8")
    output_dir = tmp_path / "runs"
    result = CliRunner().invoke(
        app,
        [
            "generate",
            "--bundle",
            str(bundle_path),
            "--output-dir",
            str(output_dir),
            "--readiness-only",
        ],
    )

    assert result.exit_code == 0, result.stdout
    entry_dir = output_dir / ready.run_id / ready.scenario_id
    assert (entry_dir / "readiness.json").is_file()
    assert (entry_dir / "execution-plan.json").is_file()
    assert not list(entry_dir.glob("*-garak.json"))
    manifest = json.loads((output_dir / ready.run_id / "artifact-manifest.json").read_text())
    assert manifest["entries"][0]["artifact_status"] == "not_attempted"


def test_observation_receipt_is_append_only_and_digest_verified() -> None:
    receipt = ObservationReceipt(
        artifact_digest="a" * 64,
        projection_semantic_digest="b" * 64,
        binding_set_digest="c" * 64,
        execution_id="execution-1",
        observations=({"event": {"name": "tool_call"}},),
        oracle_result="inconclusive",
    )

    receipt.verify()
    with pytest.raises(TypeError):
        receipt.observations[0]["event"]["name"] = "tampered"  # type: ignore[index]


def test_cli_entry_failure_is_retained_in_manifest(tmp_path, monkeypatch) -> None:
    ready = _ready_plan()
    bundle_entry = SimpleNamespace(scenario_id=ready.scenario_id, intent=object())
    verified = SimpleNamespace(
        entries=(bundle_entry,),
        run_id=ready.run_id,
        bundle_digest=ready.bundle_digest,
    )
    monkeypatch.setattr(
        "asago_artifact_generator.bundle.loader.load_execution_bundle",
        lambda path: verified,
    )

    def fail_before_readiness(intent, bindings, capabilities):
        raise RuntimeError("typed planning failed")

    monkeypatch.setattr(
        "asago_artifact_generator.planning.bind.bind_and_plan",
        fail_before_readiness,
    )
    result = CliRunner().invoke(
        app,
        [
            "generate",
            "--bundle",
            str(tmp_path / "execution-bundle.json"),
            "--output-dir",
            str(tmp_path / "runs"),
        ],
    )

    assert result.exit_code == 1
    manifest = json.loads(
        (tmp_path / "runs" / ready.run_id / "artifact-manifest.json").read_text()
    )
    assert manifest["entries"][0]["scenario_id"] == ready.scenario_id
    assert "typed planning failed" in manifest["entries"][0]["errors"]


def _ready_plan() -> ReadyExecutionPlan:
    return ReadyExecutionPlan(
        bundle_digest="b" * 64,
        projection_semantic_digest="c" * 64,
        scenario_content_sha256="d" * 64,
        projection_content_sha256="e" * 64,
        binding_set_id="BIND-1",
        binding_set_digest="d" * 64,
        run_id="run-1",
        scenario_id="SCN-001",
        candidate_id="EXEC:CL-1:CM-1:INCORRECT",
        ica_slot_id="CL-1:CM-1:INCORRECT",
        ica_id="CL-1:CM-1:INCORRECT:1",
        controller_id="CL-1",
        control_action_id="CM-1",
        uca_type="INCORRECT",
        platform="garak",
        adapter_version="garak-stpa-v1",
        steps=(
            PlanStep(
                plan_step_id="plan-1",
                projection_step_id="S-1",
                order=1,
                kind="CAUSAL_FACTOR",
                factor_id="CF-1",
                structural_source_id="PM-1-1",
                surface="user_turn",
                locator="conversation.input",
                content_slot_id="slot-1",
            ),
            PlanStep(
                plan_step_id="plan-2",
                projection_step_id="S-2",
                order=2,
                kind="UNSAFE_CONTROL_ACTION",
                structural_source_id="CM-1",
                surface="tool_call",
                locator="trace.tool_calls",
                control_action_id="CM-1",
                adapter_operation="tool_call",
                tool_name="authorize_transaction",
                tool_schema={"type": "object", "properties": {"account_id": {"type": "string"}}},
                safe_arguments={"account_id": "TEST-ACCOUNT-001"},
            ),
        ),
        observers=(
            ObserverPlan(
                condition_ref="OUTCOME-1",
                condition_type="action_value",
                observer_kind="tool_argument",
                event_source="trace.tool_calls",
                semantic_property="authorized_destination",
                field_path="arguments.account_id",
                comparison="not_equals",
                expected_from="semantic_binding:SEM-1",
                expected="AUTHORIZED-ACCOUNT-001",
            ),
        ),
        content_slots=("slot-1",),
    )
