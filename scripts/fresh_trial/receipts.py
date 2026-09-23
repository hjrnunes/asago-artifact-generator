"""Frozen-state checks, case receipts, and batch evidence helpers."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring import build_call1_packet_v2

from ._storage import _write_json_atomic
from .accounting import PersistedAuthoringBudget
from .inputs import (
    CASE_ORDER as _CASE_ORDER,
)
from .inputs import (
    TrialCaseInputs,
    TrialInputError,
    _is_sha256,
    _sha256,
)

_BATCH_SCHEMA_VERSION = "fresh-five-case-batch-status-v1"

_CASE_RECEIPT_SCHEMA_VERSION = "fresh-five-case-receipt-v1"
CONSUMER_ROOT = Path(__file__).resolve().parents[2]


def _read_frozen_policy(run_dir: Path) -> dict[str, Any]:
    """Load policy pinned by ``digests.input_index_sha256``.

    Live validation also requires ``digests.rendered_requests_index_sha256``
    and ``digests.controls`` keyed by all five case IDs.
    """

    policy_path = run_dir / "frozen-policy.json"
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialInputError(
            f"live dispatch requires a valid frozen policy: {policy_path}"
        ) from exc
    if not isinstance(policy, dict):
        raise TrialInputError("frozen policy must be an object")
    index_bytes = (run_dir / "input-index.json").read_bytes()
    expected = _policy_input_index_digest(policy)
    if expected is None or expected != _sha256(index_bytes):
        raise TrialInputError("input-index.json does not match the frozen-policy digest")
    return policy


def _policy_digests(policy: dict[str, Any]) -> dict[str, Any]:
    raw = policy.get("digests")
    return raw if isinstance(raw, dict) else {}


def _policy_input_index_digest(policy: dict[str, Any]) -> str | None:
    digests = _policy_digests(policy)
    for key in ("input_index_sha256", "input-index.json", "input_index"):
        value = digests.get(key)
        if isinstance(value, dict):
            value = value.get("sha256")
        if _is_sha256(value):
            return value
    return None


def _policy_rendered_index_digest(policy: dict[str, Any]) -> str | None:
    digests = _policy_digests(policy)
    for key in (
        "rendered_requests_index_sha256",
        "rendered-requests/index.json",
        "rendered_requests",
    ):
        value = digests.get(key)
        if isinstance(value, dict):
            value = value.get("sha256")
        if _is_sha256(value):
            return value
    return None


def _policy_control_digests(policy: dict[str, Any]) -> dict[str, str]:
    digests = _policy_digests(policy)
    raw = digests.get("controls", policy.get("control_digests"))
    if isinstance(raw, list):
        raw = {
            item.get("case_id", item.get("case")): item.get("sha256")
            for item in raw
            if isinstance(item, dict)
        }
    if not isinstance(raw, dict):
        raise TrialInputError("frozen policy must pin all five control files")
    result: dict[str, str] = {}
    for case_id in _CASE_ORDER:
        value = raw.get(case_id, raw.get(f"{case_id}.json", raw.get(f"controls/{case_id}.json")))
        if isinstance(value, dict):
            value = value.get("sha256")
        if not _is_sha256(value):
            raise TrialInputError(f"frozen policy does not pin controls for {case_id}")
        result[case_id] = value
    return result


def _verify_frozen_controls(
    run_dir: Path,
    cases: Sequence[TrialCaseInputs],
    policy: dict[str, Any],
) -> None:
    expected = _policy_control_digests(policy)
    for case in cases:
        path = run_dir / "controls" / f"{case.case_id}.json"
        try:
            actual = _sha256(path.read_bytes())
        except OSError as exc:
            raise TrialInputError(f"frozen controls are unavailable for {case.case_id}") from exc
        if actual != expected[case.case_id] or actual != case.controls_sha256:
            raise TrialInputError(f"frozen controls sha256 mismatch for {case.case_id}")


def _verify_frozen_renderings(
    run_dir: Path,
    cases: Sequence[TrialCaseInputs],
    policy: dict[str, Any],
) -> None:
    render_dir = run_dir / "rendered-requests"
    index_path = render_dir / "index.json"
    try:
        index_bytes = index_path.read_bytes()
        index = json.loads(index_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialInputError("frozen rendered requests are missing or invalid") from exc
    expected_index_sha = _policy_rendered_index_digest(policy)
    if expected_index_sha is None or expected_index_sha != _sha256(index_bytes):
        raise TrialInputError("rendered-request index does not match frozen-policy digest")
    if not isinstance(index, dict) or index.get("schema_version") != (
        "fresh-five-case-rendered-requests-v1"
    ):
        raise TrialInputError("unknown frozen rendered-request index schema")
    entries = index.get("cases")
    if not isinstance(entries, dict) or set(entries) != set(_CASE_ORDER):
        raise TrialInputError("frozen rendered-request index must contain all five cases")
    for case in cases:
        packet = build_call1_packet_v2(
            case.input_view,
            case.inventory,
            case.runtime_contract,
        )
        entry = entries.get(case.case_id)
        if not isinstance(entry, dict):
            raise TrialInputError(f"frozen Call 1 rendering is missing for {case.case_id}")
        if entry.get("prompt_version") != packet.version or entry.get("prompt_sha256") != (
            packet.sha256
        ):
            raise TrialInputError(f"{case.case_id} Call 1 rendering differs from frozen bytes")
        for role, content in (("system", packet.system.encode()), ("user", packet.user.encode())):
            file_name = entry.get(f"{role}_file")
            if not isinstance(file_name, str) or Path(file_name).name != file_name:
                raise TrialInputError(f"{case.case_id} has an invalid frozen {role} path")
            try:
                saved = (render_dir / file_name).read_bytes()
            except OSError as exc:
                raise TrialInputError(
                    f"{case.case_id} frozen {role} bytes are unavailable"
                ) from exc
            if saved != content or entry.get(f"{role}_sha256") != _sha256(content):
                raise TrialInputError(f"{case.case_id} frozen {role} bytes differ")


def _validate_loaded_case_order(cases: Sequence[TrialCaseInputs]) -> None:
    if tuple(case.case_id for case in cases) != _CASE_ORDER:
        raise TrialInputError("trial inputs must follow the fixed five-case order")


def _load_batch_status(path: Path, run_dir: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": _BATCH_SCHEMA_VERSION,
            "run_dir": str(run_dir),
            "cases": {},
        }
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrialInputError(f"invalid batch status: {path}") from exc
    if not isinstance(status, dict) or status.get("schema_version") != _BATCH_SCHEMA_VERSION:
        raise TrialInputError(f"unknown batch status schema: {path}")
    if status.get("run_dir") != str(run_dir):
        raise TrialInputError("batch status belongs to a different run directory")
    if not isinstance(status.get("cases"), dict):
        raise TrialInputError("batch status cases must be an object")
    return status


def _case_has_evidence(
    run_dir: Path,
    raw_root: Path,
    case_id: str,
    budget: PersistedAuthoringBudget | None,
) -> bool:
    if budget is not None and budget.has_reservation(case_id):
        return True
    case_raw_dir = raw_root / case_id
    if case_raw_dir.exists() and any(case_raw_dir.iterdir()):
        return True
    receipt = run_dir / "authoring" / case_id / "case-receipt.json"
    if receipt.is_file():
        try:
            saved = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return True
        return isinstance(saved, dict) and saved.get("status") not in {
            "pending",
            "unattempted",
        }
    return False


def _has_prior_evidence(run_dir: Path, raw_root: Path) -> bool:
    if any(
        (raw_root / case_id).exists() and any((raw_root / case_id).iterdir())
        for case_id in _CASE_ORDER
    ):
        return True
    for case_id in _CASE_ORDER:
        receipt = run_dir / "authoring" / case_id / "case-receipt.json"
        if not receipt.is_file():
            continue
        try:
            saved = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return True
        if isinstance(saved, dict) and saved.get("status") not in {
            "pending",
            "unattempted",
        }:
            return True
    status_path = run_dir / "authoring" / "batch-status.json"
    if status_path.is_file():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return True
        if isinstance(status, dict) and status.get("outage_stopped"):
            return True
    return False


def _budget_stop_for_case(budget: PersistedAuthoringBudget, case_id: str) -> str | None:
    if budget.total_dispatched >= budget.aggregate_limit:
        return "aggregate authoring budget exhausted"
    if budget.dispatched_by_task.get(case_id, 0) >= budget.task_limit:
        return f"per-case authoring budget exhausted: {case_id}"
    roles = budget.dispatched_by_task_role.get(case_id, {})
    if roles.get("author", 0) >= budget.author_limit:
        return f"per-case author/correction budget exhausted: {case_id}"
    return None


def _case_receipt(
    case_id: str,
    result: Any,
    *,
    budget: PersistedAuthoringBudget,
    package_dir: Path,
    raw_evidence_dir: Path,
    timings_path: Path,
    unresolved_controls: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    ledger = list(getattr(result, "ledger", []))
    findings = [
        finding.to_dict()
        for finding in getattr(result, "findings", [])
        if hasattr(finding, "to_dict")
    ]
    role_counts = {"author": 0, "reviewer": 0}
    tokens = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    controls: dict[str, list[dict[str, Any]]] = {"normal": [], "supplied": []}
    for record in ledger:
        role = record.get("role")
        if role in role_counts:
            role_counts[role] += 1
        usage = record.get("usage")
        if isinstance(usage, dict):
            for key in tokens:
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    tokens[key] += value
        raw_controls = record.get("detector_controls")
        if isinstance(raw_controls, list):
            for control in raw_controls:
                if not isinstance(control, dict):
                    continue
                origin = control.get("origin", "normal")
                controls.setdefault(origin, []).append(copy.deepcopy(control))
    prompt_records = {
        stage: {"version": packet.version, "sha256": packet.sha256}
        for stage, packet in getattr(result, "prompts", {}).items()
    }
    latency_records = _read_timing_records(timings_path, case_id)
    package = getattr(result, "package", None)
    manifest = getattr(package, "manifest", None)
    package_path = getattr(result, "package_path", None)
    prompt_overflow = next(
        (
            copy.deepcopy(finding.get("details"))
            for finding in findings
            if finding.get("code") == "prompt_overflow"
            and isinstance(finding.get("details"), dict)
        ),
        None,
    )
    terminal_stage = (
        findings[-1].get("path") if findings else (ledger[-1].get("stage") if ledger else None)
    )
    failure_path = getattr(result, "failure_evidence_path", None)
    return {
        "schema_version": _CASE_RECEIPT_SCHEMA_VERSION,
        "case_id": case_id,
        "status": getattr(result, "status", "unknown"),
        "terminal_stage": terminal_stage,
        "reason": findings[0].get("code") if findings else getattr(result, "status", None),
        "findings_summary": findings,
        "prompt_versions_and_digests": prompt_records,
        "role_counts": role_counts,
        "tokens": tokens,
        "per_call_latency_ms": [
            {
                "stage": item.get("stage"),
                "elapsed_ms": item.get("elapsed_ms"),
                "outcome": item.get("outcome"),
            }
            for item in latency_records
        ],
        "control_results_by_origin": controls,
        "unresolved_controls": [copy.deepcopy(item) for item in unresolved_controls],
        "budget": budget.snapshot(case_id),
        "package": (
            {
                "path": str(package_path or package_dir),
                "manifest_digest": getattr(manifest, "manifest_digest", None),
            }
            if package is not None
            else None
        ),
        "raw_evidence_dir": str(raw_evidence_dir),
        "failure_evidence_path": str(failure_path) if failure_path else None,
        "dispatch_count": len(ledger),
        **({"prompt_overflow": prompt_overflow} if prompt_overflow is not None else {}),
    }


def _read_timing_records(path: Path, task_id: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("task_id") == task_id:
            records.append(value)
    return records


def _is_transport_outage(result: Any) -> bool:
    if getattr(result, "status", None) in {"transport_failure", "correction_dispatch_failed"}:
        return True
    if any(
        getattr(finding, "code", None) in {"transport_failure", "correction_dispatch_failed"}
        for finding in getattr(result, "findings", [])
    ):
        return True
    if getattr(result, "status", None) != "review_unavailable":
        return False
    for record in getattr(result, "ledger", []):
        failure = record.get("failure")
        if (
            isinstance(failure, dict)
            and failure.get("phase") == "invocation"
            and record.get("stage") in {"plan_review", "artifact_review"}
        ):
            return True
    return False


def _write_case_receipt(path: Path, receipt: dict[str, Any]) -> None:
    _write_json_atomic(path, receipt)


def _write_unattempted_receipt(run_dir: Path, case_id: str, reason: str) -> None:
    path = run_dir / "authoring" / case_id / "case-receipt.json"
    if path.exists():
        return
    _write_case_receipt(
        path,
        {
            "schema_version": _CASE_RECEIPT_SCHEMA_VERSION,
            "case_id": case_id,
            "status": "unattempted",
            "terminal_stage": None,
            "reason": reason,
            "package": None,
            "dispatch_count": 0,
        },
    )


def _write_already_attempted_receipt(
    run_dir: Path,
    raw_root: Path,
    case_id: str,
    budget: PersistedAuthoringBudget,
) -> None:
    path = run_dir / "authoring" / case_id / "case-receipt.json"
    if path.exists():
        return
    raw_case_dir = raw_root / case_id
    _write_case_receipt(
        path,
        {
            "schema_version": _CASE_RECEIPT_SCHEMA_VERSION,
            "case_id": case_id,
            "status": "already_attempted",
            "terminal_stage": None,
            "reason": "existing reservation, package, or failure evidence",
            "dispatch_count": budget.dispatched_by_task.get(case_id, 0),
            "budget": budget.snapshot(case_id),
            "package": None,
            "raw_evidence_dir": str(raw_case_dir),
        },
    )


def _set_case_status(
    batch_status: dict[str, Any],
    case_id: str,
    case_status: str,
    *,
    reason: str | None = None,
) -> None:
    record = {"status": case_status}
    if reason is not None:
        record["reason"] = reason
    batch_status.setdefault("cases", {})[case_id] = record


def _default_raw_evidence_root(run_dir: Path) -> Path:
    prefix = "fresh-consumer-five-case-"
    trial_id = run_dir.name.removeprefix(prefix)
    if trial_id == run_dir.name or not trial_id:
        raise TrialInputError("run directory must be named fresh-consumer-five-case-<timestamp>")
    return CONSUMER_ROOT / "runs" / "authoring" / f"fresh-five-case-{trial_id}"


def _safe_exception_text(exc: BaseException) -> str:
    text = str(exc)
    return text.replace("\n", " ")[:500]
