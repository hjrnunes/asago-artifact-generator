"""Immutable package assembly from accepted responses, plus blocked-plan persistence."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from ..failure_evidence import metadata_record
from ..input_adapter import InputView
from ..package_io import (
    TOOL_CALL_CONDITION_MEMBER,
    ArtifactPackage,
    build_package,
    tool_call_condition_bytes,
)
from .core import AUTHORING_INTERFACE_VERSION_V2, PromptPacket, _canonical_json, _json_bytes
from .inventory import (
    _expected_authoring_input_pins,
    _input_view_payload,
    _resolved_judge_spec,
    _source_input_payload,
)
from .prompt_safety import assert_no_secrets


def _package_from_responses(
    *,
    view: InputView,
    plan: dict[str, Any],
    artifact: dict[str, Any],
    task_id: str,
    ledger: list[dict[str, Any]],
    raw_responses: dict[str, bytes],
    decoded_responses: dict[str, Any],
    prompt_packets: dict[str, PromptPacket],
    transformations: list[Any],
    inventory: dict[str, Any],
    runtime_contract: dict[str, Any],
    discovery_provenance: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    review_status: dict[str, str] | None = None,
    preserved_reviews: dict[str, dict[str, Any]] | None = None,
    terminal_status: str | None = None,
) -> ArtifactPackage:
    authoring_records = _attempt_records(ledger, raw_responses, prompt_packets, terminal_status)
    authoring_records["authoring/transformations.json"] = (
        _canonical_json(transformations).encode("utf-8") + b"\n"
    )
    review_records = _package_review_records(
        ledger,
        review_status=review_status,
        preserved_reviews=preserved_reviews,
    )
    authoring_records["authoring/reviews.json"] = (
        _canonical_json(review_records).encode("utf-8") + b"\n"
    )
    package_ledger = [{**record, **_terminal_fields(terminal_status)} for record in ledger]
    authoring_records["authoring/ledger.json"] = (
        _canonical_json(package_ledger).encode("utf-8") + b"\n"
    )
    authoring_input_pins = _expected_authoring_input_pins(
        input_view=view,
        inventory=inventory,
        runtime_contract=runtime_contract,
    )
    members = {
        "plan.json": _json_bytes(plan),
        "stimulus.json": _json_bytes(artifact["stimulus"]),
        "setup.json": _json_bytes(artifact["setup_recipe"]),
        "bindings.json": _json_bytes(artifact["runtime_bindings"]),
        "prerequisites.json": _json_bytes(artifact["prerequisites"]),
        "checks.json": _json_bytes(
            {"interface": AUTHORING_INTERFACE_VERSION_V2, "status": "structurally_valid"}
        ),
        "inputs.json": _json_bytes(
            {
                "model_facing_input": _input_view_payload(view),
                "source_input": _source_input_payload(view),
                "inventory": inventory,
                "discovery_provenance": deepcopy(discovery_provenance or {}),
                "runtime_contract": runtime_contract,
                "authoring_input_pins": authoring_input_pins,
            }
        ),
        "source-hashes.json": _json_bytes(view.source_digests),
        "observations.json": _json_bytes(artifact["required_observations"]),
        "explanation.json": _json_bytes({"text": artifact["explanation"]}),
        "examples.json": _json_bytes(artifact["examples"]),
        **authoring_records,
    }
    resolved_judge = _resolved_judge_spec(artifact["semantic_judge_spec"], inventory)
    if resolved_judge is not None:
        members["judge.json"] = _json_bytes(resolved_judge)
    condition = view.tool_call_condition
    if condition is not None:
        members[TOOL_CALL_CONDITION_MEMBER] = tool_call_condition_bytes(condition)
    authoring_summary = {
        "interface": AUTHORING_INTERFACE_VERSION_V2,
        "attempts": len(ledger),
        "correction_used": any(record["stage"] == "correction" for record in ledger),
        "max_retries": 0,
        "usage": [_summary_usage(record) for record in ledger],
        "ledger": _safe_ledger(ledger, terminal_status),
        "authoring_input_pins": authoring_input_pins,
    }
    authoring_summary.update(
        _optional_summary_fields(policy, budget, review_status, terminal_status)
    )
    creation_model = {"model": "configured-private-authoring", "controls": {"max_retries": 0}}
    assert_no_secrets({"authoring": authoring_summary, "creation_model": creation_model})
    package_id = f"{task_id}-{view.scenario_id}"
    return build_package(
        package_id=package_id,
        scenario_id=view.scenario_id,
        input_kind=view.kind.value,
        source_digests=view.source_digests or {"input": view.source_sha256},
        members=members,
        authoring=authoring_summary,
        runtime_capabilities=runtime_contract,
        creation_model=creation_model,
    )


def _terminal_fields(terminal_status: str | None) -> dict[str, str]:
    if terminal_status is None:
        return {}
    return {"terminal_status": terminal_status, "stage_status": terminal_status}


def _attempt_records(
    ledger: list[dict[str, Any]],
    raw_responses: dict[str, bytes],
    prompt_packets: dict[str, PromptPacket],
    terminal_status: str | None,
) -> dict[str, bytes]:
    """Return each ledger attempt's record, raw response, and user prompt files."""

    authoring_records: dict[str, bytes] = {}
    for index, record in enumerate(ledger, start=1):
        prefix = f"authoring/{index:02d}-{record['stage']}"
        package_record = dict(record)
        package_record.update(_terminal_fields(terminal_status))
        authoring_records[f"{prefix}.json"] = (
            _canonical_json(package_record).encode("utf-8") + b"\n"
        )
        raw = _attempt_raw_response(record, raw_responses)
        if raw is not None:
            authoring_records[f"{prefix}.raw"] = raw
        prompt = _attempt_user_prompt(record, prompt_packets)
        if prompt is not None:
            authoring_records[f"{prefix}.prompt"] = prompt.encode("utf-8")
    return authoring_records


def _attempt_raw_response(record: dict[str, Any], raw_responses: dict[str, bytes]) -> bytes | None:
    raw = raw_responses.get(record.get("raw_response_key", ""))
    if raw is None:
        raw = raw_responses.get(record["stage"])
    return raw


def _attempt_user_prompt(
    record: dict[str, Any], prompt_packets: dict[str, PromptPacket]
) -> str | None:
    """Return the recorded user prompt, or the stage packet's prompt when none was recorded."""

    prompt_user = record.get("prompt_user")
    if isinstance(prompt_user, str):
        return prompt_user
    packet = prompt_packets.get(record["stage"])
    return packet.user if packet is not None else None


def _safe_ledger(
    ledger: list[dict[str, Any]], terminal_status: str | None
) -> list[dict[str, Any]]:
    """Return ledger records without prompts or empty usage for the authoring summary."""

    return [
        {
            key: value
            for key, value in {**record, **_terminal_fields(terminal_status)}.items()
            if key not in {"prompt_system", "prompt_user"} and not (key == "usage" and not value)
        }
        for record in ledger
    ]


def _summary_usage(record: dict[str, Any]) -> dict[str, Any]:
    usage = record.get("usage")
    # Existing ledgers can already carry the failure-evidence metadata
    # envelope. Preserve it so the manifest scanner validates the closed
    # shape instead of treating the envelope as provider counters.
    if isinstance(usage, dict) and "availability" in usage:
        return deepcopy(usage)
    return metadata_record(
        usage if usage else None,
        unavailable_reason="provider_did_not_report_usage",
    )


def _optional_summary_fields(
    policy: dict[str, Any] | None,
    budget: dict[str, Any] | None,
    review_status: dict[str, str] | None,
    terminal_status: str | None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if policy is not None:
        fields["policy"] = dict(policy)
    if budget is not None:
        fields["budget"] = dict(budget)
    if review_status is not None:
        fields["review_status"] = dict(review_status)
    if terminal_status is not None:
        fields["status"] = terminal_status
        fields["terminal_status"] = terminal_status
    return fields


def _latest_stage_review(ledger: list[dict[str, Any]], stage: str) -> dict[str, Any] | None:
    reviews = [
        record.get("review")
        for record in ledger
        if record.get("stage") == stage and isinstance(record.get("review"), dict)
    ]
    return reviews[-1] if reviews else None


def _package_review_records(
    ledger: list[dict[str, Any]],
    *,
    review_status: dict[str, str] | None,
    preserved_reviews: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the package-owned review truth, including disabled stages."""

    records: dict[str, Any] = {}
    for stage, key in (("plan_review", "plan"), ("artifact_review", "artifact")):
        requested_status = (review_status or {}).get(key)
        latest = _latest_stage_review(ledger, stage)
        preserved = preserved_reviews.get(key) if isinstance(preserved_reviews, dict) else None
        if requested_status == "not_requested":
            records[key] = {"status": "not_requested"}
        elif latest is not None:
            records[key] = deepcopy(latest)
        elif isinstance(preserved, dict):
            records[key] = deepcopy(preserved)
        else:
            records[key] = {
                "status": requested_status or "not_requested",
            }
    return {
        "schema_version": "authoring-review-evidence-v1",
        "plan": records["plan"],
        "artifact": records["artifact"],
    }


def _persist_blocked_plan(destination: Path, plan: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = destination.with_suffix(destination.suffix + ".blocked.json")
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_bytes(
        _json_bytes({"status": "blocked", "plan": plan, "package_path": str(destination)})
    )
    temporary.replace(target)
