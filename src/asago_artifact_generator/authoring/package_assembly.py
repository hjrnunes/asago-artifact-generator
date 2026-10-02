"""Immutable package assembly from accepted responses, plus blocked-plan persistence."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from ..failure_evidence import metadata_record
from ..input_adapter import InputView
from ..package_io import ArtifactPackage, build_package
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
    detector_bytes: bytes,
    policy: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    review_status: dict[str, str] | None = None,
    preserved_reviews: dict[str, dict[str, Any]] | None = None,
    terminal_status: str | None = None,
) -> ArtifactPackage:
    authoring_records: dict[str, bytes] = {}
    for index, record in enumerate(ledger, start=1):
        stage = record["stage"]
        package_record = dict(record)
        if terminal_status is not None:
            package_record["terminal_status"] = terminal_status
            package_record["stage_status"] = terminal_status
        authoring_records[f"authoring/{index:02d}-{stage}.json"] = (
            _canonical_json(package_record).encode("utf-8") + b"\n"
        )
        raw = raw_responses.get(record.get("raw_response_key", ""))
        if raw is None:
            raw = raw_responses.get(stage)
        if raw is not None:
            authoring_records[f"authoring/{index:02d}-{stage}.raw"] = raw
        prompt_user = record.get("prompt_user")
        if isinstance(prompt_user, str):
            authoring_records[f"authoring/{index:02d}-{stage}.prompt"] = prompt_user.encode(
                "utf-8"
            )
        else:
            packet = prompt_packets.get(stage)
            if packet is not None:
                authoring_records[f"authoring/{index:02d}-{stage}.prompt"] = packet.user.encode(
                    "utf-8"
                )
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
    package_ledger = [
        {
            **record,
            **(
                {
                    "terminal_status": terminal_status,
                    "stage_status": terminal_status,
                }
                if terminal_status is not None
                else {}
            ),
        }
        for record in ledger
    ]
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
        "detector.py": detector_bytes,
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
    safe_ledger = [
        {
            key: value
            for key, value in (
                {
                    **record,
                    **(
                        {
                            "terminal_status": terminal_status,
                            "stage_status": terminal_status,
                        }
                        if terminal_status is not None
                        else {}
                    ),
                }
            ).items()
            if key not in {"prompt_system", "prompt_user"} and not (key == "usage" and not value)
        }
        for record in ledger
    ]

    def summary_usage(record: dict[str, Any]) -> dict[str, Any]:
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

    authoring_summary = {
        "interface": AUTHORING_INTERFACE_VERSION_V2,
        "attempts": len(ledger),
        "correction_used": any(record["stage"] == "correction" for record in ledger),
        "max_retries": 0,
        "usage": [summary_usage(record) for record in ledger],
        "ledger": safe_ledger,
        "authoring_input_pins": authoring_input_pins,
    }
    if policy is not None:
        authoring_summary["policy"] = dict(policy)
    if budget is not None:
        authoring_summary["budget"] = dict(budget)
    if review_status is not None:
        authoring_summary["review_status"] = dict(review_status)
    if terminal_status is not None:
        authoring_summary["status"] = terminal_status
        authoring_summary["terminal_status"] = terminal_status
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
        matching = [
            record.get("review")
            for record in ledger
            if record.get("stage") == stage and isinstance(record.get("review"), dict)
        ]
        if requested_status == "not_requested":
            records[key] = {"status": "not_requested"}
        elif matching:
            records[key] = deepcopy(matching[-1])
        elif isinstance(preserved_reviews, dict) and isinstance(preserved_reviews.get(key), dict):
            records[key] = deepcopy(preserved_reviews[key])
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
