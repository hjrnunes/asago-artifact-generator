"""Prepare and finalize a reproducible five-case authoring freeze.

The old trial freeze was assembled outside the consumer repository.  This
module keeps the source and control-copy operation deterministic while leaving
model dispatch and semantic request inspection to the existing trial caller.
"""

from __future__ import annotations

import argparse
import copy
import json
import socket
import subprocess
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring import (
    ARTIFACT_REVIEW_PROMPT_VERSION,
    AUTHORING_CONTEXT_WINDOW_TOKENS,
    AUTHORING_MAX_COMPLETION_TOKENS,
    CALL1_PROMPT_VERSION_V5,
    CALL2_PROMPT_VERSION_V8,
    CONTEXT_GUARD_CALIBRATION,
    CORRECTION_PROMPT_VERSION_V8,
    CORRECTION_PROMPT_VERSION_V10,
    PLAN_REVIEW_PROMPT_VERSION,
    _context_budget_estimate,
    build_call1_packet_v2,
)
from asago_artifact_generator.qualification_inputs import _schema

from ._storage import _utc_now, _write_bytes_atomic, _write_json_atomic
from .errors import TrialInputError
from .inputs import CASE_ORDER, _mapping_sha256, _sha256, load_trial_case_inputs
from .receipts import _policy_control_digests, _read_frozen_policy

_INDEX_SCHEMA = "fresh-five-case-input-index-v1"
_RENDERED_INDEX_SCHEMA = "fresh-five-case-rendered-requests-v1"
_POLICY_SCHEMA = "fresh-five-case-frozen-policy-v1"
_CONSUMER_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DOWNSTREAM_ROOT = (
    _CONSUMER_ROOT.parent.parent.parent
    / ".worktrees"
    / "llm-designed-artifacts"
    / "asago-scenario-generator"
)


def _read_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialInputError(f"cannot read {description}: {path}") from exc
    if not isinstance(value, dict):
        raise TrialInputError(f"{description} must be an object: {path}")
    return value


def _write_text(path: Path, text: str) -> None:
    _write_bytes_atomic(path, text.encode("utf-8"))


def _git_info(path: Path) -> dict[str, Any]:
    """Return revision and status without changing or staging the checkout."""

    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(path), *args],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return result.stdout.strip()

    branch = run("branch", "--show-current")
    head = run("rev-parse", "HEAD")
    status_text = run("status", "--short", "--untracked-files=all")
    status = status_text.splitlines() if status_text else []
    return {
        "path": str(path),
        "branch": branch,
        "head": head,
        "git_status_short": status,
        "dirty": bool(status),
        "revision_available": head is not None,
    }


def _git_tracked(path: Path) -> bool:
    """Return whether ``path`` is tracked in its containing Git checkout."""

    try:
        root_result = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
        repository_root = Path(root_result.stdout.strip()).resolve()
        relative_path = path.resolve().relative_to(repository_root)
        subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "ls-files",
                "--error-unmatch",
                "--",
                str(relative_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError, ValueError):
        return False
    return True


def _refresh_specification(previous_policy: dict[str, Any]) -> dict[str, Any]:
    previous_specification = previous_policy.get("specification")
    if not isinstance(previous_specification, dict) or not isinstance(
        previous_specification.get("path"), str
    ):
        raise TrialInputError("previous policy does not name a specification file")
    path = Path(previous_specification["path"]).expanduser().resolve()
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise TrialInputError(f"specification file is unavailable: {path}") from exc
    return {
        "path": str(path),
        "sha256": _sha256(data),
        "tracked": _git_tracked(path),
        "refreshed_at_utc": _utc_now(),
    }


def _listed_ports(ports_policy: dict[str, Any]) -> tuple[int, ...]:
    values: set[int] = set()
    for key in ("gateway",):
        value = ports_policy.get(key)
        if isinstance(value, int):
            values.add(value)
    safe_targets = ports_policy.get("safe_targets")
    if isinstance(safe_targets, dict):
        values.update(value for value in safe_targets.values() if isinstance(value, int))
    forbidden = ports_policy.get("forbidden")
    if isinstance(forbidden, list):
        values.update(value for value in forbidden if isinstance(value, int))
    if not values:
        raise TrialInputError("previous policy does not list any loopback ports")
    return tuple(sorted(values))


def _measure_loopback_ports(ports_policy: dict[str, Any]) -> dict[str, Any]:
    ports = _listed_ports(ports_policy)
    checked_at = _utc_now()
    results: dict[str, str] = {}
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            results[str(port)] = f"not_free:{exc.__class__.__name__}"
        else:
            results[str(port)] = "free"
        finally:
            sock.close()
    return {
        "status": "measured",
        "binding": "127.0.0.1",
        "checked_at_utc": checked_at,
        "method": (
            "bind each listed TCP port on 127.0.0.1 and close immediately; "
            "no external network access"
        ),
        "ports": results,
        "all_free": all(result == "free" for result in results.values()),
        "pre_execution_requirement": (
            "Repeat this local-only check immediately before execution and require every "
            "listed port to be free."
        ),
    }


def _prior_spend(previous_root: Path) -> dict[str, Any]:
    batch_path = previous_root / "authoring" / "batch-status.json"
    budget_path = previous_root / "authoring" / "budget-ledger.json"
    execution_dir = previous_root / "execution"
    execution_path = execution_dir / "execution-summary.json"
    results_path = previous_root / "results.json"
    batch = _read_json(batch_path, "previous authoring batch status")
    budget = _read_json(budget_path, "previous authoring budget ledger")
    execution = _read_json(execution_path, "previous execution summary")
    results = _read_json(results_path, "previous results")

    batch_cases = batch.get("cases")
    if not isinstance(batch_cases, dict):
        raise TrialInputError("previous authoring batch status has no case outcomes")
    dispatched_by_task = budget.get("dispatched_by_task")
    dispatched_by_task_role = budget.get("dispatched_by_task_role")
    if not isinstance(dispatched_by_task, dict) or not isinstance(dispatched_by_task_role, dict):
        raise TrialInputError("previous authoring budget ledger has no dispatch counts")

    execution_totals = execution.get("totals")
    if not isinstance(execution_totals, dict):
        raise TrialInputError("previous execution summary has no totals")
    execution_cases = execution.get("cases")
    if not isinstance(execution_cases, list):
        raise TrialInputError("previous execution summary has no case outcomes")
    denominators = results.get("denominators")
    if not isinstance(denominators, dict):
        raise TrialInputError("previous results have no denominators")
    dispatch_totals = results.get("dispatch_totals")
    if not isinstance(dispatch_totals, dict) or not isinstance(
        dispatch_totals.get("author_review"), dict
    ):
        raise TrialInputError("previous results have no author/review dispatch totals")

    return {
        "batch_status": batch["status"],
        "aggregate_authoring_dispatches": batch["aggregate_dispatched"],
        "per_case_dispatches": copy.deepcopy(dispatched_by_task),
        "per_case_role_dispatches": copy.deepcopy(dispatched_by_task_role),
        "budget_ledger_total_dispatched": budget["total_dispatched"],
        "author_review_dispatch_totals": copy.deepcopy(dispatch_totals["author_review"]),
        "batch_case_outcomes": copy.deepcopy(batch_cases),
        "batch_live_dispatch_enabled": batch.get("live_dispatch_enabled"),
        "batch_transport_constructed": batch.get("transport_constructed"),
        "batch_transport_constructed_note": (
            "Copied verbatim. Before the fresh-trial runner fix, live mode never updated "
            "transport_constructed from its render-only value, so a recorded false does "
            "not show that no transport was built; use the dispatch counts."
        ),
        "execution": {
            "execution_directory_files": sorted(
                path.name for path in execution_dir.iterdir() if path.is_file()
            ),
            "execution_summary_status": execution["status"],
            "accepted_package_count": execution["authoring_summary"]["accepted_package_count"],
            "totals": copy.deepcopy(execution_totals),
            "case_execution_statuses": {
                item["case"]: item["execution_status"] for item in execution_cases
            },
            "results_status": results["status"],
            "results_reconciliation_live_calls": copy.deepcopy(
                results["reconciliation_live_calls"]
            ),
            "results_execution_eligible": copy.deepcopy(denominators["execution_eligible"]),
            "results_attempted_execution": copy.deepcopy(denominators["attempted_execution"]),
            "saved_file_statement": (
                "execution/execution-summary.json reports "
                f"status={execution['status']!r} and totals exactly as recorded; "
                f"results.json reports status={results['status']!r}, "
                "reconciliation_live_calls exactly as recorded, and the recorded "
                "execution_eligible and attempted_execution denominators. No additional "
                "execution conclusion is inferred."
            ),
        },
        "new_freeze_allowance_statement": (
            "This freeze's allowance is separate from the previous trial's spend and "
            "requires explicit owner approval before any live dispatch."
        ),
    }


def _previous_freeze_record(
    previous_root: Path,
    previous_policy: dict[str, Any],
) -> dict[str, Any]:
    previous_rendered_index = previous_root / "rendered-requests" / "index.json"
    try:
        rendered_index_sha256 = _sha256(previous_rendered_index.read_bytes())
        policy_sha256 = _sha256((previous_root / "frozen-policy.json").read_bytes())
        input_index_sha256 = _sha256((previous_root / "input-index.json").read_bytes())
    except OSError as exc:
        raise TrialInputError("previous freeze is missing a required digest source") from exc
    return {
        "run_dir": str(previous_root),
        "frozen_policy_sha256": policy_sha256,
        "input_index_sha256": input_index_sha256,
        "rendered_request_index_sha256": rendered_index_sha256,
        "refreeze_history_statement": (
            "The previous policy records its own refreeze history; this new freeze "
            "does not copy that history or its superseded paths."
        ),
        "prior_spend": _prior_spend(previous_root),
        "source_policy_status": previous_policy.get("status"),
    }


def _previous_route_verification(
    previous_root: Path,
    previous_policy: dict[str, Any],
) -> dict[str, Any]:
    route_path = previous_root / "route-compatibility.json"
    route_file = _read_json(route_path, "previous route compatibility")
    verified_against = route_file.get("verified_against")
    verified_head = (
        verified_against.get("downstream_head") if isinstance(verified_against, dict) else None
    )
    if not isinstance(verified_head, str):
        policy_route = previous_policy.get("route_compatibility")
        if isinstance(policy_route, dict) and isinstance(
            policy_route.get("verified_downstream_head"), str
        ):
            verified_head = policy_route["verified_downstream_head"]
    if not isinstance(verified_head, str):
        raise TrialInputError(
            "previous route compatibility does not name its verified downstream revision"
        )
    return {
        "verified_downstream_head": verified_head,
        "verified_against": copy.deepcopy(verified_against),
        "rechecked_at": route_file.get("rechecked_at"),
        "method": route_file.get("method"),
    }


def _validate_supplied_route_compatibility(
    route_file: dict[str, Any],
    path: Path,
) -> dict[str, Any]:
    verified_against = route_file.get("verified_against")
    if not isinstance(verified_against, dict):
        raise TrialInputError(f"supplied route compatibility lacks verified_against: {path}")
    verified_head = verified_against.get("downstream_head")
    if not isinstance(verified_head, str) or not verified_head.strip():
        raise TrialInputError(
            f"supplied route compatibility does not name its verified downstream revision: {path}"
        )
    for field in ("method", "rechecked_at"):
        value = route_file.get(field)
        if not isinstance(value, str) or not value.strip():
            raise TrialInputError(f"supplied route compatibility lacks required {field}: {path}")
    return {
        "verified_against": copy.deepcopy(verified_against),
        "verified_downstream_head": verified_head,
    }


def _read_supplied_route_compatibility(
    raw_path: str | Path,
) -> tuple[Path, bytes, dict[str, Any]]:
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve()
        data = resolved.read_bytes()
    except (OSError, RuntimeError) as exc:
        raise TrialInputError(f"supplied route compatibility is unavailable: {path}") from exc
    try:
        route_file = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialInputError(f"supplied route compatibility is invalid JSON: {resolved}") from exc
    if not isinstance(route_file, dict):
        raise TrialInputError(f"supplied route compatibility must be an object: {resolved}")
    _validate_supplied_route_compatibility(route_file, resolved)
    _supplied_consumer_revision(route_file, resolved)
    return resolved, data, route_file


def _supplied_consumer_revision(route_file: dict[str, Any], path: Path) -> str | None:
    """Return an optional consumer revision named by a route evidence file."""

    verified_against = route_file["verified_against"]
    containers = (verified_against, route_file)
    for container in containers:
        for key in ("consumer_head", "consumer_revision"):
            if key not in container:
                continue
            value = container[key]
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                for nested_key in ("head", "revision"):
                    nested_value = value.get(nested_key)
                    if isinstance(nested_value, str) and nested_value.strip():
                        return nested_value
            raise TrialInputError(f"supplied route compatibility has an invalid {key}: {path}")
    return None


def _resolve_previous_source(previous_root: Path, raw_path: str) -> tuple[Path, bytes]:
    path = Path(raw_path)
    resolved = path if path.is_absolute() else previous_root / path
    try:
        resolved = resolved.resolve()
        data = resolved.read_bytes()
    except (OSError, RuntimeError) as exc:
        raise TrialInputError(f"previous freeze source is unavailable: {raw_path}") from exc
    return resolved, data


def _fact_schema_regeneration(
    case_id: str,
    inventory: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Regenerate fact schemas with the qualification input schema function."""

    transformed = copy.deepcopy(inventory)
    facts = transformed.get("facts")
    if not isinstance(facts, list):
        raise TrialInputError(f"{case_id} inventory facts must be a list")
    changed_refs: list[str] = []
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict) or "value" not in fact:
            raise TrialInputError(f"{case_id} inventory fact {index} lacks a value")
        old_schema = fact.get("schema")
        new_schema = _schema(fact["value"])
        if old_schema != new_schema:
            changed_refs.append(str(fact.get("ref", index)))
        fact["schema"] = new_schema
    old_digest = _mapping_sha256(inventory)
    new_digest = _mapping_sha256(transformed)
    note = {
        "rule": "asago_artifact_generator.qualification_inputs._schema(fact.value)",
        "old_inventory_sha256": old_digest,
        "new_inventory_sha256": new_digest,
        "changed_fact_refs": changed_refs,
        "fact_count": len(facts),
        "semantic_invariant": (
            "Fact values, refs, provenance, source handles, operations, and all "
            "non-schema inventory fields are preserved."
        ),
    }
    return transformed, note


def _assert_only_fact_schemas_changed(
    old: dict[str, Any],
    new: dict[str, Any],
    case_id: str,
) -> None:
    old_without_schemas = copy.deepcopy(old)
    new_without_schemas = copy.deepcopy(new)
    old_facts = old_without_schemas.get("facts", [])
    new_facts = new_without_schemas.get("facts", [])
    if not isinstance(old_facts, list) or not isinstance(new_facts, list):
        raise TrialInputError(f"{case_id} inventory facts must be lists")
    for fact in old_facts:
        if isinstance(fact, dict):
            fact.pop("schema", None)
    for fact in new_facts:
        if isinstance(fact, dict):
            fact.pop("schema", None)
    if old_without_schemas != new_without_schemas:
        raise TrialInputError(f"{case_id} inventory transformation changed non-schema data")


def _control_set_digest(control_digests: dict[str, str]) -> str:
    return _mapping_sha256({f"{case_id}.json": control_digests[case_id] for case_id in CASE_ORDER})


def _current_prompt_versions() -> dict[str, str]:
    return {
        "call1": CALL1_PROMPT_VERSION_V5,
        "call2": CALL2_PROMPT_VERSION_V8,
        "plan_correction": CORRECTION_PROMPT_VERSION_V10,
        "artifact_correction": CORRECTION_PROMPT_VERSION_V8,
        "plan_review": PLAN_REVIEW_PROMPT_VERSION,
        "artifact_review": ARTIFACT_REVIEW_PROMPT_VERSION,
    }


def _refresh_policy_checkouts(
    policy: dict[str, Any],
    *,
    consumer_root: Path,
    downstream_root: Path,
) -> dict[str, str]:
    checkouts = policy.setdefault("checkouts", {})
    if not isinstance(checkouts, dict):
        checkouts = {}
        policy["checkouts"] = checkouts
    checkouts["CONSUMER"] = _git_info(consumer_root)
    checkouts["DOWNSTREAM"] = _git_info(downstream_root)
    checkout_audit = {
        name: "carried from the previous freeze and not re-read at this freeze"
        for name in ("ROOT", "TARGET", "HANDOFF", "GARAK")
    }
    checkout_audit.update(
        {
            "CONSUMER": "refreshed from the consumer checkout at freeze time",
            "DOWNSTREAM": "refreshed from the downstream checkout at freeze time",
        }
    )
    for name, reason in checkout_audit.items():
        if isinstance(checkouts.get(name), dict):
            checkouts[name]["freeze_refresh"] = reason
    revisions = policy.setdefault("revisions", {})
    if not isinstance(revisions, dict):
        revisions = {}
        policy["revisions"] = revisions
    revisions["consumer"] = checkouts["CONSUMER"]["head"]
    revisions["downstream"] = checkouts["DOWNSTREAM"]["head"]
    policy["checkout_audit"] = checkout_audit
    policy["revision_audit"] = {
        "consumer": checkout_audit["CONSUMER"],
        "downstream": checkout_audit["DOWNSTREAM"],
        "target": checkout_audit["TARGET"],
        "handoff": checkout_audit["HANDOFF"],
        "garak": checkout_audit["GARAK"],
    }
    policy["source_revision"] = {
        "consumer_head": checkouts["CONSUMER"]["head"],
        "consumer_branch": checkouts["CONSUMER"]["branch"],
        "consumer_dirty": checkouts["CONSUMER"]["dirty"],
        "consumer_status": checkouts["CONSUMER"]["git_status_short"],
        "downstream_head": checkouts["DOWNSTREAM"]["head"],
        "downstream_branch": checkouts["DOWNSTREAM"]["branch"],
        "downstream_dirty": checkouts["DOWNSTREAM"]["dirty"],
        "downstream_status": checkouts["DOWNSTREAM"]["git_status_short"],
        "dirty_worktree_policy": (
            "flagged; review and resolve dirty source changes before the real freeze/live run"
            if checkouts["CONSUMER"]["dirty"] or checkouts["DOWNSTREAM"]["dirty"]
            else "clean at freeze"
        ),
    }
    return checkout_audit


def _prepare_policy(
    previous_policy: dict[str, Any],
    *,
    previous_root: Path,
    run_dir: Path,
    input_index_bytes: bytes,
    control_digests: dict[str, str],
    consumer_root: Path,
    downstream_root: Path,
    transformation_notes: dict[str, Any],
    call1_estimates: dict[str, dict[str, Any]],
    supplied_route: tuple[Path, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    policy = copy.deepcopy(previous_policy)
    previous_freeze = _previous_freeze_record(previous_root, previous_policy)
    previous_route = _previous_route_verification(previous_root, previous_policy)
    specification = _refresh_specification(previous_policy)
    ports_policy = previous_policy.get("ports")
    if not isinstance(ports_policy, dict):
        raise TrialInputError("previous policy does not contain a ports policy")
    port_measurement = _measure_loopback_ports(ports_policy)
    checkout_audit = _refresh_policy_checkouts(
        policy,
        consumer_root=consumer_root,
        downstream_root=downstream_root,
    )

    policy["schema_version"] = _POLICY_SCHEMA
    policy["run_dir"] = str(run_dir)
    policy["frozen_at_utc"] = _utc_now()
    policy["status"] = "prepared_for_render_only"
    policy["prompt_and_schema_versions"] = {
        **copy.deepcopy(policy.get("prompt_and_schema_versions", {})),
        **_current_prompt_versions(),
        "wire_version": "v2",
        "authoring_interface": "artifact-authoring-v2",
        "input_index_schema": _INDEX_SCHEMA,
        "rendered_request_index_schema": _RENDERED_INDEX_SCHEMA,
    }
    policy["transformations"] = copy.deepcopy(transformation_notes)
    policy["previous_freeze"] = previous_freeze
    policy["specification"] = specification
    policy["input_index"] = {
        "path": "input-index.json",
        "schema_version": _INDEX_SCHEMA,
        "sha256": _sha256(input_index_bytes),
        "verified_case_count": len(CASE_ORDER),
        "verified_at_utc": _utc_now(),
        "verification": (
            "The freeze creator loaded all five cases offline and verified each "
            "indexed source digest before writing this policy."
        ),
    }

    route = copy.deepcopy(policy.get("route_compatibility", {}))
    route["path"] = "route-compatibility.json"
    route["sha256"] = _sha256((run_dir / "route-compatibility.json").read_bytes())
    route["source_revision_at_freeze"] = policy["source_revision"]["downstream_head"]
    route["previous_verified_downstream_head"] = previous_route["verified_downstream_head"]
    route["previous_verified_against"] = previous_route["verified_against"]
    route["previous_rechecked_at"] = previous_route["rechecked_at"]
    route["previous_method"] = previous_route["method"]
    if supplied_route is None:
        route["status"] = (
            "requires_revalidation"
            if route["previous_verified_downstream_head"]
            != policy["source_revision"]["downstream_head"]
            else "verified_at_current_downstream_head"
        )
    else:
        supplied_path, supplied_file = supplied_route
        supplied_verification = _validate_supplied_route_compatibility(
            supplied_file,
            supplied_path,
        )
        route["source_path"] = str(supplied_path)
        route["verified_against"] = supplied_verification["verified_against"]
        route["verified_downstream_head"] = supplied_verification["verified_downstream_head"]
        for key in ("method", "rechecked_at"):
            if key in supplied_file:
                route[key] = copy.deepcopy(supplied_file[key])
            else:
                route.pop(key, None)
        supplied_consumer_head = _supplied_consumer_revision(supplied_file, supplied_path)
        if supplied_consumer_head is None:
            route.pop("verified_consumer_head", None)
            route.pop("consumer_revision_mismatch", None)
        else:
            route["verified_consumer_head"] = supplied_consumer_head
            route["consumer_revision_mismatch"] = (
                supplied_consumer_head != policy["source_revision"]["consumer_head"]
            )
        route["status"] = (
            "verified_at_current_downstream_head"
            if route["verified_downstream_head"] == policy["source_revision"]["downstream_head"]
            else "requires_revalidation"
        )
    policy["route_compatibility"] = route

    old_allowances = policy.get("allowances")
    if not isinstance(old_allowances, dict):
        raise TrialInputError("previous policy does not contain allowances")
    allowances = copy.deepcopy(old_allowances)
    authoring_allowances = allowances.get("authoring_and_review")
    if not isinstance(authoring_allowances, dict):
        raise TrialInputError("previous policy does not contain authoring allowances")
    authoring_allowances["current_dispatches"] = 0
    authoring_allowances["budget_ledger_created"] = False
    authoring_allowances.pop("persisted_shared_budget", None)
    allowances["authoring_and_review"] = authoring_allowances
    allowances["allowance_scope"] = (
        "This freeze's allowance is separate from the previous trial's spend and "
        "requires explicit owner approval before any live dispatch."
    )
    policy["allowances"] = allowances

    old_controls = policy.get("controls")
    old_control_schema = (
        old_controls.get("schema_version")
        if isinstance(old_controls, dict)
        else "fresh-five-case-controls-v1"
    )
    policy["controls"] = {
        "path": "controls/",
        "schema_version": old_control_schema,
        "refreshed_at_utc": _utc_now(),
        "copied_from_previous_freeze": str(previous_root),
        "byte_identity_verified": True,
        "file_set_sha256": _control_set_digest(control_digests),
        "files": {
            case_id: {
                "path": f"controls/{case_id}.json",
                "sha256": control_digests[case_id],
            }
            for case_id in CASE_ORDER
        },
    }

    old_context = policy.get("context_guard_estimator")
    if not isinstance(old_context, dict):
        raise TrialInputError("previous policy does not contain context guard calibration")
    context_sources = copy.deepcopy(old_context.get("calibration_sources", []))
    current_model_facing_bytes = sum(
        estimate["model_facing_utf8_bytes"] for estimate in call1_estimates.values()
    )
    policy["context_guard_estimator"] = {
        "values_are_estimates": True,
        "calibration_calls_made": 0,
        "calibration_sources": context_sources,
        "calibration_sources_statement": (
            "These are historical offline calibration records carried from the previous "
            "freeze; no calibration call was made for this freeze."
        ),
        "formula": CONTEXT_GUARD_CALIBRATION["formula"],
        "total_model_facing_utf8_bytes": current_model_facing_bytes,
        "observed_conservative_ratio_formula": (
            "minimum(model_facing_utf8_bytes / provider_reported_usage_prompt_tokens)"
        ),
        "observed_conservative_ratio_bytes_per_prompt_token": CONTEXT_GUARD_CALIBRATION[
            "observed_conservative_bytes_per_token"
        ],
        "margin": CONTEXT_GUARD_CALIBRATION["margin"],
        "final_ratio_formula": CONTEXT_GUARD_CALIBRATION["ratio_formula"],
        "final_calibrated_ratio_bytes_per_estimated_token": CONTEXT_GUARD_CALIBRATION[
            "calibrated_bytes_per_token"
        ],
        "remaining_input_budget_estimate": (
            AUTHORING_CONTEXT_WINDOW_TOKENS - AUTHORING_MAX_COMPLETION_TOKENS - 256
        ),
        "completion_reserve_tokens": AUTHORING_MAX_COMPLETION_TOKENS,
        "framing_reserve_tokens": 256,
        "per_case_call1_estimates": copy.deepcopy(call1_estimates),
    }

    policy["phase_timeouts"] = copy.deepcopy(policy.get("phase_timeouts", {}))
    policy["phase_timeouts_audit"] = (
        "Carried from the previous freeze as owner-approved limits; route code was not "
        "re-read during this offline render-only preparation."
    )
    policy["amendments_audit"] = (
        "Carried from the previous freeze as owner amendments still in force; no amendment "
        "was changed by this render-only preparation."
    )
    policy["revision_audit"] = {
        **copy.deepcopy(policy.get("revision_audit", {})),
        "target": checkout_audit["TARGET"],
        "handoff": checkout_audit["HANDOFF"],
        "garak": checkout_audit["GARAK"],
    }
    policy["policy_audit"] = {
        "amendments": policy["amendments_audit"],
        "allowances": (
            "Limits were carried; current dispatch and ledger state were reset to this "
            "new freeze, with prior spend recorded under previous_freeze."
        ),
        "controls": (
            "Control files were copied byte-for-byte and rehashed into the new controls "
            "record and digests."
        ),
        "phase_timeouts": policy["phase_timeouts_audit"],
        "model_controls": (
            "Carried unchanged from the previous freeze because the accepted model, "
            "thinking, temperature, token, retry, and credential-handling controls remain "
            "the owner-approved controls."
        ),
        "prompt_and_schema_versions": (
            "Current prompt constants and current freeze/input/rendered schema versions "
            "were refreshed; unrelated package/control/caller schema identifiers were "
            "carried unchanged."
        ),
        "ports": (
            "The listed loopback ports were measured at freeze time; the result and "
            "pre-execution requirement are recorded under freeze_invariants."
        ),
        "checkouts": "; ".join(f"{name}: {reason}" for name, reason in checkout_audit.items()),
        "context_guard_estimator": (
            "Historical calibration sources were retained with an explicit statement; "
            "current Call 1 estimates were recomputed from the new inputs."
        ),
        "revisions": (
            "Consumer and downstream revisions were refreshed; target, handoff, and "
            "Garak revisions were carried and explicitly marked not re-read."
        ),
        "input_index": "Recomputed from the new input-index bytes and five offline-loaded cases.",
        "specification": "Rehashed and Git-tracked status recomputed from the current file.",
        "route_compatibility": (
            "Copied route evidence was inspected for its actual verified revision and "
            "compared with the current downstream HEAD."
        ),
        "digests": "Input-index and control digests were recomputed for this freeze.",
        "freeze_artifact_digests": (
            "New input-index, control-set, and route digests were written; rendered "
            "request and inspection digests are added during finalization."
        ),
        "freeze_invariants": (
            "Rebuilt for this render-only freeze; no previous refreeze or superseded "
            "invariant fields were retained."
        ),
        "rendered_requests": (
            "Written during finalization from the current offline render-only request "
            "bytes and context estimates."
        ),
        "run_dir": "Set to this new run directory.",
        "source_revision": (
            "Consumer and downstream source revisions were read at freeze time and "
            "dirty status was recorded."
        ),
        "status": "Set to prepared_for_render_only and finalized after rendering.",
        "transformations": "Rebuilt from this freeze's schema-only input transformation.",
        "previous_history": (
            "Previous refreeze keys were dropped; previous_freeze contains provenance, "
            "prior spend, and a pointer to the prior policy's history."
        ),
    }

    policy["freeze_invariants"] = {
        "product_and_tooling_frozen_after_this_file": True,
        "live_model_calls_made": 0,
        "calibration_calls_made": 0,
        "gateway_or_target_calls_made": 0,
        "services_started": 0,
        "caller_mode": "render-only",
        "caller_transport_constructed": False,
        "authoring_budget_ledger_exists": False,
        "authoring_reservations_or_dispatches": 0,
        "package_directories": 0,
        "execution_directories": 0,
        "ports_free_at_freeze": port_measurement,
        "pre_execution_port_check_required": True,
    }
    policy["digests"] = {
        "input_index_sha256": _sha256(input_index_bytes),
        "controls": control_digests,
    }
    policy["freeze_artifact_digests"] = {
        "input-index.json": _sha256(input_index_bytes),
        "controls_set": _control_set_digest(control_digests),
        "route-compatibility.json": policy.get("route_compatibility", {}).get("sha256"),
    }
    for key in ("refreeze", "refreezes", "refreeze_round_2", "refreeze_round_3"):
        policy.pop(key, None)
    for key in (
        "rendered_requests",
        "inspection_and_forbidden_scan",
        "rendered_request_files_set_sha256",
    ):
        policy.pop(key, None)
    return policy


def _prepare_index(
    previous_index: dict[str, Any],
    *,
    previous_root: Path,
    new_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    index = copy.deepcopy(previous_index)
    if index.get("schema_version") != _INDEX_SCHEMA:
        raise TrialInputError("previous freeze has an unsupported input-index schema")
    cases = index.get("cases")
    if not isinstance(cases, dict) or set(cases) != set(CASE_ORDER):
        raise TrialInputError("previous freeze input index must contain the five cases")

    saved_inputs: dict[str, bytes] = {}
    transformations: dict[str, Any] = {
        "schema_version": "fresh-five-case-freeze-transformations-v1",
        "inventory_fact_schema_regeneration": {},
        "source_invariants": (
            "Original source bytes and source digests are preserved. Only G07/A03 "
            "saved inventory fact schema fields and their truthful derived pins change."
        ),
    }
    copied_sources: dict[str, bytes] = {}

    for case_id in CASE_ORDER:
        entry = cases[case_id]
        sources = entry.get("sources")
        if not isinstance(sources, dict):
            raise TrialInputError(f"{case_id} previous source index is invalid")
        for source_name, descriptor in sources.items():
            if not isinstance(descriptor, dict) or not isinstance(descriptor.get("path"), str):
                raise TrialInputError(f"{case_id} source descriptor is invalid: {source_name}")
            source_path, data = _resolve_previous_source(previous_root, descriptor["path"])
            expected = descriptor.get("sha256")
            if expected != _sha256(data):
                raise TrialInputError(f"{case_id} previous source hash differs: {source_name}")
            if source_name == "saved_inputs" and case_id in {"G07", "A03"}:
                try:
                    saved = json.loads(data)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise TrialInputError(f"{case_id} saved inputs are invalid JSON") from exc
                if not isinstance(saved, dict):
                    raise TrialInputError(f"{case_id} saved inputs must be an object")
                old_inventory = saved.get("inventory")
                runtime_contract = saved.get("runtime_contract")
                pins = saved.get("authoring_input_pins")
                if (
                    not isinstance(old_inventory, dict)
                    or not isinstance(runtime_contract, dict)
                    or not isinstance(pins, dict)
                ):
                    raise TrialInputError(f"{case_id} saved inputs lack required mappings")
                new_inventory, note = _fact_schema_regeneration(case_id, old_inventory)
                _assert_only_fact_schemas_changed(old_inventory, new_inventory, case_id)
                new_pins = copy.deepcopy(pins)
                new_pins["inventory_sha256"] = _mapping_sha256(new_inventory)
                if new_pins.get("runtime_contract_sha256") != _mapping_sha256(runtime_contract):
                    raise TrialInputError(f"{case_id} saved runtime contract pin is not truthful")
                if new_pins.get("input_sha256") != pins.get("input_sha256"):
                    raise TrialInputError(f"{case_id} saved input pin changed unexpectedly")
                if new_pins.get("source_digests") != pins.get("source_digests"):
                    raise TrialInputError(f"{case_id} saved source digests changed unexpectedly")
                rewritten = copy.deepcopy(saved)
                rewritten["inventory"] = new_inventory
                rewritten["authoring_input_pins"] = new_pins
                new_bytes = (
                    json.dumps(
                        rewritten,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
                destination = new_root / "inputs" / case_id / "saved_inputs.json"
                destination_relative = str(destination.relative_to(new_root))
                saved_inputs[destination_relative] = new_bytes
                descriptor["path"] = destination_relative
                descriptor["sha256"] = _sha256(new_bytes)
                entry["saved_input"] = {
                    **copy.deepcopy(entry.get("saved_input", {})),
                    "path": destination_relative,
                    "sha256": _sha256(new_bytes),
                    "role": "designated saved input with regenerated fact schemas",
                }
                entry.setdefault("digests", {})["inventory_sha256"] = _mapping_sha256(
                    new_inventory
                )
                entry["digests"]["authoring_input_pins_sha256"] = _mapping_sha256(new_pins)
                transformations["inventory_fact_schema_regeneration"][case_id] = {
                    **note,
                    "saved_inputs_path": destination_relative,
                    "source_saved_inputs_path": str(source_path),
                    "source_saved_inputs_sha256": _sha256(data),
                }
                continue
            if not Path(descriptor["path"]).is_absolute():
                copied_sources[descriptor["path"]] = data

    index["created_at"] = _utc_now()
    index["parent_freeze"] = {
        "path": str(previous_root),
        "input_index_sha256": _sha256((previous_root / "input-index.json").read_bytes()),
        "frozen_policy_sha256": _sha256((previous_root / "frozen-policy.json").read_bytes()),
    }
    index["transformations"] = transformations
    return index, transformations, {**copied_sources, **saved_inputs}


def create_freeze(
    previous_freeze: str | Path,
    run_dir: str | Path,
    *,
    consumer_root: str | Path | None = None,
    downstream_root: str | Path | None = None,
    route_compatibility: str | Path | None = None,
) -> dict[str, Any]:
    """Create a new freeze without overwriting either freeze directory."""

    previous_root = Path(previous_freeze).expanduser().resolve()
    new_root = Path(run_dir).expanduser().resolve()
    if not previous_root.is_dir():
        raise TrialInputError(f"previous freeze directory does not exist: {previous_root}")
    if new_root.exists():
        raise TrialInputError(f"refusing to overwrite existing run directory: {new_root}")

    previous_policy = _read_frozen_policy(previous_root)
    previous_index = _read_json(previous_root / "input-index.json", "previous input index")
    previous_cases = load_trial_case_inputs(previous_root)
    previous_control_digests = _policy_control_digests(previous_policy)
    for case in previous_cases:
        if case.controls_sha256 != previous_control_digests[case.case_id]:
            raise TrialInputError(f"previous control digest mismatch for {case.case_id}")

    consumer_path = Path(consumer_root).expanduser().resolve() if consumer_root else _CONSUMER_ROOT
    if downstream_root is None:
        previous_checkouts = previous_policy.get("checkouts", {})
        candidate = (
            previous_checkouts.get("DOWNSTREAM", {}).get("path")
            if isinstance(previous_checkouts, dict)
            and isinstance(previous_checkouts.get("DOWNSTREAM"), dict)
            else None
        )
        downstream_path = (
            Path(candidate).expanduser().resolve() if candidate else _DEFAULT_DOWNSTREAM_ROOT
        )
    else:
        downstream_path = Path(downstream_root).expanduser().resolve()

    supplied_route: tuple[Path, bytes, dict[str, Any]] | None = None
    if route_compatibility is not None:
        supplied_route = _read_supplied_route_compatibility(route_compatibility)

    index, transformations, files_to_copy = _prepare_index(
        previous_index,
        previous_root=previous_root,
        new_root=new_root,
    )
    index_bytes = (
        json.dumps(index, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    control_bytes: dict[str, bytes] = {}
    for case_id in CASE_ORDER:
        path = previous_root / "controls" / f"{case_id}.json"
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise TrialInputError(f"previous control file is unavailable: {case_id}") from exc
        if _sha256(data) != previous_control_digests[case_id]:
            raise TrialInputError(f"previous control file hash differs: {case_id}")
        control_bytes[case_id] = data

    # Build the destination only after every source, control, and checkout has
    # been validated.  mkdir(exist_ok=False) makes accidental reuse explicit.
    new_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        new_root.mkdir()
    except FileExistsError as exc:
        raise TrialInputError(f"refusing to overwrite existing run directory: {new_root}") from exc

    try:
        _write_bytes_atomic(new_root / "input-index.json", index_bytes)
        for relative_name, data in files_to_copy.items():
            _write_bytes_atomic(new_root / relative_name, data)
        for case_id, data in control_bytes.items():
            _write_bytes_atomic(new_root / "controls" / f"{case_id}.json", data)

        if supplied_route is not None:
            route_path, route_bytes, route_file = supplied_route
            _write_bytes_atomic(new_root / "route-compatibility.json", route_bytes)
            policy_route_source = (route_path, route_file)
        else:
            previous_route = previous_root / "route-compatibility.json"
            if previous_route.is_file():
                _write_bytes_atomic(
                    new_root / "route-compatibility.json",
                    previous_route.read_bytes(),
                )
            policy_route_source = None

        policy = _prepare_policy(
            previous_policy,
            previous_root=previous_root,
            run_dir=new_root,
            input_index_bytes=index_bytes,
            control_digests={case_id: _sha256(data) for case_id, data in control_bytes.items()},
            consumer_root=consumer_path,
            downstream_root=downstream_path,
            transformation_notes=transformations,
            call1_estimates=_call1_estimates(tuple(load_trial_case_inputs(new_root))),
            supplied_route=policy_route_source,
        )
        _write_json_atomic(new_root / "frozen-policy.json", policy)
        load_trial_case_inputs(new_root)
        return {
            "run_dir": str(new_root),
            "input_index_sha256": _sha256(index_bytes),
            "control_digests": policy["digests"]["controls"],
            "transformations": transformations,
            "source_revision": policy["source_revision"],
            "status": policy["status"],
        }
    except Exception:
        # The destination is intentionally not reused after a failed freeze.
        # Leave the partial directory for diagnosis rather than touching a
        # path the caller may have populated concurrently.
        raise


def _inspection_markdown(
    run_dir: Path,
    cases: tuple[Any, ...],
    rendered: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> str:
    ratio = CONTEXT_GUARD_CALIBRATION["calibrated_bytes_per_token"]
    remaining = AUTHORING_CONTEXT_WINDOW_TOKENS - AUTHORING_MAX_COMPLETION_TOKENS - 256
    lines = [
        "# Frozen Call 1 request inspection",
        "",
        f"- Run directory: `{run_dir}`",
        f"- Generated at: `{_utc_now()}`",
        f"- Input-index SHA-256: `{_sha256((run_dir / 'input-index.json').read_bytes())}`",
        (
            "- Rendered-request index SHA-256: "
            f"`{_sha256((run_dir / 'rendered-requests' / 'index.json').read_bytes())}`"
        ),
        "- Mode: `--render-only`; no transport or model endpoint is constructed.",
        "",
        "## Call 1 sizes and context preflight",
        "",
        (
            "The estimates use the current authoring context guard: "
            f"`ceil(model-facing UTF-8 bytes / {ratio})`, with a "
            f"`{remaining}`-token input budget after the completion and framing reserves."
        ),
        "",
        (
            "| Case | Prompt version | User bytes | Model-facing bytes | "
            "Estimated tokens | Budget result |"
        ),
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    estimates = _call1_estimates(cases)
    for case in cases:
        estimate = estimates[case.case_id]
        entry = rendered[case.case_id]
        status = "fit" if estimate["estimated_prompt_tokens"] <= remaining else "overflow"
        lines.append(
            f"| {case.case_id} | {entry['prompt_version']} | {entry['user_bytes']} | "
            f"{estimate['model_facing_utf8_bytes']} | {estimate['estimated_prompt_tokens']} | "
            f"{status} |"
        )
    lines.extend(
        [
            "",
            "## Required human inspection before live dispatch",
            "",
            "- Read the exact saved system and user bytes for all five cases.",
            "- Confirm supplied meaning, facts, provenance, operation/result schemas, and limits.",
            (
                "- Confirm every copied handle is defined and fixture identifiers "
                "are distinguished from live values."
            ),
            "- Run the spec's forbidden-material scan against current request bytes.",
            (
                "- Revalidate `route-compatibility.json` against the recorded "
                "downstream source revision."
            ),
            "",
            "This file records deterministic byte and context measurements. It does not "
            "claim that the required semantic or forbidden-material inspection is complete.",
        ]
    )
    return "\n".join(lines) + "\n"


def _call1_estimates(cases: tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    remaining = AUTHORING_CONTEXT_WINDOW_TOKENS - AUTHORING_MAX_COMPLETION_TOKENS - 256
    estimates: dict[str, dict[str, Any]] = {}
    for case in cases:
        packet = build_call1_packet_v2(case.input_view, case.inventory, case.runtime_contract)
        estimate = _context_budget_estimate(packet)
        estimates[case.case_id] = {
            "system_bytes": estimate["system_bytes"],
            "user_bytes": estimate["user_bytes"],
            "model_facing_utf8_bytes": estimate["model_facing_utf8_bytes"],
            "estimated_prompt_tokens": estimate["estimated_prompt_tokens"],
            "remaining_input_budget_estimate": remaining,
            "fit_by_estimate": estimate["estimated_prompt_tokens"] <= remaining,
        }
    return estimates


def finalize_renderings(run_dir: str | Path) -> dict[str, Any]:
    """Pin the current render-only request index and write the inspection record."""

    root = Path(run_dir).expanduser().resolve()
    if not root.is_dir():
        raise TrialInputError(f"run directory does not exist: {root}")
    policy = _read_frozen_policy(root)
    cases = tuple(load_trial_case_inputs(root))
    rendered_index_path = root / "rendered-requests" / "index.json"
    rendered_index = _read_json(rendered_index_path, "rendered request index")
    if rendered_index.get("schema_version") != _RENDERED_INDEX_SCHEMA:
        raise TrialInputError("rendered request index has an unsupported schema")
    rendered = rendered_index.get("cases")
    if not isinstance(rendered, dict) or set(rendered) != set(CASE_ORDER):
        raise TrialInputError("rendered request index must contain the five cases")
    for case in cases:
        entry = rendered[case.case_id]
        for role in ("system", "user"):
            file_name = entry.get(f"{role}_file")
            if not isinstance(file_name, str) or Path(file_name).name != file_name:
                raise TrialInputError(f"{case.case_id} has an invalid rendered {role} file")
            path = root / "rendered-requests" / file_name
            data = path.read_bytes()
            if entry.get(f"{role}_sha256") != _sha256(data):
                raise TrialInputError(f"{case.case_id} rendered {role} digest is not truthful")
    rendered_index_sha = _sha256(rendered_index_path.read_bytes())
    control_digests = _policy_control_digests(policy)
    call1_estimates = _call1_estimates(cases)
    _write_text(
        root / "request-inspection.md",
        _inspection_markdown(root, cases, rendered, policy),
    )
    policy["status"] = "frozen_render_only"
    policy["rendered_at_utc"] = _utc_now()
    context_estimator = copy.deepcopy(policy.get("context_guard_estimator", {}))
    context_estimator.update(
        {
            "values_are_estimates": True,
            "formula": CONTEXT_GUARD_CALIBRATION["formula"],
            "observed_conservative_bytes_per_token": CONTEXT_GUARD_CALIBRATION[
                "observed_conservative_bytes_per_token"
            ],
            "margin": CONTEXT_GUARD_CALIBRATION["margin"],
            "final_calibrated_bytes_per_estimated_token": CONTEXT_GUARD_CALIBRATION[
                "calibrated_bytes_per_token"
            ],
            "remaining_input_budget": AUTHORING_CONTEXT_WINDOW_TOKENS
            - AUTHORING_MAX_COMPLETION_TOKENS
            - 256,
            "completion_reserve_tokens": AUTHORING_MAX_COMPLETION_TOKENS,
            "framing_reserve_tokens": 256,
            "per_case_call1_estimates": call1_estimates,
        }
    )
    policy["context_guard_estimator"] = context_estimator
    policy["digests"] = {
        "input_index_sha256": _sha256((root / "input-index.json").read_bytes()),
        "rendered_requests_index_sha256": rendered_index_sha,
        "controls": control_digests,
    }
    policy["rendered_requests"] = {
        "path": "rendered-requests/",
        "schema_version": _RENDERED_INDEX_SCHEMA,
        "index_sha256": rendered_index_sha,
        "cases": copy.deepcopy(rendered),
        "context_budget": {
            "context_window_tokens": AUTHORING_CONTEXT_WINDOW_TOKENS,
            "max_completion_tokens": AUTHORING_MAX_COMPLETION_TOKENS,
            "framing_reserve_tokens": 256,
            "remaining_input_budget_estimate": (
                AUTHORING_CONTEXT_WINDOW_TOKENS - AUTHORING_MAX_COMPLETION_TOKENS - 256
            ),
            "calibration": copy.deepcopy(CONTEXT_GUARD_CALIBRATION),
            "per_case_call1_estimates": call1_estimates,
        },
    }
    policy["freeze_artifact_digests"] = {
        **copy.deepcopy(policy.get("freeze_artifact_digests", {})),
        "input-index.json": policy["digests"]["input_index_sha256"],
        "rendered-requests/index.json": rendered_index_sha,
        "request-inspection.md": _sha256((root / "request-inspection.md").read_bytes()),
        "controls_set": _control_set_digest(control_digests),
    }
    _write_json_atomic(root / "frozen-policy.json", policy)
    return {
        "run_dir": str(root),
        "rendered_requests_index_sha256": rendered_index_sha,
        "request_inspection_sha256": policy["freeze_artifact_digests"]["request-inspection.md"],
        "prompt_versions": {
            case_id: rendered[case_id]["prompt_version"] for case_id in CASE_ORDER
        },
        "status": policy["status"],
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or finalize a frozen five-case authoring trial."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--create",
        action="store_true",
        help="Create a new freeze from a previous one.",
    )
    mode.add_argument(
        "--finalize-renderings",
        action="store_true",
        help="Pin render-only request digests in an existing freeze.",
    )
    parser.add_argument("--previous-freeze", type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--consumer-root", type=Path)
    parser.add_argument("--downstream-root", type=Path)
    parser.add_argument(
        "--route-compatibility",
        type=Path,
        help="Use and copy a validated route-compatibility JSON file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        if args.create:
            if args.previous_freeze is None:
                raise TrialInputError("--previous-freeze is required with --create")
            result = create_freeze(
                args.previous_freeze,
                args.run_dir,
                consumer_root=args.consumer_root,
                downstream_root=args.downstream_root,
                route_compatibility=args.route_compatibility,
            )
        else:
            result = finalize_renderings(args.run_dir)
    except (OSError, TrialInputError, ValueError) as exc:
        print(f"freeze tooling stopped: {exc}", flush=True)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


__all__ = [
    "create_freeze",
    "finalize_renderings",
    "build_argument_parser",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
