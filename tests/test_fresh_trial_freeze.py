from __future__ import annotations

import copy
import json
from hashlib import sha256
from pathlib import Path

import pytest
from scripts.fresh_trial.freeze_trial import (
    _assert_only_fact_schemas_changed,
    _fact_schema_regeneration,
    create_freeze,
    finalize_renderings,
)
from scripts.fresh_trial.inputs import CASE_ORDER, _mapping_sha256, load_trial_case_inputs
from scripts.fresh_trial.receipts import (
    _policy_control_digests,
    _read_frozen_policy,
    _verify_frozen_controls,
    _verify_frozen_renderings,
)
from scripts.fresh_trial.run_fresh_authoring_trial import run_trial

from asago_artifact_generator.authoring import _binding_selector_type

PREVIOUS_FREEZE = Path(
    "/Users/hjrnunes/workspace/redhat/hjrnunes/asago-scenario-generator/"
    ".worktrees/llm-designed-artifacts/asago-scenario-generator/build/qualification/"
    "fresh-consumer-five-case-20260923T182810Z"
)


def _snapshot(path: Path) -> dict[Path, bytes]:
    return {
        item.relative_to(path): item.read_bytes() for item in path.rglob("*") if item.is_file()
    }


def _saved_inventory(path: Path, case_id: str) -> dict:
    if case_id in {"G07", "A03"}:
        saved_path = Path(
            json.loads((path / "input-index.json").read_text(encoding="utf-8"))["cases"][case_id][
                "sources"
            ]["saved_inputs"]["path"]
        )
        if not saved_path.is_absolute():
            saved_path = path / saved_path
        return json.loads(saved_path.read_text(encoding="utf-8"))["inventory"]
    raise AssertionError(f"unsupported saved inventory case: {case_id}")


def _make_freeze(tmp_path: Path) -> Path:
    run_dir = tmp_path / "fresh-consumer-five-case-DRYRUN"
    create_freeze(
        PREVIOUS_FREEZE,
        run_dir,
        consumer_root=Path(__file__).parents[1],
        downstream_root=PREVIOUS_FREEZE.parents[2],
    )
    return run_dir


def _write_route_compatibility(
    path: Path,
    *,
    downstream_head: str,
    consumer_head: str | None = None,
) -> bytes:
    route = json.loads((PREVIOUS_FREEZE / "route-compatibility.json").read_bytes())
    route["verified_against"]["downstream_head"] = downstream_head
    if consumer_head is None:
        route["verified_against"].pop("consumer_head", None)
    else:
        route["verified_against"]["consumer_head"] = consumer_head
    data = json.dumps(route, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.write_bytes(data)
    return data


def test_schema_regeneration_changes_only_fact_schemas() -> None:
    inventory = {
        "schema_version": "test",
        "facts": [
            {
                "ref": "state:order",
                "value": {"order_id": "ORD-1", "remaining_to_pay": 12.5},
                "schema": {"type": "object"},
                "provenance": "fixture",
            }
        ],
        "operations": [{"name": "lookup_order"}],
        "source_handles": [{"ref": "source:test", "sha256": "0" * 64}],
    }

    transformed, note = _fact_schema_regeneration("G07", inventory)

    _assert_only_fact_schemas_changed(inventory, transformed, "G07")
    assert transformed["facts"][0]["schema"] == {
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "remaining_to_pay": {"type": "number"},
        },
    }
    assert transformed["facts"][0]["value"] == inventory["facts"][0]["value"]
    assert note["old_inventory_sha256"] == _mapping_sha256(inventory)
    assert note["new_inventory_sha256"] == _mapping_sha256(transformed)
    assert note["changed_fact_refs"] == ["state:order"]


def test_new_freeze_pins_regenerated_inputs_and_preserves_other_inventory_data(tmp_path) -> None:
    run_dir = _make_freeze(tmp_path)
    for case_id in ("G07", "A03"):
        before = _saved_inventory(PREVIOUS_FREEZE, case_id)
        after = _saved_inventory(run_dir, case_id)
        before_without_schema = copy.deepcopy(before)
        after_without_schema = copy.deepcopy(after)
        for fact in before_without_schema["facts"]:
            fact.pop("schema", None)
        for fact in after_without_schema["facts"]:
            fact.pop("schema", None)
        assert after_without_schema == before_without_schema

    cases = load_trial_case_inputs(run_dir)
    assert tuple(case.case_id for case in cases) == CASE_ORDER
    index = json.loads((run_dir / "input-index.json").read_text(encoding="utf-8"))
    for case_id in ("G07", "A03"):
        case = next(item for item in cases if item.case_id == case_id)
        saved_path = run_dir / index["cases"][case_id]["sources"]["saved_inputs"]["path"]
        saved = json.loads(saved_path.read_text(encoding="utf-8"))
        assert saved["authoring_input_pins"]["inventory_sha256"] == _mapping_sha256(
            saved["inventory"]
        )
        assert index["cases"][case_id]["digests"]["inventory_sha256"] == _mapping_sha256(
            saved["inventory"]
        )
        assert case.authoring_input_pins["source_digests"] == _saved_source_digests(
            PREVIOUS_FREEZE, case_id
        )

    g07_order = next(
        fact
        for fact in _saved_inventory(run_dir, "G07")["facts"]
        if fact["ref"] == "state:orders:ORD-102"
    )
    assert _binding_selector_type(g07_order["schema"], "value.order_id") == "string"
    assert _binding_selector_type(g07_order["schema"], "value.remaining_to_pay") == "number"


def test_new_policy_replaces_history_and_records_prior_spend(tmp_path) -> None:
    run_dir = _make_freeze(tmp_path)
    policy = _read_frozen_policy(run_dir)
    previous_policy = _read_frozen_policy(PREVIOUS_FREEZE)
    previous = policy["previous_freeze"]

    assert not {
        "refreeze",
        "refreezes",
        "refreeze_round_2",
        "refreeze_round_3",
    }.intersection(policy)
    assert previous["run_dir"] == str(PREVIOUS_FREEZE.resolve())
    assert (
        previous["frozen_policy_sha256"]
        == sha256((PREVIOUS_FREEZE / "frozen-policy.json").read_bytes()).hexdigest()
    )
    assert (
        previous["input_index_sha256"]
        == sha256((PREVIOUS_FREEZE / "input-index.json").read_bytes()).hexdigest()
    )
    assert (
        previous["rendered_request_index_sha256"]
        == sha256((PREVIOUS_FREEZE / "rendered-requests" / "index.json").read_bytes()).hexdigest()
    )
    assert previous["refreeze_history_statement"] == (
        "The previous policy records its own refreeze history; this new freeze "
        "does not copy that history or its superseded paths."
    )
    prior_spend = previous["prior_spend"]
    assert prior_spend["batch_status"] == "completed"
    assert prior_spend["aggregate_authoring_dispatches"] == 16
    assert prior_spend["per_case_dispatches"] == {
        "A03": 2,
        "G07": 2,
        "O03": 4,
        "O04": 5,
        "SCN-030": 3,
    }
    assert prior_spend["per_case_role_dispatches"] == {
        "A03": {"author": 2},
        "G07": {"author": 2},
        "O03": {"author": 2, "reviewer": 2},
        "O04": {"author": 4, "reviewer": 1},
        "SCN-030": {"author": 2, "reviewer": 1},
    }
    assert prior_spend["budget_ledger_total_dispatched"] == 16
    assert prior_spend["author_review_dispatch_totals"] == {
        "dispatches": 16,
        "author_correction_dispatches": 12,
        "review_dispatches": 4,
        "ceiling": 40,
        "per_case_ceiling": 8,
    }
    assert prior_spend["execution"]["execution_summary_status"] == (
        "completed_no_accepted_packages"
    )
    assert prior_spend["execution"]["accepted_package_count"] == 0
    assert prior_spend["execution"]["totals"] == {
        "cases": 5,
        "eligible_packages": 0,
        "not_eligible_nonaccepted_cases": 5,
        "preflight_invocations": 0,
        "live_launcher_invocations": 0,
        "setup_operations": 0,
        "generation_count": 0,
        "judge_count": 0,
        "actual_target_tool_calls": 0,
        "services_started": 0,
        "cleanup_required": False,
    }
    assert prior_spend["execution"]["results_status"] == "reconciled_zero_accepted_packages"
    assert prior_spend["execution"]["results_reconciliation_live_calls"] == {
        "model": 0,
        "gateway": 0,
        "target": 0,
    }
    assert prior_spend["execution"]["results_attempted_execution"] == {
        "count": 0,
        "denominator": 5,
    }
    assert prior_spend["new_freeze_allowance_statement"] == (
        "This freeze's allowance is separate from the previous trial's spend and "
        "requires explicit owner approval before any live dispatch."
    )
    assert previous_policy["status"] == "refrozen_after_scrutiny_round_3_before_any_live_dispatch"


def test_new_policy_refreshes_spec_ports_and_route_metadata(tmp_path) -> None:
    run_dir = _make_freeze(tmp_path)
    policy = _read_frozen_policy(run_dir)

    specification = policy["specification"]
    specification_path = Path(specification["path"])
    assert specification["sha256"] == sha256(specification_path.read_bytes()).hexdigest()
    assert specification["tracked"] is False
    assert "a03_path_typo_occurrences" not in specification
    assert "intended_path_occurrences" not in specification
    assert "reconstructed_pre_correction_sha256" not in specification
    assert "reverified_at_utc" not in specification

    ports = policy["freeze_invariants"]["ports_free_at_freeze"]
    assert ports["status"] == "measured"
    assert ports["binding"] == "127.0.0.1"
    assert ports["checked_at_utc"]
    assert ports["ports"]
    assert ports["all_free"] == all(value == "free" for value in ports["ports"].values())
    assert policy["freeze_invariants"]["pre_execution_port_check_required"] is True

    route = policy["route_compatibility"]
    previous_route = json.loads(
        (PREVIOUS_FREEZE / "route-compatibility.json").read_text(encoding="utf-8")
    )
    assert (
        route["previous_verified_downstream_head"]
        == previous_route["verified_against"]["downstream_head"]
    )
    assert route["previous_verified_against"] == previous_route["verified_against"]
    assert route["previous_rechecked_at"] == previous_route["rechecked_at"]
    assert route["previous_method"] == previous_route["method"]
    assert route["status"] == "requires_revalidation"
    assert route["source_revision_at_freeze"] != route["previous_verified_downstream_head"]


def test_default_route_path_preserves_previous_route_bytes(tmp_path) -> None:
    run_dir = _make_freeze(tmp_path)
    previous_bytes = (PREVIOUS_FREEZE / "route-compatibility.json").read_bytes()
    route_bytes = (run_dir / "route-compatibility.json").read_bytes()
    policy_route = _read_frozen_policy(run_dir)["route_compatibility"]

    assert route_bytes == previous_bytes
    assert policy_route["sha256"] == sha256(previous_bytes).hexdigest()
    assert "source_path" not in policy_route


def test_supplied_route_is_copied_byte_for_byte_and_digest_is_recorded(tmp_path) -> None:
    baseline = _make_freeze(tmp_path / "baseline")
    current_downstream = _read_frozen_policy(baseline)["source_revision"]["downstream_head"]
    source = tmp_path / "supplied-route.json"
    source_bytes = _write_route_compatibility(
        source,
        downstream_head=current_downstream,
    )

    run_dir = tmp_path / "supplied-freeze"
    create_freeze(
        PREVIOUS_FREEZE,
        run_dir,
        consumer_root=Path(__file__).parents[1],
        downstream_root=PREVIOUS_FREEZE.parents[2],
        route_compatibility=source,
    )

    policy = _read_frozen_policy(run_dir)
    route = policy["route_compatibility"]
    assert (run_dir / "route-compatibility.json").read_bytes() == source_bytes
    assert route["source_path"] == str(source.resolve())
    assert route["sha256"] == sha256(source_bytes).hexdigest()
    assert route["verified_downstream_head"] == current_downstream
    assert route["previous_verified_downstream_head"]
    assert route["status"] == "verified_at_current_downstream_head"


def test_supplied_route_requires_revalidation_when_downstream_differs(tmp_path) -> None:
    source = tmp_path / "supplied-route.json"
    _write_route_compatibility(source, downstream_head="0" * 40)
    run_dir = tmp_path / "supplied-freeze"

    create_freeze(
        PREVIOUS_FREEZE,
        run_dir,
        consumer_root=Path(__file__).parents[1],
        downstream_root=PREVIOUS_FREEZE.parents[2],
        route_compatibility=source,
    )

    route = _read_frozen_policy(run_dir)["route_compatibility"]
    assert route["verified_downstream_head"] == "0" * 40
    assert route["status"] == "requires_revalidation"


def test_supplied_route_records_consumer_revision_mismatch(tmp_path) -> None:
    source = tmp_path / "supplied-route.json"
    _write_route_compatibility(
        source,
        downstream_head="0" * 40,
        consumer_head="f" * 40,
    )
    run_dir = tmp_path / "supplied-freeze"

    create_freeze(
        PREVIOUS_FREEZE,
        run_dir,
        consumer_root=Path(__file__).parents[1],
        downstream_root=PREVIOUS_FREEZE.parents[2],
        route_compatibility=source,
    )

    route = _read_frozen_policy(run_dir)["route_compatibility"]
    assert route["verified_consumer_head"] == "f" * 40
    assert route["consumer_revision_mismatch"] is True


def test_invalid_supplied_route_is_rejected_before_run_directory_creation(tmp_path) -> None:
    source = tmp_path / "invalid-route.json"
    source.write_bytes(b'{"verified_against":')
    run_dir = tmp_path / "supplied-freeze"

    with pytest.raises(ValueError, match="supplied route compatibility"):
        create_freeze(
            PREVIOUS_FREEZE,
            run_dir,
            consumer_root=Path(__file__).parents[1],
            downstream_root=PREVIOUS_FREEZE.parents[2],
            route_compatibility=source,
        )

    assert not run_dir.exists()


def _saved_source_digests(path: Path, case_id: str) -> dict:
    index = json.loads((path / "input-index.json").read_text(encoding="utf-8"))
    saved_path = Path(index["cases"][case_id]["sources"]["saved_inputs"]["path"])
    if not saved_path.is_absolute():
        saved_path = path / saved_path
    return json.loads(saved_path.read_text(encoding="utf-8"))["authoring_input_pins"][
        "source_digests"
    ]


def test_rendered_freeze_passes_input_control_and_rendering_verification_without_transport(
    tmp_path,
) -> None:
    run_dir = _make_freeze(tmp_path)
    transport_calls: list[str] = []
    status = run_trial(
        run_dir,
        mode="render-only",
        transport_factory=lambda: transport_calls.append("constructed"),
    )
    assert status["transport_constructed"] is False
    finalize_renderings(run_dir)

    cases = load_trial_case_inputs(run_dir)
    policy = _read_frozen_policy(run_dir)
    _verify_frozen_controls(run_dir, cases, policy)
    _verify_frozen_renderings(run_dir, cases, policy)
    assert transport_calls == []
    assert _policy_control_digests(policy) == {
        case_id: case.controls_sha256 for case_id, case in zip(CASE_ORDER, cases, strict=True)
    }


def test_freeze_refuses_to_overwrite_and_leaves_existing_directory_unchanged(tmp_path) -> None:
    run_dir = _make_freeze(tmp_path)
    before = _snapshot(run_dir)

    with pytest.raises(ValueError, match="refusing to overwrite"):
        create_freeze(
            PREVIOUS_FREEZE,
            run_dir,
            consumer_root=Path(__file__).parents[1],
            downstream_root=PREVIOUS_FREEZE.parents[2],
        )

    assert _snapshot(run_dir) == before


def test_previous_freeze_is_unchanged_by_new_freeze(tmp_path) -> None:
    before = _snapshot(PREVIOUS_FREEZE)
    _make_freeze(tmp_path)
    assert _snapshot(PREVIOUS_FREEZE) == before


def test_previous_control_digests_are_copied_exactly(tmp_path) -> None:
    run_dir = _make_freeze(tmp_path)
    for case_id in CASE_ORDER:
        old_bytes = (PREVIOUS_FREEZE / "controls" / f"{case_id}.json").read_bytes()
        new_bytes = (run_dir / "controls" / f"{case_id}.json").read_bytes()
        assert new_bytes == old_bytes
        assert sha256(new_bytes).hexdigest() == sha256(old_bytes).hexdigest()
