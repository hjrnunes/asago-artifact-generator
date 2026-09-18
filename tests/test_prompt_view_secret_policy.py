"""Prompt-view secret-policy regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.authoring import (
    AuthoringError,
    AuthoringOrchestrator,
    ScriptedAuthoringTransport,
    assert_no_prompt_secrets,
    assert_no_secrets,
    build_call1_packet,
    build_call2_packet,
    scan_for_prompt_secrets,
)
from asago_artifact_generator.input_adapter import InputKind, load_input
from asago_artifact_generator.metadata_policy import prompt_secret_metadata_paths
from asago_artifact_generator.package_io import PackageIntegrityError, build_package

PROJECT_ROOT = Path(__file__).resolve().parents[4]
CONSUMER_ROOT = Path(__file__).resolve().parents[1]
GOLD = PROJECT_ROOT / "data" / "gold" / "miniklarna" / "gold-cases.yaml"
BENCHMARK = GOLD.parent / "benchmark-v4.yaml"
SAVED_PLAN_RENDER = (
    CONSUMER_ROOT
    / "runs"
    / "authoring"
    / "g07-interface-correction-20260918"
    / "call2-saved-plan-replay.json"
)
INVENTORY = CONSUMER_ROOT / "runs" / "authoring" / "g07-live-input" / "inventory.json"
RUNTIME_CONTRACT = (
    CONSUMER_ROOT / "runs" / "authoring" / "g07-live-input" / "runtime-contract.json"
)


def _g07_view():
    return load_input(
        GOLD,
        kind=InputKind.REFERENCE_TASK,
        reference_label="miniklarna-gold-development-reference",
        reference_id="G07",
        benchmark_source_path=BENCHMARK,
    )


def _g07_inputs() -> tuple[dict, dict]:
    return (
        json.loads(INVENTORY.read_text(encoding="utf-8")),
        json.loads(RUNTIME_CONTRACT.read_text(encoding="utf-8")),
    )


def _saved_g07_plan() -> dict:
    rendered = json.loads(SAVED_PLAN_RENDER.read_text(encoding="utf-8"))
    return rendered["payload"]["validated_plan"]


def test_prompt_policy_preserves_benchmark_v4_session_path_in_call1_and_call2() -> None:
    inventory, runtime_contract = _g07_inputs()
    view = _g07_view()

    call1 = build_call1_packet(view, inventory, runtime_contract)
    call2 = build_call2_packet(
        view,
        _saved_g07_plan(),
        inventory,
        runtime_contract,
    )

    expected_path = ["authenticated_customer_id"]
    for packet in (call1, call2):
        reference_task = packet.payload["input"]["reference_task"]
        assert (
            reference_task["benchmark_interpretation"]["record_conditions"]["session_path"]
            == expected_path
        )
        assert "remaining balance" in reference_task["semantic_failure_condition"]
        assert reference_task["safe_alternative"]
        inventory_key = (
            "operation_inventory" if packet.stage == "call2" else "environment_inventory"
        )
        assert len(packet.payload[inventory_key]["operations"]) == 7
        assert all(
            operation["description"] and operation["arguments"] and operation["result_schema"]
            for operation in packet.payload[inventory_key]["operations"]
        )
        assert "stimulus" not in reference_task
        assert "oracle" not in reference_task
    assert call2.payload["selected_material"]["operations"] == []


def test_prompt_policy_is_independent_of_structural_path_name(tmp_path: Path) -> None:
    source = {
        "gold_cases": [
            {
                "id": "neutral-path",
                "constraint_meaning": "Keep the supplied record in scope.",
                "safe_behaviors": ["Decline an out-of-scope record."],
                "record_conditions": {
                    "session_locator": ["authenticated_subject_id"],
                },
            }
        ]
    }
    source_path = tmp_path / "neutral.yaml"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    view = load_input(
        source_path,
        kind=InputKind.REFERENCE_TASK,
        reference_id="neutral-path",
    )

    packet = build_call1_packet(view, {}, {"setup_permissions": []})

    assert packet.payload["input"]["reference_task"]["benchmark_interpretation"][
        "record_conditions"
    ] == {"session_locator": ["authenticated_subject_id"]}


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "auth_token",
        "api-key",
        "APIKEY",
        "credential",
        "password",
        "authorization",
        "bearer",
        "endpoint",
        "base_url",
        "baseurl",
        "api__key",
        "base--url",
        "session_token",
    ],
)
def test_prompt_policy_rejects_secret_bearing_and_deceptive_keys(key: str) -> None:
    value = {"input": {key: "do-not-log-this-value"}}

    paths = prompt_secret_metadata_paths(value)
    assert paths == [f"input.{key}"]
    assert scan_for_prompt_secrets(value) == paths
    with pytest.raises(AuthoringError) as exc_info:
        assert_no_prompt_secrets(value)
    assert f"input.{key}" in str(exc_info.value)
    assert "do-not-log-this-value" not in str(exc_info.value)


def test_strict_package_and_response_policies_still_reject_session_path() -> None:
    value = {"record_conditions": {"session_path": ["authenticated_customer_id"]}}

    with pytest.raises(AuthoringError):
        assert_no_secrets(value)
    with pytest.raises(PackageIntegrityError):
        build_package(
            package_id="strict-policy",
            scenario_id="neutral",
            input_kind="reference-task",
            source_digests={"input": "a" * 64},
            members={"detector.py": b"source\n"},
            authoring=value,
        )


def test_correction_accepts_the_safe_g07_input_view_without_secret_values(
    tmp_path: Path,
) -> None:
    inventory, runtime_contract = _g07_inputs()
    transport = ScriptedAuthoringTransport([b"{}", b"{}"])

    result = AuthoringOrchestrator(
        transport=transport,
        package_dir=tmp_path / "package",
        task_id="g07-prompt-policy",
    ).run(_g07_view(), inventory, runtime_contract)

    assert result.status == "failed"
    assert len(transport.requests) == 2
    assert "correction" in result.prompts
    original_input = result.prompts["correction"].payload["original_request"]["payload"]["input"]
    assert original_input["reference_task"]["benchmark_interpretation"]["record_conditions"][
        "session_path"
    ] == ["authenticated_customer_id"]
