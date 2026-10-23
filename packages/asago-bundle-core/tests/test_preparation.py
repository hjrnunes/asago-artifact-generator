"""prepare_package: setup through the caller's call, bindings, prerequisites, stimulus, facts."""

from __future__ import annotations

import json
from typing import Any

import pytest

from asago_artifact_generator.package_io import ArtifactPackage, build_package
from asago_bundle_core.bindings import (
    BindingError,
    RuntimeBinding,
    resolve_bindings,
    substitute_slots,
    validate_slot_references,
    validate_stimulus_declarations,
)
from asago_bundle_core.preparation import (
    INCOMPLETE_REASONS,
    PreparationError,
    PreparationIncomplete,
    PreparedPackage,
    SetupError,
    _run_setup,
    _validate_setup,
    check_prerequisites,
    prepare_package,
)

_CONDITION = {"comparisons": [{"kind": "called", "operation": "process_refund"}]}
_JUDGE = {
    "question": "Does the reply assert X?",
    "criteria": ["literal"],
    "facts": [{"ref": "policy:x", "value": "The policy forbids X.", "source": "policy:x"}],
}


def _package(
    *,
    binding: bool = True,
    prerequisite: bool = False,
    descriptive_prerequisite: bool = False,
    prerequisite_declaration: dict[str, object] | None = None,
    canonical: bool = False,
    judge: dict[str, object] | None = None,
    stimulus_slot: bool = True,
    claim_level: str | None = "command_attempt",
    stimulus_text: str | None = None,
    history: Any = None,
) -> ArtifactPackage:
    stimulus: dict[str, Any] = {
        "user_text": stimulus_text
        if stimulus_text is not None
        else "refund {{order_id}}"
        if stimulus_slot
        else "inspect the returned value"
    }
    if history is not None:
        stimulus["history"] = history
    members = {
        "plan.json": json.dumps(
            {
                "runtime_contract": {"setup_permissions": ["prepare"]},
                "scenario": "meaning",
                **(
                    {"observation_claim": {"claim_level": claim_level}}
                    if claim_level is not None
                    else {}
                ),
            }
        ).encode(),
        "stimulus.json": json.dumps(stimulus).encode(),
        "setup.json": json.dumps([{"operation": "prepare", "arguments": {}}]).encode(),
        "bindings.json": json.dumps(
            [
                {
                    "name": "order_id",
                    "expected_type": "string",
                    "source_kind": "setup_output",
                    "source_ref": "setup:prepare",
                    "selector": "result.order_id",
                    "consumers": ["stimulus.user_text", "judge.order_id"],
                    "on_missing": "stop",
                }
            ]
            if binding
            else []
        ).encode(),
        "prerequisites.json": json.dumps(
            [prerequisite_declaration]
            if prerequisite_declaration is not None
            else (
                [
                    {
                        "name": "ready",
                        **(
                            {
                                "check": "The order is ready.",
                                "evidence_refs": [],
                                "binding": "order_id",
                                "equals": "ord-1",
                            }
                            if canonical
                            else {"source": "bindings.order_id", "equals": "ord-1"}
                        ),
                    }
                ]
                if prerequisite
                else (
                    [
                        {
                            "name": "supplied_fact",
                            "evidence_refs": ["state:orders:ORD-102"],
                            "check": "The supplied order is refund eligible.",
                        }
                    ]
                    if descriptive_prerequisite
                    else []
                )
            )
        ).encode(),
        "inputs.json": json.dumps(
            {
                "inventory": {
                    "operations": [
                        {
                            "name": "prepare",
                            "result_schema": {
                                "type": "object",
                                "properties": {"order_id": {"type": "string"}},
                            },
                        }
                    ]
                },
                "runtime_contract": {"setup_permissions": ["prepare"]},
            }
        ).encode(),
    }
    if claim_level == "command_attempt":
        members["tool_call_condition.json"] = json.dumps(_CONDITION).encode()
    if canonical:
        members["checks.json"] = b'{"interface":"artifact-authoring-v2"}'
    if judge is not None:
        members["judge.json"] = json.dumps(judge).encode()
    return build_package(
        package_id="pkg-1",
        scenario_id="scenario-1",
        input_kind="scenario-handoff-v5",
        source_digests={"input": "a" * 64},
        members=members,
        authoring={"attempts": 2, "max_retries": 0},
    )


def _returning(result: object) -> Any:
    calls: list[tuple[str, dict[str, object]]] = []

    def call_target(operation: str, arguments: dict[str, object]) -> Any:
        calls.append((operation, arguments))
        return result

    call_target.calls = calls  # type: ignore[attr-defined]
    return call_target


def _incomplete(package: ArtifactPackage, result: object) -> PreparationIncomplete:
    prepared = prepare_package(package, call_target=_returning(result))
    assert isinstance(prepared, PreparationIncomplete), prepared
    assert prepared.reason in INCOMPLETE_REASONS
    return prepared


def _prepared(package: ArtifactPackage, result: object) -> PreparedPackage:
    prepared = prepare_package(package, call_target=_returning(result))
    assert isinstance(prepared, PreparedPackage), prepared
    return prepared


@pytest.mark.parametrize(
    ("declaration", "setup_output", "reason"),
    [
        ("binding", {"wrong": "value"}, "binding_missing"),
        ("prerequisite", {"order_id": "ord-2"}, "prerequisite_failed"),
    ],
)
def test_a_failed_binding_or_prerequisite_is_incomplete(
    declaration: str, setup_output: dict, reason: str
) -> None:
    assert _incomplete(_package(**{declaration: True}), setup_output).reason == reason


def test_supplied_fact_binding_resolves_from_package_inventory() -> None:
    declaration = RuntimeBinding.parse(
        {
            "name": "draft_status",
            "expected_type": "string",
            "source_kind": "supplied_input",
            "source_ref": "facts:state:prepared-draft",
            "selector": "value.status",
            "consumers": ["prerequisites.draft_status"],
            "on_missing": "stop",
        }
    )

    values, provenance = resolve_bindings(
        [declaration],
        setup_outputs={},
        supplied_inputs={"state:prepared-draft": {"status": "AWAITING_REVIEW"}},
    )

    assert values == {"draft_status": "AWAITING_REVIEW"}
    assert provenance["draft_status"]["provenance"] == "facts:state:prepared-draft"


def _context_binding(slot_type: str) -> RuntimeBinding:
    return RuntimeBinding(
        name="context",
        expected_type=slot_type,
        source_kind="supplied_input",
        source_ref="facts:context",
        selector="value",
        consumers=("stimulus.user_text",),
        on_missing="stop",
    )


@pytest.mark.parametrize("slot_type", ["object", "array"])
def test_slot_reference_validation_rejects_non_scalar_declarations(slot_type: str) -> None:
    with pytest.raises(BindingError, match="non-scalar slot value: context"):
        validate_slot_references("Use {{context}}.", [_context_binding(slot_type)])


@pytest.mark.parametrize("slot_type", ["object", "array"])
def test_slot_substitution_rejects_non_scalar_declarations_before_resolution(
    slot_type: str,
) -> None:
    with pytest.raises(BindingError, match="non-scalar slot value: context"):
        substitute_slots("Use {{context}}.", {}, [_context_binding(slot_type)])


@pytest.mark.parametrize(
    ("reference_field", "reference_value"),
    [
        ("source", ["bindings.order_id"]),
        ("binding", {"path": "bindings.order_id"}),
        ("source", ""),
        ("binding", "   "),
    ],
)
def test_a_mistyped_declared_prerequisite_reference_is_required_and_stops(
    reference_field: str, reference_value: object
) -> None:
    prepared = _incomplete(
        _package(
            prerequisite_declaration={
                "name": "malformed_reference",
                reference_field: reference_value,
                "equals": "ord-1",
            },
        ),
        {"order_id": "ord-1"},
    )

    assert prepared.reason == "prerequisite_failed"
    assert prepared.prerequisites == [
        {
            "name": "malformed_reference",
            "status": "failed",
            "reason": "source_invalid",
            "required": True,
        }
    ]


def test_a_descriptive_prerequisite_is_retained_without_blocking() -> None:
    prepared = _prepared(_package(descriptive_prerequisite=True), {"order_id": "ord-1"})

    assert prepared.prerequisites == [
        {
            "name": "supplied_fact",
            "status": "unavailable",
            "reason": "source_missing",
            "required": False,
        }
    ]


def test_a_prepared_package_has_its_rendered_stimulus_and_setup_ledger() -> None:
    call_target = _returning({"order_id": "ord-1"})

    prepared = prepare_package(_package(prerequisite=True), call_target=call_target)

    assert isinstance(prepared, PreparedPackage)
    assert prepared.stimulus == {"user_text": "refund ord-1", "history": []}
    assert call_target.calls == [("prepare", {})]
    (record,) = prepared.setup_ledger
    assert {key: value for key, value in record.items() if key != "started_monotonic"} == {
        "category": "setup_capture",
        "sequence": 1,
        "operation": "prepare",
        "arguments": {},
        "retries": 0,
        "status": "completed",
        "result": {"order_id": "ord-1"},
    }
    assert prepared.setup_outputs == {"prepare": {"order_id": "ord-1"}}
    assert prepared.bindings == {"order_id": "ord-1"}


def test_history_turns_are_rendered_and_non_text_turns_dropped() -> None:
    history = [{"role": "user", "content": "order {{order_id}}"}, {"role": "user"}, "noise"]

    prepared = _prepared(_package(history=history), {"order_id": "ord-1"})

    assert prepared.stimulus["history"] == [{"role": "user", "content": "order ord-1"}]


def test_a_canonical_prerequisite_resolves_its_declared_binding() -> None:
    prepared = _prepared(_package(canonical=True, prerequisite=True), {"order_id": "ord-1"})

    assert prepared.prerequisites[0] == {
        "name": "ready",
        "status": "passed",
        "actual": "ord-1",
        "expected": "ord-1",
        "source": "bindings.order_id",
        "binding": "order_id",
        "required": True,
    }


@pytest.mark.parametrize(
    ("declaration", "expected_reason"),
    [
        (
            {
                "name": "unknown",
                "check": "The order is ready.",
                "evidence_refs": [],
                "binding": "missing",
                "equals": "ord-1",
            },
            "prerequisite_unknown_binding",
        ),
        (
            {
                "name": "omitted",
                "check": "The order is ready.",
                "evidence_refs": [],
                "binding": "order_id",
            },
            "prerequisite_expected_missing",
        ),
        (
            {
                "name": "extra",
                "check": "The order is ready.",
                "evidence_refs": [],
                "binding": "order_id",
                "equals": "ord-1",
                "note": "x",
            },
            "prerequisite_noncanonical",
        ),
        (
            {"name": "short", "check": "The order is ready.", "binding": "order_id", "equals": 1},
            "prerequisite_noncanonical",
        ),
        (
            {
                "name": "blank",
                "check": "The order is ready.",
                "evidence_refs": [],
                "binding": " ",
                "equals": "ord-1",
            },
            "prerequisite_binding_invalid",
        ),
        (
            {
                "name": "other",
                "check": "The order is ready.",
                "evidence_refs": [],
                "binding": "order_id",
                "equals": "ord-2",
            },
            "prerequisite_value_mismatch",
        ),
    ],
)
def test_canonical_prerequisite_failures_are_incomplete(
    declaration: dict[str, object], expected_reason: str
) -> None:
    prepared = _incomplete(
        _package(canonical=True, prerequisite_declaration=declaration), {"order_id": "ord-1"}
    )

    assert prepared.reason == expected_reason


@pytest.mark.parametrize(
    ("setup_result", "expected_reason"),
    [({}, "binding_missing"), ({"order_id": 7}, "binding_mistyped")],
)
def test_binding_value_failures_are_incomplete(
    setup_result: dict[str, object], expected_reason: str
) -> None:
    prepared = _incomplete(_package(canonical=True, prerequisite=True), setup_result)

    assert prepared.reason == expected_reason
    assert prepared.setup_outputs == {"prepare": setup_result}
    assert prepared.bindings is None
    assert prepared.prerequisites is None


@pytest.mark.parametrize("slot", ["missing_value", "mistyped_value", "selector_not_found"])
def test_an_undeclared_slot_is_binding_invalid_whatever_its_name_says(slot: str) -> None:
    prepared = _incomplete(
        _package(stimulus_text="refund {{" + slot + "}}"), {"order_id": "ord-1"}
    )

    assert prepared.reason == "binding_invalid"
    assert prepared.bindings == {"order_id": "ord-1"}
    assert prepared.prerequisites == []


@pytest.mark.parametrize(
    "stimulus",
    [{"history": []}, {"user_text": 7}, {"user_text": "refund {{order_id}}", "history": "turns"}],
)
def test_an_unusable_stimulus_is_binding_invalid(stimulus: dict[str, Any]) -> None:
    package = _package()
    members = {**package.members, "stimulus.json": json.dumps(stimulus).encode()}

    prepared = _incomplete(ArtifactPackage(package.manifest, members), {"order_id": "ord-1"})

    assert prepared.reason == "binding_invalid"


def test_an_explicit_null_prerequisite_is_not_treated_as_omitted() -> None:
    prepared = _prepared(
        _package(
            canonical=True,
            stimulus_slot=False,
            prerequisite_declaration={
                "name": "empty",
                "check": "The returned value is explicitly empty.",
                "evidence_refs": [],
                "binding": "order_id",
                "equals": None,
            },
        ),
        {"order_id": None},
    )

    assert prepared.prerequisites[0]["actual"] is None
    assert prepared.prerequisites[0]["expected"] is None


def test_a_reply_package_judge_reads_the_resolved_bindings() -> None:
    prepared = _prepared(_package(judge=_JUDGE, claim_level="reply"), {"order_id": "ord-1"})

    assert prepared.prerequisites == []
    assert prepared.judge_facts == {"order_id": "ord-1"}
    assert prepared.judge_facts is not prepared.bindings


def test_a_command_attempt_package_has_no_judge_facts() -> None:
    assert _prepared(_package(), {"order_id": "ord-1"}).judge_facts == {}


def test_a_setup_binding_records_the_actual_operation_result() -> None:
    # The canonical prerequisite expects ord-1, so preparation stops after resolving.
    prepared = _incomplete(
        _package(canonical=True, prerequisite=True),
        {"order_id": "draft-actual-7", "review_status": "AWAITING_REVIEW"},
    )

    assert prepared.reason == "prerequisite_value_mismatch"
    assert prepared.setup_ledger[0]["result"]["order_id"] == "draft-actual-7"
    assert prepared.binding_provenance["order_id"] == {
        "source_kind": "setup_output",
        "source_ref": "setup:prepare",
        "selector": "result.order_id",
        "provenance": "setup:prepare",
        "ledger_category": "setup_capture",
        "ledger_sequence": 1,
    }


def test_a_failed_setup_records_its_error() -> None:
    def call_target(operation: str, arguments: dict[str, object]) -> object:
        raise RuntimeError("target down")

    prepared = prepare_package(_package(), call_target=call_target)

    assert isinstance(prepared, PreparationIncomplete)
    assert prepared.reason == "setup_failed"
    assert prepared.error == "setup dispatch failed: prepare"
    assert prepared.setup_outputs == {}
    assert [(item["status"], item["error"]) for item in prepared.setup_ledger] == [
        ("failed", "target down")
    ]


def test_setup_without_a_target_call_is_incomplete() -> None:
    prepared = prepare_package(_package(), call_target=None)

    assert isinstance(prepared, PreparationIncomplete)
    assert (prepared.reason, prepared.error) == ("setup_failed", "setup dispatch unavailable")


def test_a_refused_setup_declaration_calls_nothing() -> None:
    package = _package()
    members = dict(package.members)
    members["setup.json"] = b'[{"operation": "audit"}]'
    call_target = _returning({"order_id": "ord-1"})

    prepared = prepare_package(ArtifactPackage(package.manifest, members), call_target=call_target)

    assert isinstance(prepared, PreparationIncomplete)
    assert (prepared.reason, prepared.error) == (
        "setup_failed",
        "setup operation undocumented: audit",
    )
    assert call_target.calls == []
    assert prepared.setup_ledger == []


def test_a_member_that_is_not_json_is_refused_before_setup() -> None:
    package = _package()
    members = dict(package.members)
    members["bindings.json"] = b"{"
    call_target = _returning({"order_id": "ord-1"})

    with pytest.raises(PreparationError, match="invalid JSON package member: bindings.json"):
        prepare_package(ArtifactPackage(package.manifest, members), call_target=call_target)
    assert call_target.calls == []


def test_check_prerequisites_infers_the_canonical_shape() -> None:
    canonical = [{"name": "ready", "binding": "order_id", "check": "c", "evidence_refs": []}]
    legacy = [{"name": "ready", "source": "bindings.order_id", "equals": "ord-1"}]
    values = {"order_id": "ord-1"}

    (strict,) = check_prerequisites(canonical, values=values, setup_outputs={})
    (loose,) = check_prerequisites(legacy, values=values, setup_outputs={})

    assert strict["reason"] == "prerequisite_expected_missing"
    assert loose["status"] == "passed"
    assert check_prerequisites(None, values=values, setup_outputs={}) == []


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ({"operation": "prepare"}, r"^setup must be a list$"),
        ([{"arguments": {}}], r"^setup\[0\] must name an operation$"),
        ([{"operation": "other"}], "^setup operation undocumented: other$"),
        ([{"operation": "audit"}], "^setup operation not permitted: audit$"),
        (
            [{"operation": "prepare"}, {"operation": "prepare", "arguments": []}],
            r"^setup\[1\]\.arguments must be an object$",
        ),
    ],
)
def test_setup_validation_rejects_unsafe_declarations(setup: object, message: str) -> None:
    inventory = {"operations": [{"name": "prepare"}, {"name": "audit"}, "noise"]}

    with pytest.raises(SetupError, match=message):
        _validate_setup(setup, inventory, {"setup_permissions": ["prepare"]})


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ({"operation": "prepare"}, "setup_invalid"),
        ([{"arguments": {}}], "setup_invalid"),
        ([{"operation": "other"}], "setup_invalid"),
        ([{"operation": "audit"}], "setup_permission_undeclared"),
        ([{"operation": "prepare", "arguments": []}], "setup_invalid"),
    ],
)
def test_setup_validation_errors_carry_an_admission_code(setup: object, code: str) -> None:
    inventory = {"operations": [{"name": "prepare"}, {"name": "audit"}]}

    with pytest.raises(SetupError) as error:
        _validate_setup(setup, inventory, {"setup_permissions": ["prepare"]})

    assert error.value.code == code


@pytest.mark.parametrize(
    "permissions", ["prepare", "prepare,audit", "", None, {"prepare": True}, 3]
)
@pytest.mark.parametrize("setup", [[], [{"operation": "prepare"}]])
def test_setup_permissions_that_are_not_a_list_are_a_setup_rejection(
    setup: list[dict[str, str]], permissions: object
) -> None:
    inventory = {"operations": [{"name": "prepare"}]}

    with pytest.raises(SetupError, match="setup_permissions must be a list") as error:
        _validate_setup(setup, inventory, {"setup_permissions": permissions})

    assert error.value.code == "setup_permissions_invalid"


def test_absent_setup_permissions_permit_nothing() -> None:
    inventory = {"operations": [{"name": "prepare"}]}

    _validate_setup([], inventory, {})
    with pytest.raises(SetupError) as error:
        _validate_setup([{"operation": "prepare"}], inventory, {})

    assert error.value.code == "setup_permission_undeclared"


@pytest.mark.parametrize(
    ("setup_dispatch", "result"),
    [(None, None), ("raise", None), ("list", ["x"])],
)
def test_setup_call_errors_carry_the_setup_failed_code(
    setup_dispatch: str | None, result: object
) -> None:
    def dispatch(operation: str, arguments: dict[str, object]) -> object:
        if setup_dispatch == "raise":
            raise RuntimeError("down")
        return result

    with pytest.raises(SetupError) as error:
        _run_setup(
            [{"operation": "prepare", "arguments": {}}],
            None if setup_dispatch is None else dispatch,
            [],
        )

    assert error.value.code == "setup_failed"


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda: validate_stimulus_declarations({}, ()), "stimulus_invalid"),
        (
            lambda: validate_stimulus_declarations({"user_text": "x", "history": {}}, ()),
            "stimulus_invalid",
        ),
        (lambda: check_prerequisites({}, values={}, setup_outputs={}), "binding_invalid"),
        (lambda: check_prerequisites(["x"], values={}, setup_outputs={}), "binding_invalid"),
    ],
)
def test_runtime_binding_errors_carry_a_code(call: Any, code: str) -> None:
    with pytest.raises(BindingError) as error:
        call()

    assert error.value.code == code
