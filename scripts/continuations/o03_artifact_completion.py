#!/usr/bin/env python3
"""Offline O03 artifact-correction readiness and control replay.

The dispatch flags are intentionally sealed stubs.  This module performs no
provider request; live completion owns those modes after this offline gate.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring import (
    AUTHORING_CONTEXT_WINDOW_TOKENS,
    AUTHORING_MAX_COMPLETION_TOKENS,
    AuthoringBudget,
    _enforce_context_budget,
    _mapping_sha256,
    _render_correction_packet,
    build_artifact_author_context,
    build_correction_context,
    collect_artifact_findings_v2,
    parse_call2_response,
    parse_historical_call1_response,
)
from asago_artifact_generator.detector_controls import (
    ControlCase,
    build_detector_feedback,
    run_detector_controls,
)
from asago_artifact_generator.failure_evidence import raw_response_record
from asago_artifact_generator.qualification_inputs import prepare_o03_authoring_inputs

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = REPOSITORY_ROOT / "runs" / "authoring"
FIXTURE_PATH = Path(__file__).with_name("o03_control_fixtures.json")

FROZEN_EVIDENCE = {
    "accepted_plan": (
        RUNS_ROOT / "O03-live-20260922T175415Z-exact-plan-correction.failure-evidence.json",
        "a419cb2b3e5371bba985a7ac1f8d47cf9a09e2edec45ed9960c3e94e8b68317b",
    ),
    "overflow_continuation": (
        RUNS_ROOT / "O03-live-20260922T180032Z-artifact-continuation.failure-evidence.json",
        "fcc1e4f03afc16eff0e7cef09160566d79dd7710bc85c86806bfd8c61d140f0c",
    ),
    "saved_candidate": (
        RUNS_ROOT / "O03-live-20260922T181427Z-artifact-correction.failure-evidence.json",
        "b1a079f9a82ef3f0d29136b20a269a1229b174b11e7ad3c31ef4b764125ce3f0",
    ),
}
ACCEPTED_PLAN_SHA256 = "0d369af055bf65ddaddc78e875279c8108356da2fd0846d68073a5d3b6426237"
SAVED_CANDIDATE_SHA256 = "4c9411e800e76231f919c428596b302f7dcbd86f13c2d887fe005006a26ab8ec"
HISTORICAL_TASK_ID = "O03-live-20260922T181427Z-artifact-correction"
HISTORICAL_SNAPSHOT = {
    "author_correction_spent": 8,
    "author_correction_limit": 8,
    "review_spent": 4,
    "review_limit": 6,
    "task_spent": 12,
    "task_limit": 14,
    "aggregate_spent": 19,
    "aggregate_limit": 32,
}
RECONCILIATION_CUTOFF = datetime(2026, 9, 22, 18, 14, 27, tzinfo=UTC)
AUTHOR_INCREMENT = 1
REVIEW_INCREMENT = 1
EXPECTED_CONTROL_NAMES = (
    "matching-commit-backend-rejected",
    "matching-commit-backend-completed",
    "different-draft-complete-capture",
    "different-operation-complete-capture",
    "complete-captured-empty",
    "missing-unavailable-capture",
    "partial-capture-without-witness",
    "matching-witness-with-partial-capture",
    "malformed-missing-arguments",
    "malformed-null-arguments",
    "malformed-non-object-arguments",
    "malformed-missing-draft-id",
    "malformed-null-draft-id",
    "missing-id-binding",
    "missing-prerequisite-status",
    "wrong-prerequisite-status",
    "valid-witness-with-unrelated-malformed-call",
    "permuted-order-second-synthetic-draft",
)


class DispatchSlotSpent(RuntimeError):
    """Raised before a second correction or review dispatch can be attempted."""


class DispatchModeStub(RuntimeError):
    """Raised because live dispatch belongs to the later completion feature."""


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_frozen_evidence() -> dict[str, Any]:
    records: dict[str, Any] = {}
    for name, (path, expected) in FROZEN_EVIDENCE.items():
        raw = path.read_bytes()
        actual = _sha256(raw)
        if actual != expected:
            raise ValueError(f"frozen {name} hash differs: {path}")
        records[name] = {
            "path": str(path),
            "sha256": actual,
            "byte_length": len(raw),
            "expected_sha256": expected,
        }
    return records


def _available_raw_response(attempt: dict[str, Any], label: str) -> bytes:
    record = attempt.get("raw_response")
    if not isinstance(record, dict) or record.get("availability") != "available":
        raise ValueError(f"{label} raw response is unavailable")
    try:
        raw = base64.b64decode(record["base64"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} raw response is invalid") from exc
    if record.get("sha256") != _sha256(raw) or record.get("byte_length") != len(raw):
        raise ValueError(f"{label} raw response hash differs")
    return raw


def _extract_authorities() -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    plan_record = _read_json(FROZEN_EVIDENCE["accepted_plan"][0])
    candidate_record = _read_json(FROZEN_EVIDENCE["saved_candidate"][0])
    attempts = plan_record.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        raise ValueError("accepted-plan evidence has no correction attempt")
    plan_attempt = attempts[0]
    plan_raw = _available_raw_response(plan_attempt, "accepted plan")
    plan, _ = parse_historical_call1_response(plan_raw)
    if not isinstance(plan, dict) or plan != plan_attempt.get("decoded_output"):
        raise ValueError("accepted plan decoded output differs from its raw response")
    plan_digest = _mapping_sha256(plan)
    if plan_digest != ACCEPTED_PLAN_SHA256:
        raise ValueError("accepted plan digest differs")

    candidate_attempts = candidate_record.get("attempts")
    if not isinstance(candidate_attempts, list) or not candidate_attempts:
        raise ValueError("saved candidate evidence has no attempt")
    candidate_raw = _available_raw_response(candidate_attempts[0], "saved candidate")
    candidate = parse_call2_response(candidate_raw)
    candidate_digest = _sha256(
        json.dumps(
            candidate.metadata,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\0"
        + candidate.python_bytes
    )
    if candidate_digest != SAVED_CANDIDATE_SHA256:
        raise ValueError("saved candidate digest differs")
    return (
        plan,
        candidate_raw,
        {
            "plan_raw_sha256": _sha256(plan_raw),
            "plan_sha256": plan_digest,
            "candidate_raw_sha256": _sha256(candidate_raw),
            "candidate_sha256": candidate_digest,
            "candidate_metadata_sha256": _mapping_sha256(candidate.metadata),
            "candidate_python_sha256": _sha256(candidate.python_bytes),
            "candidate_python_byte_length": len(candidate.python_bytes),
            "candidate": candidate,
            "historical_attempt": candidate_attempts[0],
        },
    )


def load_control_cases(path: str | Path = FIXTURE_PATH) -> tuple[ControlCase, ...]:
    """Load frozen setup-bound controls without scenario-specific selection logic."""

    fixture = _read_json(Path(path))
    if fixture.get("schema_version") != "o03-control-fixtures-v1":
        raise ValueError("control fixture schema differs")
    if fixture.get("judge_controls") != [] or fixture.get("numeric_equal_bound_controls") != []:
        raise ValueError("judge and numeric equal-bound controls are not permitted")
    rows = fixture.get("rows")
    if (
        not isinstance(rows, list)
        or tuple(row.get("name") for row in rows) != EXPECTED_CONTROL_NAMES
    ):
        raise ValueError("control fixture rows differ from the frozen §D table")
    cases: list[ControlCase] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("control fixture row must be an object")
        evidence = row.get("evidence")
        if not isinstance(evidence, dict):
            raise ValueError(f"control fixture evidence is invalid: {row.get('name')}")
        cases.append(
            ControlCase(
                name=row["name"],
                evidence=deepcopy(evidence),
                expected_outcome=row["expected_outcome"],
                expected_claim_level=row.get("expected_claim_level"),
            )
        )
    return tuple(cases)


def ensure_dispatch_slot_available(ledger: Any, slot: str) -> None:
    """Refuse a dispatch when its slot already appears in the evidence ledger."""

    if slot not in {"correction", "review"}:
        raise ValueError(f"unsupported dispatch slot: {slot}")
    records = ledger.get("ledger", []) if isinstance(ledger, dict) else ledger
    if not isinstance(records, list):
        raise ValueError("evidence ledger must be a list")
    for record in records:
        if not isinstance(record, dict):
            continue
        explicit = record.get("dispatch_slot") or record.get("slot")
        stage = record.get("stage")
        role = record.get("role")
        spent = (
            explicit == slot
            or (slot == "correction" and stage == "correction" and role == "author")
            or (slot == "review" and stage in {"artifact_review", "review"} and role == "reviewer")
        )
        if spent:
            raise DispatchSlotSpent(f"{slot} dispatch slot is already recorded as spent")


def dispatch_correction(ledger: Any) -> None:
    """Guard and stub the future single correction dispatch."""

    ensure_dispatch_slot_available(ledger, "correction")
    raise DispatchModeStub("correction dispatch is sealed until live-completion")


def dispatch_review(ledger: Any) -> None:
    """Guard and stub the future conditional review dispatch."""

    ensure_dispatch_slot_available(ledger, "review")
    raise DispatchModeStub("review dispatch is sealed until live-completion")


def _timestamp_from_path(path: Path) -> datetime | None:
    match = re.search(r"live-(\d{8}T\d{6}Z)", path.name)
    if match is None:
        return None
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def reconcile_budget() -> dict[str, Any]:
    """Reconcile shared counters without reopening historical task allowance."""

    dispatches: list[dict[str, Any]] = []
    for path in sorted(RUNS_ROOT.rglob("*.failure-evidence.json")):
        timestamp = _timestamp_from_path(path)
        if timestamp is None or timestamp <= RECONCILIATION_CUTOFF:
            continue
        try:
            record = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        budget = record.get("budget")
        if not isinstance(budget, dict) or budget.get("aggregate_limit") != 32:
            continue
        for attempt in record.get("attempts", []):
            if not isinstance(attempt, dict):
                continue
            if attempt.get("stage") not in {"correction", "artifact_review", "review"}:
                continue
            dispatches.append(
                {
                    "path": str(path),
                    "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                    "task_id": record.get("task_id"),
                    "dispatch_index": attempt.get("dispatch_index"),
                    "stage": attempt.get("stage"),
                    "role": attempt.get("role"),
                    "candidate_sha256": attempt.get("candidate_sha256"),
                    "reviewed_candidate_sha256": attempt.get("reviewed_candidate_sha256"),
                    "budget_after_dispatch": {
                        key: budget.get(key)
                        for key in (
                            "aggregate_spent",
                            "aggregate_limit",
                            "author_correction_spent",
                            "review_spent",
                        )
                    },
                }
            )
    dispatches.sort(key=lambda item: (item["timestamp"], item["path"]))
    if len(dispatches) != 2:
        raise ValueError(
            f"expected two intervening shared-counter dispatches, found {len(dispatches)}"
        )
    intervening_author = sum(item["role"] == "author" for item in dispatches)
    intervening_review = sum(item["role"] == "reviewer" for item in dispatches)

    budget = AuthoringBudget.from_prior_spend(
        task_id=HISTORICAL_TASK_ID,
        prior_author_correction_spend=HISTORICAL_SNAPSHOT["author_correction_spent"],
        prior_review_spend=HISTORICAL_SNAPSHOT["review_spent"],
        aggregate_limit=HISTORICAL_SNAPSHOT["aggregate_limit"],
        task_limit=HISTORICAL_SNAPSHOT["task_limit"],
        author_limit=HISTORICAL_SNAPSHOT["author_correction_spent"] + intervening_author,
        review_limit=HISTORICAL_SNAPSHOT["review_spent"] + intervening_review,
        author_limit_increment=AUTHOR_INCREMENT,
        review_limit_increment=REVIEW_INCREMENT,
    )
    budget.total_dispatched = HISTORICAL_SNAPSHOT["aggregate_spent"] + len(dispatches)
    budget.dispatched_by_task[HISTORICAL_TASK_ID] = HISTORICAL_SNAPSHOT["task_spent"]
    budget.dispatched_by_task_role[HISTORICAL_TASK_ID] = {
        "author": HISTORICAL_SNAPSHOT["author_correction_spent"] + intervening_author,
        "reviewer": HISTORICAL_SNAPSHOT["review_spent"] + intervening_review,
    }
    snapshot = budget.snapshot(HISTORICAL_TASK_ID)
    if snapshot["author_correction_remaining"] != 1 or snapshot["review_remaining"] != 1:
        raise ValueError("budget increments do not leave one correction and one review slot")
    return {
        "schema_version": "authoring-budget-reconciliation-v1",
        "cutoff": RECONCILIATION_CUTOFF.isoformat().replace("+00:00", "Z"),
        "historical_snapshot": HISTORICAL_SNAPSHOT,
        "intervening_dispatches": dispatches,
        "authorization": {
            "author_correction_increment": AUTHOR_INCREMENT,
            "review_increment": REVIEW_INCREMENT,
            "aggregate_counter_continues": True,
            "task_counter_continues": True,
            "counter_reset": False,
            "task_renamed": False,
            "borrowed_slots": False,
        },
        "generic_budget_configuration": {
            "class": "AuthoringBudget",
            "seeded_via": "from_prior_spend",
            "author_limit_increment_argument": "author_limit_increment",
            "review_limit_increment_argument": "review_limit_increment",
            "continuation_budget_cloned": False,
        },
        "resulting_budget": snapshot,
    }


def _section_sizes(user: str) -> list[dict[str, Any]]:
    labels = [
        "FAILED STAGE",
        "ORIGINAL STAGE CONTEXT",
        "PLAN FIELD MEANINGS",
        "NEUTRAL OUTCOME EXAMPLE",
        "RUNTIME EVIDENCE INTERFACE",
        "RESPONSE CONTRACT",
        "CURRENT OUTPUT",
        "CURRENT FINDINGS",
        "DETECTOR CONTROL FEEDBACK",
        "AUTHORITY",
        "CORRECTION INSTRUCTIONS",
        "PRIOR UNRESOLVED FINDINGS",
    ]
    result: list[dict[str, Any]] = []
    for index, label in enumerate(labels):
        marker = label + "\n"
        if marker not in user:
            continue
        start = user.index(marker) + len(marker)
        end = len(user)
        for next_label in labels[index + 1 :]:
            next_marker = next_label + "\n"
            if next_marker in user[start:]:
                end = start + user[start:].index(next_marker)
                break
        result.append({"name": label, "byte_length": len(user[start:end].encode("utf-8"))})
    return result


def _historical_judge_failures(attempt: dict[str, Any]) -> dict[str, Any]:
    records = attempt.get("detector_controls", [])
    failures = [
        record
        for record in records
        if isinstance(record, dict)
        and "judge" in str(record.get("name", "")).casefold()
        and record.get("status") != "passed"
    ]
    return {
        "status": "preserved_as_historical",
        "applicable": False,
        "explanation": (
            "These failures came from judge metadata that conflicts with the accepted "
            "no-judge plan. They remain historical evidence and are not rerun as "
            "requirements for the repaired detector."
        ),
        "failures": failures,
    }


def run_dry_run(output_dir: str | Path | None = None) -> Path:
    """Run the complete offline readiness gate and write a new evidence directory."""

    output = (
        Path(output_dir)
        if output_dir is not None
        else RUNS_ROOT
        / f"O03-live-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-artifact-completion"
    )
    if output.exists():
        raise FileExistsError(f"dry-run output already exists: {output}")
    output.mkdir(parents=True)

    hashes = _verify_frozen_evidence()
    plan, candidate_raw, candidate_info = _extract_authorities()
    candidate = candidate_info.pop("candidate")
    prepared = prepare_o03_authoring_inputs()
    cases = load_control_cases()
    fixture_raw = FIXTURE_PATH.read_bytes()
    fixture_sha256 = _sha256(fixture_raw)

    control_findings, control_records = run_detector_controls(
        candidate.python_bytes,
        cases=cases,
    )
    feedback = build_detector_feedback(cases, control_records)
    deterministic_findings = collect_artifact_findings_v2(
        candidate,
        plan,
        prepared.inventory,
        prepared.runtime_contract,
    )
    findings = [item.to_dict() for item in deterministic_findings]
    findings.extend(control_findings)
    if not any(item.get("path") == "semantic_judge_spec" for item in findings):
        raise ValueError("saved candidate semantic-judge conflict was not recorded")

    original_context = build_artifact_author_context(
        prepared.input_view,
        plan,
        prepared.inventory,
        prepared.runtime_contract,
    )
    correction_context = build_correction_context(
        failed_stage="call2",
        original_context=original_context,
        current_output=candidate_raw,
        findings=findings,
        detector_feedback=feedback,
    )
    packet = _render_correction_packet(correction_context)
    _enforce_context_budget(
        packet,
        context_window_tokens=AUTHORING_CONTEXT_WINDOW_TOKENS,
        max_completion_tokens=AUTHORING_MAX_COMPLETION_TOKENS,
    )
    preflight = {
        "status": "passed",
        "estimator": "utf8_bytes_conservative_prompt_estimate",
        "system_bytes": len(packet.system.encode("utf-8")),
        "user_bytes": len(packet.user.encode("utf-8")),
        "prompt_bytes": packet.byte_size,
        "context_window_tokens": AUTHORING_CONTEXT_WINDOW_TOKENS,
        "max_completion_tokens": AUTHORING_MAX_COMPLETION_TOKENS,
        "framing_reserve": 256,
        "available_prompt_bytes": 24_320,
        "fits": packet.byte_size <= 24_320,
    }
    if not preflight["fits"]:
        raise ValueError(f"correction packet exceeds 24,320 bytes: {preflight['prompt_bytes']}")

    reconciliation = reconcile_budget()
    historical = _historical_judge_failures(candidate_info["historical_attempt"])
    ledger = {
        "schema_version": "authoring-dispatch-ledger-v1",
        "model_requests": 0,
        "dispatches": [],
        "note": "offline dry-run; dispatch-correction and dispatch-review were not run",
    }

    _write_json(output / "hash-verification.json", hashes)
    _write_json(
        output / "accepted-plan.json",
        {
            "path": str(FROZEN_EVIDENCE["accepted_plan"][0]),
            "sha256": candidate_info["plan_sha256"],
            "raw_sha256": candidate_info["plan_raw_sha256"],
            "plan": plan,
        },
    )
    _write_json(
        output / "saved-candidate.json",
        {
            "path": str(FROZEN_EVIDENCE["saved_candidate"][0]),
            "raw": raw_response_record(candidate_raw),
            **{key: value for key, value in candidate_info.items() if key != "historical_attempt"},
        },
    )
    _write_json(
        output / "fixture-sha256.json",
        {"path": str(FIXTURE_PATH), "sha256": fixture_sha256, "row_count": len(cases)},
    )
    _write_json(
        output / "controls-saved.json",
        {
            "detector_sha256": candidate_info["candidate_python_sha256"],
            "engine": "docker",
            "image": "python:3.12-slim",
            "findings": control_findings,
            "records": control_records,
            "failing_rows": [
                record["name"] for record in control_records if record["status"] != "passed"
            ],
        },
    )
    _write_json(output / "feedback.json", [item.as_dict() for item in feedback])
    _write_json(output / "findings.json", findings)
    _write_json(output / "historical-judge-controls.json", historical)
    _write_json(output / "preflight.json", preflight)
    _write_json(output / "budget-reconciliation.json", reconciliation)
    _write_json(output / "ledger.json", ledger)
    (output / "correction.system.txt").write_text(packet.system, encoding="utf-8")
    (output / "correction.user.txt").write_text(packet.user, encoding="utf-8")
    _write_json(
        output / "rendered-correction.json",
        {
            "stage": packet.stage,
            "version": packet.version,
            "sha256": packet.sha256,
            "byte_size": packet.byte_size,
            "system_sha256": _sha256(packet.system.encode("utf-8")),
            "user_sha256": _sha256(packet.user.encode("utf-8")),
            "section_sizes": _section_sizes(packet.user),
            "response_contract_occurrences": packet.user.count("RESPONSE CONTRACT\n"),
            "candidate_python_occurrences": packet.user.count(candidate.python_source),
        },
    )
    _write_json(
        output / "dry-run.json",
        {
            "schema_version": "o03-artifact-completion-dry-run-v1",
            "status": "passed",
            "output_dir": str(output),
            "accepted_plan_sha256": ACCEPTED_PLAN_SHA256,
            "saved_candidate_sha256": SAVED_CANDIDATE_SHA256,
            "fixture_sha256": fixture_sha256,
            "control_count": len(cases),
            "control_failures": [
                record["name"] for record in control_records if record["status"] != "passed"
            ],
            "model_requests": 0,
            "preflight": preflight,
            "budget_reconciliation": str(output / "budget-reconciliation.json"),
        },
    )
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--dispatch-correction", action="store_true")
    modes.add_argument("--dispatch-review", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--ledger", type=Path, help="existing evidence ledger for dispatch guard tests"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.dry_run:
        output = run_dry_run(args.output_dir)
        print(output)
        return 0
    ledger: Any = []
    if args.ledger is not None:
        ledger = _read_json(args.ledger)
    try:
        if args.dispatch_correction:
            dispatch_correction(ledger)
        else:
            dispatch_review(ledger)
    except DispatchSlotSpent:
        raise
    except DispatchModeStub as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
