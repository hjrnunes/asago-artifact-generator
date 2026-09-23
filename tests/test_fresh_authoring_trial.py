from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.fresh_trial import inputs as trial_inputs
from scripts.fresh_trial import run_fresh_authoring_trial as trial
from scripts.fresh_trial.inputs import _mapping_sha256
from scripts.fresh_trial.run_fresh_authoring_trial import (
    CASE_ORDER,
    PersistedAuthoringBudget,
    TimedAuthoringTransport,
    TrialCaseInputs,
    TrialInputError,
    build_argument_parser,
    load_control_suite,
    remap_control_cases,
    run_authoring_batch,
    run_trial,
)

from asago_artifact_generator.authoring import (
    AuthoringPolicy,
    BudgetExceeded,
    PromptPacket,
    ScriptedAuthoringTransport,
    TransportResponse,
    build_call1_packet_v2,
)
from asago_artifact_generator.input_adapter import InputKind, load_input
from asago_artifact_generator.qualification_inputs import PreparedAuthoringInputs

from .test_versioned_authoring_wire import (
    _framed,
    _inventory,
    _plan,
    _runtime_contract,
    _view,
)


def _case_inputs() -> tuple[TrialCaseInputs, ...]:
    inventory = _inventory()
    runtime = {
        **_runtime_contract(),
        "detector_controls": {"cases": []},
    }
    return tuple(
        TrialCaseInputs(
            case_id=case_id,
            input_view=_view(),
            inventory=inventory,
            runtime_contract=runtime,
            authoring_input_pins={},
        )
        for case_id in CASE_ORDER
    )


def _review() -> bytes:
    return json.dumps(
        {"decision": "accept", "summary": "scripted acceptance", "findings": []}
    ).encode("utf-8")


def _successful_responses(case_count: int) -> list[bytes]:
    one_case = [json.dumps(_plan()).encode("utf-8"), _review(), _framed(), _review()]
    return one_case * case_count


def _run_batch(tmp_path, responses, *, budget=None, transport_factory=None):
    run_dir = tmp_path / "fresh-consumer-five-case-20260923T000000Z"
    run_dir.mkdir()
    budget = budget or PersistedAuthoringBudget.load(run_dir / "authoring" / "budget-ledger.json")
    transport = ScriptedAuthoringTransport(responses)
    factory_calls: list[object] = []

    def factory():
        factory_calls.append(object())
        return transport

    status = run_authoring_batch(
        run_dir,
        cases=_case_inputs(),
        budget=budget,
        transport_factory=transport_factory or factory,
        raw_evidence_root=tmp_path / "raw",
    )
    return run_dir, budget, transport, factory_calls, status


def _write_valid_g07_index(run_dir, *, digest_overrides=None):
    run_dir.mkdir(parents=True, exist_ok=True)
    gold_source = run_dir / "gold-cases.yaml"
    gold_source.write_text(
        "gold_cases:\n"
        "  - id: G07\n"
        "    constraint_meaning: Preserve the supplied refund constraint.\n",
        encoding="utf-8",
    )
    benchmark_source = run_dir / "benchmark.yaml"
    benchmark_source.write_text(
        "benchmark_version: 4\ncases:\n  - id: G07\n    record_conditions: {}\n",
        encoding="utf-8",
    )
    inventory = {
        "schema_version": "inventory-v1",
        "facts": [],
        "operations": [],
        "source_handles": [
            {
                "ref": "source:gold-cases.yaml",
                "sha256": sha256(gold_source.read_bytes()).hexdigest(),
            },
            {
                "ref": "source:benchmark-v4.yaml",
                "sha256": sha256(benchmark_source.read_bytes()).hexdigest(),
            },
        ],
    }
    runtime = {"setup_permissions": [], "observation": {}, "delivery": []}
    gold_sha = sha256(gold_source.read_bytes()).hexdigest()
    benchmark_sha = sha256(benchmark_source.read_bytes()).hexdigest()
    pins = {
        "schema_version": "authoring-input-pins-v1",
        "scenario_id": "G07",
        "input_sha256": gold_sha,
        "source_digests": {"input": gold_sha, "benchmark": benchmark_sha},
        "inventory_sha256": _mapping_sha256(inventory),
        "runtime_contract_sha256": _mapping_sha256(runtime),
    }
    saved_inputs = run_dir / "saved-inputs.json"
    saved_inputs.write_text(
        json.dumps(
            {
                "authoring_input_pins": pins,
                "inventory": inventory,
                "runtime_contract": runtime,
            }
        ),
        encoding="utf-8",
    )
    digests = {
        "input_sha256": gold_sha,
        "benchmark_sha256": benchmark_sha,
        "inventory_sha256": _mapping_sha256(inventory),
        "runtime_contract_sha256": _mapping_sha256(runtime),
    }
    digests.update(digest_overrides or {})
    cases = {case_id: {} for case_id in CASE_ORDER}
    cases["G07"] = {
        "input_kind": "reference-task",
        "reference_id": "G07",
        "sources": {
            "gold_cases": {
                "path": str(gold_source),
                "sha256": gold_sha,
            },
            "benchmark": {
                "path": str(benchmark_source),
                "sha256": benchmark_sha,
            },
            "saved_inputs": {
                "path": str(saved_inputs),
                "sha256": sha256(saved_inputs.read_bytes()).hexdigest(),
            },
        },
        "digests": digests,
    }
    index_bytes = (
        json.dumps(
            {
                "schema_version": "fresh-five-case-input-index-v1",
                "cases": cases,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    (run_dir / "input-index.json").write_bytes(index_bytes)
    (run_dir / "frozen-policy.json").write_text(
        json.dumps(
            {"digests": {"input_index_sha256": sha256(index_bytes).hexdigest()}},
        ),
        encoding="utf-8",
    )


def _control_file(run_dir, case_id):
    path = run_dir / "controls" / f"{case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "fresh-five-case-controls-v1",
                "cases": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return sha256(path.read_bytes()).hexdigest()


def _write_all_inputs_index(run_dir, monkeypatch):
    run_dir.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict] = {}
    prepared_values: dict[str, tuple[dict, dict]] = {}
    for case_id in CASE_ORDER:
        case_dir = run_dir / "inputs" / case_id
        case_dir.mkdir(parents=True)
        gold = case_dir / "gold-cases.yaml"
        gold.write_text(
            f"gold_cases:\n  - id: {case_id}\n    constraint_meaning: Keep the frozen meaning.\n",
            encoding="utf-8",
        )
        gold_bytes = gold.read_bytes()
        sources: dict[str, tuple[str, bytes]] = {
            "gold_cases": (str(gold.relative_to(run_dir)), gold_bytes)
        }
        if case_id in {"G07", "A03"}:
            supplemental_names = (
                ("benchmark", "benchmark-v4.yaml")
                if case_id == "G07"
                else (
                    ("seeded_state", "seeded-state.json"),
                    ("executor_evidence", "executor-evidence.json"),
                )
            )
            if case_id == "G07":
                benchmark = case_dir / supplemental_names[1]
                benchmark.write_text(
                    "benchmark_version: 4\n"
                    "cases:\n"
                    f"  - id: {case_id}\n"
                    "    record_conditions: {}\n",
                    encoding="utf-8",
                )
                sources["benchmark"] = (
                    str(benchmark.relative_to(run_dir)),
                    benchmark.read_bytes(),
                )
            else:
                for source_name, filename in supplemental_names:
                    source_path = case_dir / filename
                    source_path.write_text("{}", encoding="utf-8")
                    sources[source_name] = (
                        str(source_path.relative_to(run_dir)),
                        source_path.read_bytes(),
                    )
            inventory = {
                "facts": [],
                "operations": [],
                "source_handles": [
                    {
                        "ref": "source:gold-cases.yaml",
                        "sha256": sha256(gold_bytes).hexdigest(),
                    }
                ],
            }
            if case_id == "G07":
                inventory["source_handles"].append(
                    {
                        "ref": "source:benchmark-v4.yaml",
                        "sha256": sha256(sources["benchmark"][1]).hexdigest(),
                    }
                )
            else:
                inventory["source_handles"].extend(
                    [
                        {
                            "ref": "source:seeded-state.json",
                            "sha256": sha256(sources["seeded_state"][1]).hexdigest(),
                        },
                        {
                            "ref": "source:executor-evidence.json",
                            "sha256": sha256(sources["executor_evidence"][1]).hexdigest(),
                        },
                    ]
                )
            runtime = {"setup_permissions": [], "delivery": [], "observation": {}}
            prepared_values[case_id] = (inventory, runtime)
            input_hashes = {"input": sha256(gold_bytes).hexdigest()}
            if case_id == "G07":
                input_hashes["benchmark"] = sha256(sources["benchmark"][1]).hexdigest()
            pins = {
                "schema_version": "authoring-input-pins-v1",
                "scenario_id": case_id,
                "input_sha256": input_hashes["input"],
                "source_digests": input_hashes,
                "inventory_sha256": _mapping_sha256(inventory),
                "runtime_contract_sha256": _mapping_sha256(runtime),
            }
            saved = case_dir / "inputs.json"
            saved.write_text(
                json.dumps(
                    {
                        "authoring_input_pins": pins,
                        "inventory": inventory,
                        "runtime_contract": runtime,
                    }
                ),
                encoding="utf-8",
            )
            sources["saved_inputs"] = (
                str(saved.relative_to(run_dir)),
                saved.read_bytes(),
            )
            reference_label = f"frozen-{case_id}"
            source_names = set(sources)
            source_digests = {
                "input_sha256": input_hashes["input"],
                "inventory_sha256": _mapping_sha256(inventory),
                "runtime_contract_sha256": _mapping_sha256(runtime),
                "authoring_input_pins_sha256": _mapping_sha256(pins),
            }
            if case_id == "G07":
                source_digests["benchmark_sha256"] = input_hashes["benchmark"]
        else:
            runtime = {"setup_permissions": [], "delivery": [], "observation": {}}
            inventory = {"facts": [], "operations": [], "source_handles": []}
            source_digests = {
                "inventory_sha256": _mapping_sha256(inventory),
                "runtime_contract_sha256": _mapping_sha256(runtime),
            }
            reference_label = None
            source_names = set()
            if case_id == "O03":
                for source_name, filename in (
                    ("prepared_draft_state", "prepared-draft-state.json"),
                    ("source_evidence", "source-evidence.json"),
                ):
                    source_path = case_dir / filename
                    source_path.write_text("{}", encoding="utf-8")
                    sources[source_name] = (
                        str(source_path.relative_to(run_dir)),
                        source_path.read_bytes(),
                    )
                source_names = {"gold_cases", "prepared_draft_state", "source_evidence"}
            elif case_id == "O04":
                for source_name, filename in (
                    ("seed_state", "seed-state.json"),
                    ("source_evidence", "source-evidence.json"),
                ):
                    source_path = case_dir / filename
                    source_path.write_text("{}", encoding="utf-8")
                    sources[source_name] = (
                        str(source_path.relative_to(run_dir)),
                        source_path.read_bytes(),
                    )
                source_names = {"gold_cases", "seed_state", "source_evidence"}
            else:
                selection = case_dir / "selection.json"
                selection.write_text("{}", encoding="utf-8")
                handoff_source = (
                    Path(__file__).parents[1]
                    / "contracts"
                    / "scenario-handoff"
                    / "handoff-v1"
                    / "valid"
                    / "adversarial-refund.json"
                )
                handoff = case_dir / "handoff.json"
                handoff.write_bytes(handoff_source.read_bytes())
                feature = handoff.with_suffix(".feature")
                feature.write_text("Feature: frozen fixture\n", encoding="utf-8")
                sources.update(
                    {
                        "selection": (str(selection.relative_to(run_dir)), selection.read_bytes()),
                        "handoff": (str(handoff.relative_to(run_dir)), handoff.read_bytes()),
                        "handoff_feature": (
                            str(feature.relative_to(run_dir)),
                            feature.read_bytes(),
                        ),
                    }
                )
                source_names = {"gold_cases", "selection", "handoff", "handoff_feature"}
            source_digests.update(
                {
                    name: sha256(sources[name][1]).hexdigest()
                    for name in source_names
                    if name != "gold_cases"
                }
            )
            source_digests["input_sha256"] = sha256(
                sources["handoff" if case_id == "SCN-030" else "gold_cases"][1]
            ).hexdigest()
            prepared_values[case_id] = (inventory, runtime)
            if case_id == "SCN-030":
                source_digests["gherkin_sha256"] = sha256(
                    sources["handoff_feature"][1]
                ).hexdigest()
                source_digests["selection_sha256"] = sha256(sources["selection"][1]).hexdigest()
                source_digests["gold_cases_sha256"] = sha256(sources["gold_cases"][1]).hexdigest()
        controls_sha = _control_file(run_dir, case_id)
        source_digests["controls_sha256"] = controls_sha
        source_descriptors = {
            name: {"path": path, "sha256": sha256(content).hexdigest()}
            for name, (path, content) in sources.items()
        }
        entry = {
            "input_kind": ("scenario-handoff-v1" if case_id == "SCN-030" else "reference-task"),
            "reference_id": case_id,
            "reference_label": reference_label,
            "sources": source_descriptors,
            "digests": source_digests,
        }
        if case_id == "O04":
            owner_scope = {
                "scenario_premises": [{"text": "Owner premise.", "source": "SPEC"}],
                "evaluation_instructions": [{"text": "Owner rule.", "source": "SPEC"}],
            }
            entry["owner_scope"] = {
                **owner_scope,
                "sha256": _mapping_sha256(owner_scope),
            }
            entry["digests"]["owner_scope_sha256"] = _mapping_sha256(owner_scope)
        entries[case_id] = entry

    def prepared_case(case_id: str, kwargs: dict[str, Path]) -> PreparedAuthoringInputs:
        gold_path = kwargs["gold_cases_path"]
        view = load_input(
            gold_path,
            kind=InputKind.REFERENCE_TASK,
            reference_id=case_id,
        )
        if case_id == "SCN-030":
            handoff_path = kwargs["handoff_source_path"]
            feature_path = handoff_path.with_suffix(".feature")
            view = load_input(handoff_path, kind=InputKind.SCENARIO_HANDOFF_V1)
            source_digests = {
                "input": sha256(handoff_path.read_bytes()).hexdigest(),
                "gherkin": sha256(feature_path.read_bytes()).hexdigest(),
                "selection": sha256(kwargs["selection_path"].read_bytes()).hexdigest(),
                "gold_cases": sha256(gold_path.read_bytes()).hexdigest(),
            }
        else:
            source_arguments = {
                "O03": {
                    "prepared_draft_state": "prepared_draft_path",
                    "source_evidence": "source_evidence_path",
                },
                "O04": {
                    "seed_state": "seed_state_path",
                    "source_evidence": "source_evidence_path",
                },
            }.get(case_id, {})
            source_digests = {
                "input": sha256(gold_path.read_bytes()).hexdigest(),
                **{
                    source_name: sha256(kwargs[argument].read_bytes()).hexdigest()
                    for source_name, argument in source_arguments.items()
                },
            }
        view = replace(
            view,
            scenario_id=case_id,
            source_digests=source_digests,
        )
        prepared_inventory, prepared_runtime = prepared_values[case_id]
        pins = {
            "schema_version": "authoring-input-pins-v1",
            "scenario_id": case_id,
            "input_sha256": view.source_sha256,
            "source_digests": dict(view.source_digests),
            "inventory_sha256": _mapping_sha256(prepared_inventory),
            "runtime_contract_sha256": _mapping_sha256(prepared_runtime),
        }
        return PreparedAuthoringInputs(
            view,
            prepared_inventory,
            prepared_runtime,
            pins,
        )

    for case_id in ("O03", "O04"):
        monkeypatch.setattr(
            trial_inputs,
            f"prepare_{case_id.lower()}_authoring_inputs",
            lambda _case_id=case_id, **kwargs: prepared_case(_case_id, kwargs),
        )
    monkeypatch.setattr(
        trial_inputs,
        "prepare_scn030_authoring_inputs",
        lambda **kwargs: prepared_case("SCN-030", kwargs),
    )
    index_bytes = (
        json.dumps(
            {
                "schema_version": "fresh-five-case-input-index-v1",
                "cases": entries,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    (run_dir / "input-index.json").write_bytes(index_bytes)
    (run_dir / "frozen-policy.json").write_text(
        json.dumps({"digests": {"input_index_sha256": sha256(index_bytes).hexdigest()}}),
        encoding="utf-8",
    )
    return entries


def test_render_only_writes_exact_call1_bytes_without_transport_construction(
    tmp_path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    _write_all_inputs_index(run_dir, monkeypatch)
    (run_dir / "frozen-policy.json").unlink()
    constructed: list[object] = []

    status = run_trial(
        run_dir,
        mode="render-only",
        transport_factory=lambda: constructed.append(object()),
    )

    assert status["status"] == "rendered_only"
    assert status["transport_constructed"] is False
    assert tuple(status["rendered_requests"]) == CASE_ORDER
    assert constructed == []
    cases = trial.load_trial_case_inputs(run_dir)
    for case_id in CASE_ORDER:
        case = next(item for item in cases if item.case_id == case_id)
        packet = build_call1_packet_v2(
            case.input_view,
            case.inventory,
            case.runtime_contract,
        )
        system_bytes = packet.system.encode("utf-8")
        user_bytes = packet.user.encode("utf-8")
        rendered = status["rendered_requests"][case_id]
        assert (
            run_dir / "rendered-requests" / f"{case_id}.call1.system.txt"
        ).read_bytes() == system_bytes
        assert (
            run_dir / "rendered-requests" / f"{case_id}.call1.user.txt"
        ).read_bytes() == user_bytes
        assert rendered["prompt_sha256"] == packet.sha256
        assert rendered["system_sha256"] == sha256(system_bytes).hexdigest()
        assert rendered["user_sha256"] == sha256(user_bytes).hexdigest()


def test_validation_failure_ends_one_case_and_next_case_runs(tmp_path) -> None:
    responses = [b"{}", b"{}"] + _successful_responses(4)

    run_dir, _budget, transport, _factory_calls, status = _run_batch(tmp_path, responses)

    assert status["status"] == "completed"
    assert status["cases"]["G07"]["status"] == "unresolved"
    assert status["cases"]["A03"]["status"] == "accepted"
    assert status["cases"]["SCN-030"]["status"] == "accepted"
    assert len(transport.requests) == 18
    assert (
        json.loads(
            (run_dir / "authoring" / "G07" / "case-receipt.json").read_text(encoding="utf-8")
        )["status"]
        == "unresolved"
    )


def test_batch_uses_normal_v2_orchestrator_with_shared_reviewed_policy(tmp_path) -> None:
    run_dir = tmp_path / "fresh-consumer-five-case-20260923T000005Z"
    run_dir.mkdir()
    budget = PersistedAuthoringBudget.load(run_dir / "authoring" / "budget-ledger.json")
    captured: list[dict[str, object]] = []

    class Orchestrator:
        def __init__(self, **kwargs) -> None:
            captured.append(kwargs)

        def run(self, *_args):
            return SimpleNamespace(
                status="accepted",
                task_id=captured[-1]["task_id"],
                ledger=[],
                findings=[],
                prompts={},
                package=None,
                package_path=None,
                failure_evidence_path=None,
            )

    status = run_authoring_batch(
        run_dir,
        cases=_case_inputs(),
        budget=budget,
        transport_factory=lambda: object(),
        orchestrator_factory=Orchestrator,
        raw_evidence_root=tmp_path / "raw",
    )

    assert status["status"] == "completed"
    assert [call["task_id"] for call in captured] == list(CASE_ORDER)
    assert all(call["wire_version"] == "v2" for call in captured)
    assert all(call["budget"] is budget for call in captured)
    for call in captured:
        policy = call["policy"]
        assert isinstance(policy, AuthoringPolicy)
        assert policy.plan_max_corrections == 1
        assert policy.artifact_max_corrections == 1
        assert policy.review_plan is True
        assert policy.review_artifact is True
        assert callable(call["supplied_control_cases"])


def test_invalid_review_response_is_case_local_not_a_transport_outage(tmp_path) -> None:
    responses = [json.dumps(_plan()).encode("utf-8"), b"{}"] + _successful_responses(4)

    _run_dir, _budget, transport, _factory_calls, status = _run_batch(tmp_path, responses)

    assert status["status"] == "completed"
    assert status["cases"]["G07"]["status"] == "review_unavailable"
    assert status["cases"]["A03"]["status"] == "accepted"
    assert len(transport.requests) == 18


@pytest.mark.parametrize(
    ("responses", "expected_status", "expected_code"),
    [
        ([TimeoutError("provider unavailable")], "transport_failure", "transport_failure"),
        (
            [
                json.dumps(_plan()).encode("utf-8"),
                TimeoutError("review endpoint unavailable"),
            ],
            "review_unavailable",
            "transport_failure",
        ),
        (
            [
                json.dumps(_plan()).encode("utf-8"),
                json.dumps(
                    {
                        "decision": "revise",
                        "summary": "The plan needs one correction.",
                        "findings": [
                            {
                                "location": "plan.interpretation",
                                "problem": "The supplied meaning is not preserved.",
                                "basis": "The request requires preserving the supplied meaning.",
                                "required_change": "Preserve the supplied meaning.",
                            }
                        ],
                    }
                ).encode("utf-8"),
                TimeoutError("correction endpoint unavailable"),
            ],
            "transport_failure",
            "correction_dispatch_failed",
        ),
    ],
)
def test_transport_outage_stops_batch_and_marks_later_cases_unattempted(
    tmp_path,
    responses,
    expected_status,
    expected_code,
) -> None:
    run_dir, budget, transport, _factory_calls, status = _run_batch(tmp_path, responses)

    assert status["status"] == "outage_stopped"
    assert status["outage_stopped"] is True
    assert status["cases"]["G07"]["status"] == expected_status
    receipt = json.loads(
        (run_dir / "authoring" / "G07" / "case-receipt.json").read_text(encoding="utf-8")
    )
    assert expected_code in {finding["code"] for finding in receipt["findings_summary"]}
    assert all(status["cases"][case_id]["status"] == "unattempted" for case_id in CASE_ORDER[1:])
    assert len(transport.requests) == len(responses)
    assert budget.total_dispatched == len(responses)
    for case_id in CASE_ORDER[1:]:
        receipt = json.loads(
            (run_dir / "authoring" / case_id / "case-receipt.json").read_text(encoding="utf-8")
        )
        assert receipt["status"] == "unattempted"


def test_existing_reservation_package_or_failure_evidence_blocks_redispatch(tmp_path) -> None:
    run_dir = tmp_path / "fresh-consumer-five-case-20260923T000001Z"
    run_dir.mkdir()
    budget = PersistedAuthoringBudget.load(run_dir / "authoring" / "budget-ledger.json")
    budget.reserve("G07", role="author")
    raw_root = tmp_path / "raw"
    (raw_root / "A03" / "package").mkdir(parents=True)
    (raw_root / "A03" / "package" / "manifest.json").write_text("{}", encoding="utf-8")
    (raw_root / "O03").mkdir(parents=True)
    (raw_root / "O03" / "package.failure-evidence.json").write_text("{}", encoding="utf-8")
    transport = ScriptedAuthoringTransport(_successful_responses(2))
    factory_calls: list[object] = []

    status = run_authoring_batch(
        run_dir,
        cases=_case_inputs(),
        budget=budget,
        transport_factory=lambda: factory_calls.append(object()) or transport,
        raw_evidence_root=raw_root,
    )

    assert [status["cases"][case_id]["status"] for case_id in CASE_ORDER[:3]] == [
        "already_attempted",
        "already_attempted",
        "already_attempted",
    ]
    assert status["cases"]["O04"]["status"] == "accepted"
    assert status["cases"]["SCN-030"]["status"] == "accepted"
    assert len(factory_calls) == 1
    assert len(transport.requests) == 8
    for case_id in CASE_ORDER[:3]:
        assert (run_dir / "authoring" / case_id / "case-receipt.json").is_file()


def test_exhausted_shared_budget_stops_before_transport_construction(tmp_path) -> None:
    run_dir = tmp_path / "fresh-consumer-five-case-20260923T000002Z"
    run_dir.mkdir()
    budget = PersistedAuthoringBudget.load(run_dir / "authoring" / "budget-ledger.json")
    for task_number in range(5):
        task_id = f"prior-case-{task_number}"
        for role in ("author", "reviewer"):
            for _ in range(4):
                budget.reserve(task_id, role=role)
    factory_calls: list[object] = []

    status = run_authoring_batch(
        run_dir,
        cases=_case_inputs(),
        budget=budget,
        transport_factory=lambda: factory_calls.append(object()),
        raw_evidence_root=tmp_path / "raw",
    )

    assert status["status"] == "budget_stopped"
    assert status["cases"]["G07"]["status"] == "budget_exhausted"
    assert all(status["cases"][case_id]["status"] == "unattempted" for case_id in CASE_ORDER[1:])
    assert factory_calls == []


def test_control_loader_keeps_unresolved_rows_out_of_dispatch_and_remaps_bindings(
    tmp_path,
) -> None:
    control_path = tmp_path / "O03.json"
    control_document = {
        "schema_version": "fresh-five-case-controls-v1",
        "cases": [
            {
                "name": "matching-commit",
                "evidence": {
                    "bindings": {"fixture_draft": "SYN-DRAFT-001"},
                    "tool_calls": [
                        {
                            "name": "commit_to_ehr",
                            "decoded_arguments": {
                                "draft_id": "{{record_id:draft}}",
                            },
                        }
                    ],
                },
                "expected_outcome": "detected",
                "expected_claim_level": "command_attempt",
                "binding_remap": {
                    "fixture_draft": {
                        "source_kind": "setup_output",
                        "source_ref": "setup:summarize_for_ehr",
                        "selector": "result.draft_id",
                    }
                },
                "dynamic_record_ids": {"draft": "fixture_draft"},
            },
            {
                "name": "historical-runtime-crash",
                "status": "unresolved",
                "reason": "The recorded detector crash is not a verdict.",
            },
        ],
    }
    control_bytes = (json.dumps(control_document) + "\n").encode("utf-8")
    control_path.write_bytes(control_bytes)

    suite = load_control_suite(
        control_path,
        expected_sha256=sha256(control_bytes).hexdigest(),
    )
    remapped = remap_control_cases(
        suite.cases,
        {
            "runtime_bindings": [
                {
                    "name": "actual_draft_binding",
                    "source_kind": "setup_output",
                    "source_ref": "setup:summarize_for_ehr",
                    "selector": "result.draft_id",
                }
            ]
        },
        {},
    )

    assert [case.name for case in remapped] == ["matching-commit"]
    evidence = remapped[0].evidence
    assert evidence["bindings"] == {"actual_draft_binding": "SYN-DRAFT-001"}
    assert evidence["tool_calls"][0]["decoded_arguments"]["draft_id"] == "SYN-DRAFT-001"
    assert [item["name"] for item in suite.unresolved] == ["historical-runtime-crash"]


def test_cli_requires_explicit_render_only_or_live_mode() -> None:
    parser = build_argument_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--run-dir", "/tmp/trial"])

    assert parser.parse_args(["--run-dir", "/tmp/trial", "--render-only"]).mode == ("render-only")
    assert parser.parse_args(["--run-dir", "/tmp/trial", "--live"]).mode == "live"
    with pytest.raises(SystemExit):
        parser.parse_args(["--run-dir", "/tmp/trial", "--render-only", "--live"])


def test_private_transport_uses_pinned_profile_controls(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Transport:
        max_retries = 0

        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(
        trial,
        "load_authoring_profile",
        lambda _profiles_file, name: type(
            "Profile",
            (),
            {
                "base_url": "https://private.example.invalid/v1",
                "api_key": "in-memory-test-key",
                "model": "gemma-4-26b-a4b-it",
                "name": name,
            },
        )(),
    )
    monkeypatch.setattr(trial, "PrivateModelAuthoringTransport", Transport)

    transport = trial.build_private_model_transport("/tmp/profiles.yaml")

    assert isinstance(transport, Transport)
    assert captured == {
        "base_url": "https://private.example.invalid/v1",
        "api_key": "in-memory-test-key",
        "model": "gemma-4-26b-a4b-it",
        "profile_name": "gemma4-oc",
        "temperature": 0.0,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        "context_window_tokens": 32768,
        "max_completion_tokens": 8192,
    }


def test_persisted_budget_reserves_before_timing_transport_and_reloads(tmp_path) -> None:
    budget_path = tmp_path / "budget-ledger.json"
    timings_path = tmp_path / "call-timings.jsonl"
    budget = PersistedAuthoringBudget.load(budget_path)

    class InspectingTransport:
        max_retries = 0

        def complete(self, packet: PromptPacket) -> TransportResponse:
            self.packet = packet
            state = json.loads(budget_path.read_text(encoding="utf-8"))
            assert state["limits"] == {
                "aggregate_limit": 40,
                "task_limit": 8,
                "author_limit": 4,
                "review_limit": 4,
            }
            assert state["total_dispatched"] == 1
            assert state["reservations"][-1]["status"] == "reserved"
            assert packet.system == "system bytes"
            assert packet.user == "user bytes"
            return TransportResponse(raw=b"response bytes")

    inner = InspectingTransport()
    transport = TimedAuthoringTransport(
        inner,
        timings_path=timings_path,
        budget=budget,
        task_id="G07",
    )
    packet = PromptPacket(
        stage="call1",
        version="test-v1",
        system="system bytes",
        user="user bytes",
        payload={},
    )

    budget.reserve("G07", role="author")
    response = transport.complete(packet)

    assert response.raw == b"response bytes"
    assert inner.packet is packet
    timing = json.loads(timings_path.read_text(encoding="utf-8").splitlines()[0])
    assert timing["task_id"] == "G07"
    assert timing["stage"] == "call1"
    assert timing["start"]
    assert timing["end"]
    assert timing["elapsed_ms"] >= 0
    assert timing["outcome"] == "response_returned"

    saved = json.loads(budget_path.read_text(encoding="utf-8"))
    assert saved["reservations"][-1]["status"] == "completed"
    assert saved["reservations"][-1]["outcome"] == "response_returned"

    restarted = PersistedAuthoringBudget.load(budget_path)
    assert restarted.total_dispatched == 1
    assert restarted.dispatched_by_task == {"G07": 1}
    assert restarted.dispatched_by_task_role == {"G07": {"author": 1}}


def test_timing_wrapper_records_transport_exception_and_consumes_reservation(tmp_path) -> None:
    budget_path = tmp_path / "budget-ledger.json"
    timings_path = tmp_path / "call-timings.jsonl"
    budget = PersistedAuthoringBudget.load(budget_path)

    class FailingTransport:
        max_retries = 0

        def complete(self, packet: PromptPacket) -> bytes:
            raise TimeoutError("provider unavailable")

    transport = TimedAuthoringTransport(
        FailingTransport(),
        timings_path=timings_path,
        budget=budget,
        task_id="A03",
    )
    packet = PromptPacket("call1", "test-v1", "system", "user", {})
    budget.reserve("A03", role="author")

    with pytest.raises(TimeoutError, match="provider unavailable"):
        transport.complete(packet)

    timing = json.loads(timings_path.read_text(encoding="utf-8").splitlines()[0])
    assert timing["outcome"] == "transport_error"
    state = json.loads(budget_path.read_text(encoding="utf-8"))
    assert state["total_dispatched"] == 1
    assert state["reservations"][0]["status"] == "failed"
    assert state["reservations"][0]["outcome"] == "transport_error"


def test_live_input_hash_mismatch_aborts_before_transport_construction(tmp_path) -> None:
    run_dir = tmp_path / "trial"
    _write_valid_g07_index(
        run_dir,
        digest_overrides={"input_sha256": "0" * 64},
    )
    transports: list[object] = []

    with pytest.raises(TrialInputError, match="G07 loaded input sha256"):
        run_trial(
            run_dir,
            mode="live",
            transport_factory=lambda: transports.append(object()),
        )

    assert transports == []


def test_frozen_index_loads_all_five_input_routes_before_dispatch(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "trial"
    _write_all_inputs_index(run_dir, monkeypatch)

    cases = trial.load_trial_case_inputs(run_dir)

    assert tuple(case.case_id for case in cases) == CASE_ORDER
    assert cases[0].input_view.reference_id == "G07"
    assert cases[1].input_view.reference_id == "A03"
    assert cases[2].input_view.reference_id == "O03"
    assert cases[3].input_view.owner_scope == {
        "scenario_premises": [{"text": "Owner premise.", "source": "SPEC"}],
        "evaluation_instructions": [{"text": "Owner rule.", "source": "SPEC"}],
    }
    assert cases[4].case_id == "SCN-030"
    assert cases[0].inventory["source_handles"]
    assert cases[1].inventory["source_handles"]


@pytest.mark.parametrize(
    ("digest_name", "message"),
    [
        ("inventory_sha256", "G07 inventory sha256"),
        ("runtime_contract_sha256", "G07 runtime_contract sha256"),
    ],
)
def test_frozen_input_mapping_hash_mismatch_aborts_before_transport_construction(
    tmp_path,
    digest_name,
    message,
) -> None:
    run_dir = tmp_path / digest_name
    _write_valid_g07_index(run_dir, digest_overrides={digest_name: "0" * 64})
    transports: list[object] = []

    with pytest.raises(TrialInputError, match=message):
        run_trial(
            run_dir,
            mode="live",
            transport_factory=lambda: transports.append(object()),
        )

    assert transports == []


def test_owner_scope_hash_mismatch_aborts_before_transport_construction(
    tmp_path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "owner-scope"
    entries = _write_all_inputs_index(run_dir, monkeypatch)
    owner_scope = entries["O04"]["owner_scope"]
    owner_scope["scenario_premises"][0]["text"] = "altered scope"
    owner_scope["sha256"] = _mapping_sha256(
        {key: owner_scope[key] for key in ("scenario_premises", "evaluation_instructions")}
    )
    index_bytes = (
        json.dumps(
            {
                "schema_version": "fresh-five-case-input-index-v1",
                "cases": entries,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    (run_dir / "input-index.json").write_bytes(index_bytes)
    (run_dir / "frozen-policy.json").write_text(
        json.dumps({"digests": {"input_index_sha256": sha256(index_bytes).hexdigest()}}),
        encoding="utf-8",
    )
    transports: list[object] = []

    with pytest.raises(TrialInputError, match="O04 owner_scope sha256 mismatch"):
        run_trial(
            run_dir,
            mode="live",
            transport_factory=lambda: transports.append(object()),
        )

    assert transports == []


def test_missing_control_hash_aborts_render_before_transport_construction(
    tmp_path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "missing-control-pin"
    entries = _write_all_inputs_index(run_dir, monkeypatch)
    del entries["G07"]["digests"]["controls_sha256"]
    index_bytes = (
        json.dumps(
            {
                "schema_version": "fresh-five-case-input-index-v1",
                "cases": entries,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    (run_dir / "input-index.json").write_bytes(index_bytes)
    (run_dir / "frozen-policy.json").write_text(
        json.dumps({"digests": {"input_index_sha256": sha256(index_bytes).hexdigest()}}),
        encoding="utf-8",
    )
    transports: list[object] = []

    with pytest.raises(TrialInputError, match="G07 input index.*control file sha256"):
        run_trial(
            run_dir,
            mode="render-only",
            transport_factory=lambda: transports.append(object()),
        )

    assert transports == []


@pytest.mark.parametrize(
    ("digest_field", "message"),
    [
        ("inventory_sha256", "G07 inventory sha256"),
        ("runtime_contract_sha256", "G07 runtime_contract sha256"),
    ],
)
def test_saved_input_mapping_hash_mismatch_aborts_before_transport_construction(
    tmp_path,
    digest_field,
    message,
) -> None:
    run_dir = tmp_path / "fresh-consumer-five-case-20260923T000003Z"
    _write_valid_g07_index(
        run_dir,
        digest_overrides={digest_field: "0" * 64},
    )
    transports: list[object] = []

    with pytest.raises(TrialInputError, match=message):
        run_trial(
            run_dir,
            mode="live",
            transport_factory=lambda: transports.append(object()),
        )

    assert transports == []


def test_persisted_budget_rejects_over_limit_without_a_new_reservation(tmp_path) -> None:
    budget_path = tmp_path / "budget-ledger.json"
    budget = PersistedAuthoringBudget.load(budget_path)

    for task_id in ("G07", "A03", "O03", "O04", "SCN-030"):
        for role in ("author", "reviewer"):
            for _ in range(4):
                budget.reserve(task_id, role=role)

    with pytest.raises(BudgetExceeded):
        budget.reserve("G07", role="author")

    saved = json.loads(budget_path.read_text(encoding="utf-8"))
    assert saved["total_dispatched"] == 40
    assert len(saved["reservations"]) == 40
    assert saved["limits"] == {
        "aggregate_limit": 40,
        "task_limit": 8,
        "author_limit": 4,
        "review_limit": 4,
    }
