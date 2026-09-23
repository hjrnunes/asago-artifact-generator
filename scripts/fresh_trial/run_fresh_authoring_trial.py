"""Render or run the frozen five-case consumer authoring trial."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring import (
    AUTHORING_CONTEXT_WINDOW_TOKENS,
    AUTHORING_MAX_COMPLETION_TOKENS,
    AUTHORING_THINKING_EXTRA_BODY,
    AuthoringOrchestrator,
    AuthoringPolicy,
    PrivateModelAuthoringTransport,
    build_call1_packet_v2,
)
from asago_artifact_generator.detector_controls import ControlCase
from asago_artifact_generator.profiles import load_authoring_profile

from ._storage import _utc_now, _write_bytes_atomic, _write_json_atomic
from .accounting import PersistedAuthoringBudget, TimedAuthoringTransport
from .controls import ControlSuite, load_control_cases, load_control_suite, remap_control_cases
from .errors import TrialInputError
from .inputs import (
    CASE_ORDER,
    TrialCaseInputs,
    _sha256,
    load_trial_case_inputs,
)
from .receipts import (
    _BATCH_SCHEMA_VERSION,
    _CASE_RECEIPT_SCHEMA_VERSION,
    _budget_stop_for_case,
    _case_has_evidence,
    _case_receipt,
    _default_raw_evidence_root,
    _has_prior_evidence,
    _is_transport_outage,
    _load_batch_status,
    _read_frozen_policy,
    _safe_exception_text,
    _set_case_status,
    _validate_loaded_case_order,
    _verify_frozen_controls,
    _verify_frozen_renderings,
    _write_already_attempted_receipt,
    _write_case_receipt,
    _write_unattempted_receipt,
)

CONSUMER_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = CONSUMER_ROOT.parents[2]
PROFILES = PROJECT_ROOT / "config" / "model-profiles.yaml"


def render_call1_requests(
    run_dir: str | Path,
    *,
    cases: Sequence[TrialCaseInputs],
    transport_factory: Callable[[], Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Render and save all first requests without constructing a transport."""

    del transport_factory
    root = Path(run_dir)
    output_dir = root / "rendered-requests"
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: dict[str, dict[str, Any]] = {}
    if tuple(case.case_id for case in cases) != CASE_ORDER:
        raise TrialInputError("rendering requires the five cases in their fixed order")
    for case in cases:
        packet = build_call1_packet_v2(
            case.input_view,
            case.inventory,
            case.runtime_contract,
        )
        system = packet.system.encode("utf-8")
        user = packet.user.encode("utf-8")
        system_name = f"{case.case_id}.call1.system.txt"
        user_name = f"{case.case_id}.call1.user.txt"
        _write_bytes_atomic(output_dir / system_name, system)
        _write_bytes_atomic(output_dir / user_name, user)
        rendered[case.case_id] = {
            "prompt_version": packet.version,
            "prompt_sha256": packet.sha256,
            "system_sha256": _sha256(system),
            "system_bytes": len(system),
            "system_file": system_name,
            "user_sha256": _sha256(user),
            "user_bytes": len(user),
            "user_file": user_name,
        }
    _write_json_atomic(
        output_dir / "index.json",
        {
            "schema_version": "fresh-five-case-rendered-requests-v1",
            "cases": rendered,
        },
    )
    return rendered


def run_trial(
    run_dir: str | Path,
    *,
    mode: str,
    profiles_file: str | Path = PROFILES,
    transport_factory: Callable[[], Any] | None = None,
    input_loader: Callable[[str | Path], Sequence[TrialCaseInputs]] = load_trial_case_inputs,
    orchestrator_factory: Callable[..., AuthoringOrchestrator] = AuthoringOrchestrator,
    raw_evidence_root: str | Path | None = None,
) -> dict[str, Any]:
    """Render offline or dispatch the single explicitly authorized live batch."""

    root = Path(run_dir).resolve()
    if mode not in {"render-only", "live"}:
        raise ValueError("mode must be 'render-only' or 'live'")
    if not root.is_dir():
        raise TrialInputError(f"run directory does not exist: {root}")
    authoring_root = root / "authoring"
    authoring_root.mkdir(parents=True, exist_ok=True)

    frozen_policy: dict[str, Any] | None = None
    if mode == "live":
        frozen_policy = _read_frozen_policy(root)
    cases = tuple(input_loader(root))
    _validate_loaded_case_order(cases)

    if mode == "render-only":
        rendered = render_call1_requests(root, cases=cases)
        batch_status = {
            "schema_version": _BATCH_SCHEMA_VERSION,
            "run_dir": str(root),
            "status": "rendered_only",
            "live_dispatch_enabled": False,
            "transport_constructed": False,
            "cases": {case_id: {"status": "rendered"} for case_id in CASE_ORDER},
            "rendered_requests": rendered,
            "updated_at": _utc_now(),
        }
        _write_json_atomic(authoring_root / "batch-status.json", batch_status)
        return batch_status

    assert frozen_policy is not None
    _verify_frozen_controls(root, cases, frozen_policy)
    _verify_frozen_renderings(root, cases, frozen_policy)
    evidence_root = Path(raw_evidence_root or _default_raw_evidence_root(root))
    budget_path = authoring_root / "budget-ledger.json"
    if not budget_path.exists() and _has_prior_evidence(root, evidence_root):
        raise TrialInputError(
            "authoring budget ledger is missing while case evidence exists; refusing to reset"
        )
    budget = PersistedAuthoringBudget.load(budget_path)
    return run_authoring_batch(
        root,
        cases=cases,
        budget=budget,
        transport_factory=transport_factory
        or (lambda: build_private_model_transport(profiles_file)),
        orchestrator_factory=orchestrator_factory,
        raw_evidence_root=evidence_root,
    )


def run_authoring_batch(
    run_dir: str | Path,
    *,
    cases: Sequence[TrialCaseInputs],
    budget: PersistedAuthoringBudget,
    transport_factory: Callable[[], Any],
    orchestrator_factory: Callable[..., AuthoringOrchestrator] = AuthoringOrchestrator,
    raw_evidence_root: str | Path,
) -> dict[str, Any]:
    """Run the normal orchestrator once per eligible case with shared accounting."""

    root = Path(run_dir).resolve()
    ordered_cases = tuple(cases)
    _validate_loaded_case_order(ordered_cases)
    authoring_root = root / "authoring"
    authoring_root.mkdir(parents=True, exist_ok=True)
    status_path = authoring_root / "batch-status.json"
    status = _load_batch_status(status_path, root)
    if status.get("outage_stopped"):
        for case in ordered_cases:
            if _case_has_evidence(root, Path(raw_evidence_root), case.case_id, budget):
                _write_already_attempted_receipt(
                    root,
                    Path(raw_evidence_root),
                    case.case_id,
                    budget,
                )
                _set_case_status(status, case.case_id, "already_attempted")
            else:
                _write_unattempted_receipt(
                    root,
                    case.case_id,
                    "prior_transport_outage",
                )
                _set_case_status(status, case.case_id, "unattempted")
        status["status"] = "outage_stopped"
        status["updated_at"] = _utc_now()
        _write_json_atomic(status_path, status)
        return status

    status.setdefault("cases", {})
    status.update(
        {
            "schema_version": _BATCH_SCHEMA_VERSION,
            "run_dir": str(root),
            "case_order": list(CASE_ORDER),
            "status": "running",
            "live_dispatch_enabled": True,
            "outage_stopped": False,
            "started_at": status.get("started_at", _utc_now()),
            "updated_at": _utc_now(),
        }
    )
    _write_json_atomic(status_path, status)
    shared_transport: Any | None = None
    stopped_reason: str | None = None
    cases_by_id = {case.case_id: case for case in ordered_cases}

    for index, case_id in enumerate(CASE_ORDER):
        case = cases_by_id[case_id]
        case_raw_dir = Path(raw_evidence_root) / case_id
        receipt_path = authoring_root / case_id / "case-receipt.json"
        if _case_has_evidence(root, Path(raw_evidence_root), case_id, budget):
            _write_already_attempted_receipt(
                root,
                Path(raw_evidence_root),
                case_id,
                budget,
            )
            _set_case_status(status, case_id, "already_attempted")
            status["updated_at"] = _utc_now()
            _write_json_atomic(status_path, status)
            continue

        exhaustion = _budget_stop_for_case(budget, case_id)
        if exhaustion is not None:
            _write_case_receipt(
                receipt_path,
                {
                    "schema_version": _CASE_RECEIPT_SCHEMA_VERSION,
                    "case_id": case_id,
                    "status": "budget_exhausted",
                    "terminal_stage": None,
                    "reason": exhaustion,
                    "dispatch_counts": {
                        "aggregate": budget.total_dispatched,
                        "task": budget.dispatched_by_task.get(case_id, 0),
                    },
                    "package": None,
                    "raw_evidence_dir": str(case_raw_dir),
                },
            )
            _set_case_status(status, case_id, "budget_exhausted", reason=exhaustion)
            if budget.total_dispatched >= budget.aggregate_limit:
                stopped_reason = "aggregate_budget_exhausted"
                for later_id in CASE_ORDER[index + 1 :]:
                    _write_unattempted_receipt(root, later_id, stopped_reason)
                    _set_case_status(status, later_id, "unattempted", reason=stopped_reason)
                break
            status["updated_at"] = _utc_now()
            _write_json_atomic(status_path, status)
            continue

        status["active_case"] = case_id
        status["updated_at"] = _utc_now()
        _write_json_atomic(status_path, status)
        try:
            if shared_transport is None:
                shared_transport = transport_factory()
            timed_transport = TimedAuthoringTransport(
                shared_transport,
                timings_path=authoring_root / "call-timings.jsonl",
                budget=budget,
                task_id=case_id,
            )
            package_dir = case_raw_dir / "package"

            def supplied_controls(
                plan: Mapping[str, Any],
                metadata: Mapping[str, Any],
                records: tuple[dict[str, Any], ...] = case.control_records,
            ) -> tuple[ControlCase, ...]:
                return remap_control_cases(records, plan, metadata)

            orchestrator = orchestrator_factory(
                transport=timed_transport,
                package_dir=package_dir,
                task_id=case_id,
                wire_version="v2",
                policy=AuthoringPolicy(
                    plan_max_corrections=1,
                    artifact_max_corrections=1,
                    review_plan=True,
                    review_artifact=True,
                ),
                budget=budget,
                supplied_control_cases=supplied_controls,
            )
            result = orchestrator.run(
                case.input_view,
                case.inventory,
                case.runtime_contract,
            )
        except Exception as exc:
            failure_code = (
                "transport_construction_failed" if budget.total_dispatched == 0 else "caller_error"
            )
            receipt = {
                "schema_version": _CASE_RECEIPT_SCHEMA_VERSION,
                "case_id": case_id,
                "status": failure_code,
                "terminal_stage": None,
                "reason": _safe_exception_text(exc),
                "dispatch_counts": {
                    "aggregate": budget.total_dispatched,
                    "task": budget.dispatched_by_task.get(case_id, 0),
                },
                "package": None,
                "raw_evidence_dir": str(case_raw_dir),
            }
            _write_case_receipt(receipt_path, receipt)
            _set_case_status(status, case_id, failure_code, reason=receipt["reason"])
            stopped_reason = failure_code
            for later_id in CASE_ORDER[index + 1 :]:
                _write_unattempted_receipt(root, later_id, stopped_reason)
                _set_case_status(status, later_id, "unattempted", reason=stopped_reason)
            break

        _write_json_atomic(
            case_raw_dir / "authoring" / "ledger.json",
            {
                "schema_version": "fresh-five-case-authoring-ledger-v1",
                "task_id": case_id,
                "ledger": list(getattr(result, "ledger", [])),
            },
        )
        receipt = _case_receipt(
            case_id,
            result,
            budget=budget,
            package_dir=package_dir,
            raw_evidence_dir=case_raw_dir,
            timings_path=authoring_root / "call-timings.jsonl",
            unresolved_controls=case.unresolved_controls,
        )
        _write_case_receipt(receipt_path, receipt)
        _set_case_status(status, case_id, result.status, reason=receipt.get("reason"))
        if result.status == "prompt_overflow" and isinstance(receipt.get("prompt_overflow"), dict):
            status["cases"][case_id]["prompt_overflow"] = copy.deepcopy(receipt["prompt_overflow"])
        if _is_transport_outage(result):
            status["outage_stopped"] = True
            stopped_reason = "transport_outage"
            for later_id in CASE_ORDER[index + 1 :]:
                _write_unattempted_receipt(root, later_id, stopped_reason)
                _set_case_status(status, later_id, "unattempted", reason=stopped_reason)
            break
        if budget.total_dispatched >= budget.aggregate_limit:
            stopped_reason = "aggregate_budget_exhausted"
            for later_id in CASE_ORDER[index + 1 :]:
                _write_unattempted_receipt(root, later_id, stopped_reason)
                _set_case_status(status, later_id, "unattempted", reason=stopped_reason)
            break
        status.pop("active_case", None)
        status["updated_at"] = _utc_now()
        _write_json_atomic(status_path, status)

    status.pop("active_case", None)
    status["stop_reason"] = stopped_reason
    if status.get("outage_stopped"):
        status["status"] = "outage_stopped"
    elif stopped_reason == "aggregate_budget_exhausted":
        status["status"] = "budget_stopped"
    elif stopped_reason is not None:
        status["status"] = "stopped"
    elif all(
        status.get("cases", {}).get(case_id, {}).get("status") != "unattempted"
        for case_id in CASE_ORDER
    ):
        status["status"] = "completed"
    else:
        status["status"] = "partial"
    status["aggregate_dispatched"] = budget.total_dispatched
    status["updated_at"] = _utc_now()
    _write_json_atomic(status_path, status)
    return status


def build_private_model_transport(
    profiles_file: str | Path = PROFILES,
) -> PrivateModelAuthoringTransport:
    """Build the explicitly pinned private transport without exposing credentials."""

    profile = load_authoring_profile(profiles_file, "gemma4-oc")
    return PrivateModelAuthoringTransport(
        base_url=profile.base_url,
        api_key=profile.api_key,
        model=profile.model,
        profile_name=profile.name,
        temperature=0.0,
        extra_body=copy.deepcopy(AUTHORING_THINKING_EXTRA_BODY),
        context_window_tokens=AUTHORING_CONTEXT_WINDOW_TOKENS,
        max_completion_tokens=AUTHORING_MAX_COMPLETION_TOKENS,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the explicit render-only/live command line."""

    parser = argparse.ArgumentParser(
        description="Render or run the frozen five-case consumer authoring trial."
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--profiles-file", type=Path, default=PROFILES)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--render-only",
        dest="mode",
        action="store_const",
        const="render-only",
        help="Write the five exact Call 1 requests; do not construct a transport.",
    )
    mode.add_argument(
        "--live",
        dest="mode",
        action="store_const",
        const="live",
        help="Dispatch the five-case batch against the frozen inputs.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and print only batch metadata."""

    args = build_argument_parser().parse_args(argv)
    try:
        status = run_trial(
            args.run_dir,
            mode=args.mode,
            profiles_file=args.profiles_file,
        )
    except (TrialInputError, ValueError, OSError) as exc:
        print(f"fresh authoring trial stopped: {_safe_exception_text(exc)}", file=sys.stderr)
        return 1
    print(json.dumps(status, sort_keys=True, indent=2))
    return 0


__all__ = [
    "CASE_ORDER",
    "ControlSuite",
    "PersistedAuthoringBudget",
    "TimedAuthoringTransport",
    "TrialCaseInputs",
    "TrialInputError",
    "build_argument_parser",
    "load_control_cases",
    "load_control_suite",
    "load_trial_case_inputs",
    "main",
    "remap_control_cases",
    "render_call1_requests",
    "run_authoring_batch",
    "run_trial",
]


if __name__ == "__main__":
    raise SystemExit(main())
