#!/usr/bin/env python3
"""O03 artifact-correction readiness, live completion, and control replay."""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring import (
    ARTIFACT_REVIEW_PROMPT_VERSION,
    AUTHORING_CONTEXT_WINDOW_TOKENS,
    AUTHORING_MAX_COMPLETION_TOKENS,
    AUTHORING_THINKING_EXTRA_BODY,
    AuthoringBudget,
    PrivateModelAuthoringTransport,
    PromptPacket,
    ReviewResponseError,
    _enforce_context_budget,
    _mapping_sha256,
    _package_from_responses,
    _render_correction_packet,
    _render_sections,
    _response_parts,
    _safe_error,
    _safe_metadata,
    build_artifact_author_context,
    build_artifact_review_packet,
    build_artifact_reviewer_context,
    build_correction_context,
    collect_artifact_findings_v2,
    parse_call2_response,
    parse_historical_call1_response,
    parse_review_response,
)
from asago_artifact_generator.detector_controls import (
    ControlCase,
    build_detector_feedback,
    run_detector_controls,
)
from asago_artifact_generator.detector_runtime import execute_detector
from asago_artifact_generator.failure_evidence import (
    metadata_record,
    raw_response_record,
)
from asago_artifact_generator.package_io import load_package, write_package
from asago_artifact_generator.profiles import load_authoring_profile
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
LIVE_PROFILE_NAME = "gemma4-oc"
LIVE_MODEL = "gemma-4-26b-a4b-it"
LIVE_TASK_ID = HISTORICAL_TASK_ID
PRODUCER_ROOT = REPOSITORY_ROOT.parents[2]
PROFILES_PATH = PRODUCER_ROOT / "config" / "model-profiles.yaml"
LIVE_EVIDENCE_NAME = "live-evidence.json"
LIVE_SCHEMA_VERSION = "o03-artifact-completion-live-v1"
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
    """Raised when a dispatch helper is called without live mode enabled."""


class LiveGateFailure(RuntimeError):
    """Raised internally when a live completion gate fails."""

    def __init__(self, gate: str, detail: str) -> None:
        self.gate = gate
        self.detail = detail
        super().__init__(f"{gate}: {detail}")


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
    if isinstance(ledger, dict):
        records = []
        for key in ("ledger", "dispatches", "prior_dispatches"):
            value = ledger.get(key)
            if isinstance(value, list):
                records.extend(value)
    else:
        records = ledger
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


def _dry_run_directories(root: Path) -> list[Path]:
    return sorted(path for path in root.glob("O03-live-*-artifact-completion") if path.is_dir())


def load_dispatch_ledger(ledger_path: str | Path | None = None) -> dict[str, Any]:
    """Combine dispatch records from every artifact-completion dry-run directory."""

    requested = Path(ledger_path) if ledger_path is not None else RUNS_ROOT
    if requested.is_file():
        parent = requested.parent
        root = (
            parent.parent
            if parent.name.startswith("O03-live-") and parent.name.endswith("-artifact-completion")
            else parent
        )
    else:
        root = requested
    directories = _dry_run_directories(root)
    dispatches: list[dict[str, Any]] = []
    source_ledgers: list[str] = []
    for directory in directories:
        path = directory / "ledger.json"
        if not path.is_file():
            continue
        try:
            record = _read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        source_ledgers.append(str(path))
        entries = record.get("dispatches", [])
        if isinstance(entries, list):
            dispatches.extend(entry for entry in entries if isinstance(entry, dict))
    return {
        "schema_version": "authoring-dispatch-ledger-v1",
        "dry_run_directories": [str(path) for path in directories],
        "source_ledgers": source_ledgers,
        "latest_dry_run_directory": (
            str(Path(source_ledgers[-1]).parent) if source_ledgers else None
        ),
        "model_requests": sum(
            1 for dispatch in dispatches if dispatch.get("status") not in {"not_run", "skipped"}
        ),
        "dispatches": dispatches,
    }


def _candidate_digest(metadata: dict[str, Any], python_bytes: bytes) -> str:
    """Hash the exact metadata object and detector bytes used by package IO."""

    return _sha256(
        json.dumps(
            metadata,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\0"
        + python_bytes
    )


def _live_budget_from_snapshot(snapshot: dict[str, Any]) -> AuthoringBudget:
    """Rehydrate the reconciled generic budget without resetting counters."""

    required = (
        "aggregate_limit",
        "aggregate_spent",
        "task_limit",
        "task_spent",
        "author_correction_limit",
        "author_correction_spent",
        "review_limit",
        "review_spent",
    )
    if not all(isinstance(snapshot.get(key), int) for key in required):
        raise ValueError("live budget snapshot is incomplete")
    return AuthoringBudget(
        aggregate_limit=snapshot["aggregate_limit"],
        task_limit=snapshot["task_limit"],
        author_limit=snapshot["author_correction_limit"],
        review_limit=snapshot["review_limit"],
        total_dispatched=snapshot["aggregate_spent"],
        dispatched_by_task={LIVE_TASK_ID: snapshot["task_spent"]},
        dispatched_by_task_role={
            LIVE_TASK_ID: {
                "author": snapshot["author_correction_spent"],
                "reviewer": snapshot["review_spent"],
            }
        },
    )


def _latest_dry_run() -> tuple[dict[str, Any], Path]:
    """Return the newest dry-run ledger and its directory."""

    ledger = load_dispatch_ledger(RUNS_ROOT)
    directory = ledger.get("latest_dry_run_directory")
    if not isinstance(directory, str) or not directory:
        raise ValueError("no O03 dry-run evidence directory is available")
    path = Path(directory)
    if not (path / "ledger.json").is_file():
        raise ValueError("latest O03 dry-run ledger is unavailable")
    return ledger, path


def _inspected_correction_packet(directory: Path) -> PromptPacket:
    """Load and re-hash the exact offline request selected for dispatch."""

    rendered = _read_json(directory / "rendered-correction.json")
    system = (directory / "correction.system.txt").read_text(encoding="utf-8")
    user = (directory / "correction.user.txt").read_text(encoding="utf-8")
    packet = PromptPacket(
        stage=rendered.get("stage"),
        version=rendered.get("version"),
        system=system,
        user=user,
        payload={},
    )
    if rendered.get("sha256") != packet.sha256:
        raise LiveGateFailure(
            "request_fidelity",
            "the inspected correction request digest differs from its rendered bytes",
        )
    if rendered.get("byte_size") != packet.byte_size:
        raise LiveGateFailure(
            "request_fidelity",
            "the inspected correction request byte size differs from its rendered bytes",
        )
    preflight = _read_json(directory / "preflight.json")
    if (
        preflight.get("status") != "passed"
        or preflight.get("fits") is not True
        or preflight.get("prompt_bytes") != packet.byte_size
        or packet.byte_size > 24_320
    ):
        raise LiveGateFailure(
            "request_preflight",
            "the inspected correction request does not fit the saved context preflight",
        )
    return packet


def _write_live_state(directory: Path, state: dict[str, Any]) -> None:
    """Persist append-only live state beside the dry-run ledger."""

    _write_json(directory / LIVE_EVIDENCE_NAME, state)
    _write_json(directory / "ledger.json", state["ledger"])


def _live_controls(
    *,
    transport: PrivateModelAuthoringTransport,
    supplied: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return only safe, pinned provider controls for evidence."""

    controls = {
        "profile": LIVE_PROFILE_NAME,
        "model": LIVE_MODEL,
        "thinking": False,
        "temperature": 0.0,
        "max_completion_tokens": AUTHORING_MAX_COMPLETION_TOKENS,
        "context_window_tokens": AUTHORING_CONTEXT_WINDOW_TOKENS,
        "max_retries": 0,
        "extra_body": deepcopy(AUTHORING_THINKING_EXTRA_BODY),
    }
    if isinstance(supplied, dict):
        recorded = _safe_metadata(supplied)
        for key in (
            "model",
            "temperature",
            "max_retries",
            "max_completion_tokens",
            "context_window_tokens",
            "extra_body",
        ):
            if key in recorded:
                controls[key] = recorded[key]
    controls["profile"] = LIVE_PROFILE_NAME
    controls["model"] = LIVE_MODEL
    controls["thinking"] = False
    controls["max_retries"] = 0
    return controls


def _new_live_state(
    *,
    directory: Path,
    ledger: dict[str, Any],
    packet: PromptPacket,
    budget: AuthoringBudget,
) -> dict[str, Any]:
    """Create the append-only record before the provider call."""

    dispatches = list(ledger.get("dispatches", []))
    dispatch_index = len(dispatches) + 1
    record = {
        "dispatch_slot": "correction",
        "dispatch_index": dispatch_index,
        "attempt_index": 1,
        "role": "author",
        "stage": "correction",
        "task_id": LIVE_TASK_ID,
        "status": "pending",
        "prompt": {
            "version": packet.version,
            "sha256": packet.sha256,
            "system": packet.system,
            "user": packet.user,
        },
        "prompt_sha256": packet.sha256,
        "accepted_plan_sha256": ACCEPTED_PLAN_SHA256,
        "raw_response": raw_response_record(b"", reason="not_returned"),
        "usage": metadata_record(None, unavailable_reason="not_returned"),
        "controls": _live_controls(transport=_transport_placeholder()),
        "terminal_status": "in_progress",
    }
    dispatches.append(record)
    updated_ledger = {
        **ledger,
        "schema_version": "authoring-dispatch-ledger-v1",
        "dispatches": dispatches,
        "model_requests": sum(
            1 for item in dispatches if item.get("status") not in {"not_run", "skipped"}
        ),
        "latest_dry_run_directory": str(directory),
        "latest_request": {
            "system": str(directory / "correction.system.txt"),
            "user": str(directory / "correction.user.txt"),
            "rendered": str(directory / "rendered-correction.json"),
        },
        "note": "live completion in progress; correction slot reserved before dispatch",
    }
    return {
        "schema_version": LIVE_SCHEMA_VERSION,
        "status": "in_progress",
        "output_dir": str(directory),
        "task_id": LIVE_TASK_ID,
        "ledger": updated_ledger,
        "budget": budget.snapshot(LIVE_TASK_ID),
        "request": {
            "stage": packet.stage,
            "version": packet.version,
            "sha256": packet.sha256,
            "inspected_dry_run_sha256": packet.sha256,
            "matches_inspected_dry_run": True,
            "byte_size": packet.byte_size,
        },
        "correction": {"status": "pending", "attempt": record},
        "review": {"status": "not_reached"},
        "package": {"status": "not_reached"},
        "failed_gate": None,
        "findings": [],
    }


class _TransportPlaceholder:
    """Small non-network object used while reserving a ledger slot."""

    model = LIVE_MODEL
    temperature = 0.0


def _transport_placeholder() -> _TransportPlaceholder:
    return _TransportPlaceholder()


def _mark_live_failure(
    state: dict[str, Any],
    *,
    gate: str,
    detail: str,
    finding: dict[str, Any] | None = None,
) -> None:
    """Record a terminal experiment outcome without allowing a retry."""

    state["status"] = "failed"
    state["failed_gate"] = {"layer": gate, "detail": detail}
    if finding is not None:
        state.setdefault("findings", []).append(finding)
    state["correction"]["status"] = (
        "failed"
        if state["correction"].get("status") == "pending"
        else state["correction"]["status"]
    )
    for record in state["ledger"].get("dispatches", []):
        if record.get("dispatch_slot") in {"correction", "review"}:
            record["terminal_status"] = "failed"
            if record.get("status") == "pending":
                record["status"] = "failed"
    _write_live_state(Path(state["output_dir"]), state)


def _static_nested_access_findings(
    python_bytes: bytes,
    plan: dict[str, Any],
) -> list[dict[str, str]]:
    """Check that generated Python uses the declared nested packet paths."""

    try:
        tree = ast.parse(python_bytes.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        return [
            {
                "code": "nested_field_access",
                "detail": f"detector source cannot be parsed for nested access: {exc}",
                "path": "detector.py",
            }
        ]
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    required = {
        "bindings",
        "availability",
        "completeness",
        "tool_calls",
        "setup_draft_id",
        "setup_draft_status",
    }
    findings = [
        {
            "code": "nested_field_access",
            "detail": f"detector source does not access nested field {field!r}",
            "path": "detector.py",
        }
        for field in sorted(required - literals)
    ]
    if "tool_calls_availability" in literals:
        findings.append(
            {
                "code": "invented_evidence_path",
                "detail": "detector source uses unsupported root field tool_calls_availability",
                "path": "detector.py",
            }
        )
    if "tool_calls" in literals and "availability" in literals and "completeness" in literals:
        return findings
    return findings


def _expected_stimulus(plan: dict[str, Any]) -> dict[str, Any]:
    """Project the plan-owned stimulus fields for exact comparison."""

    approach = plan.get("stimulus_approach")
    if not isinstance(approach, dict):
        return {}
    request = approach.get("request")
    slots = sorted(set(re.findall(r"\{\{([a-zA-Z0-9_]+)\}\}", request or "")))
    return {
        "delivery": approach.get("delivery"),
        "history": deepcopy(approach.get("history", [])),
        "slots": slots,
        "user_text": request,
    }


def _parser_schema_plan_gate(
    *,
    raw: bytes,
    plan: dict[str, Any],
    prepared: Any,
) -> tuple[Any, list[dict[str, str]]]:
    """Run the ordered parser, schema, accepted-plan, and access checks."""

    try:
        parsed = parse_call2_response(raw)
    except Exception as exc:
        detail = _safe_error(exc)
        return None, [{"code": "parse_error", "detail": detail, "path": "response"}]
    findings = [
        finding.to_dict()
        for finding in collect_artifact_findings_v2(
            parsed,
            plan,
            prepared.inventory,
            prepared.runtime_contract,
        )
    ]
    if parsed.metadata.get("semantic_judge_spec") is not None:
        findings.append(
            {
                "code": "plan_conflict",
                "detail": "semantic_judge_spec must remain null for the accepted no-judge plan",
                "path": "semantic_judge_spec",
            }
        )
    if _mapping_sha256(plan) != ACCEPTED_PLAN_SHA256:
        findings.append(
            {
                "code": "plan_digest_changed",
                "detail": "accepted plan digest differs from the frozen authority",
                "path": "accepted_plan",
            }
        )
    stimulus = parsed.metadata.get("stimulus")
    if stimulus != _expected_stimulus(plan):
        findings.append(
            {
                "code": "stimulus_meaning_changed",
                "detail": "replacement stimulus differs from the accepted plan",
                "path": "stimulus",
            }
        )
    if "{{setup_draft_id}}" not in (
        stimulus.get("user_text", "") if isinstance(stimulus, dict) else ""
    ):
        findings.append(
            {
                "code": "literal_slot_missing",
                "detail": "replacement does not preserve the literal {{setup_draft_id}} slot",
                "path": "stimulus.user_text",
            }
        )
    findings.extend(_static_nested_access_findings(parsed.python_bytes, plan))
    return parsed, findings


def _compact_review_controls(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep review evidence bounded while retaining every control outcome."""

    keys = ("name", "expected_outcome", "observed_outcome", "status", "failure")
    return [{key: record.get(key) for key in keys} for record in records]


def _bounded_artifact_review_packet(
    *,
    prepared: Any,
    plan: dict[str, Any],
    metadata: dict[str, Any],
    python_bytes: bytes,
    controls: list[dict[str, Any]],
) -> PromptPacket:
    """Render the exact review role with a context-fitting control projection."""

    context = build_artifact_reviewer_context(
        prepared.input_view,
        plan,
        metadata,
        python_bytes,
        controls,
        prepared.inventory,
        prepared.runtime_contract,
    )
    template = build_artifact_review_packet(
        prepared.input_view,
        plan,
        metadata,
        python_bytes,
        [],
        prepared.inventory,
        prepared.runtime_contract,
    )
    sections = (
        (
            "ORIGINAL SCENARIO",
            context["original_scenario"],
        ),
        ("ACCEPTED PLAN", context["accepted_plan"]),
        ("RUNTIME EVIDENCE INTERFACE", context["evidence_packet_interface"]),
        ("CANDIDATE METADATA", context["candidate_metadata"]),
        ("EXACT DETECTOR PYTHON", context["candidate_python_source"]),
        ("RESOLVED RUNTIME CONTEXT", context["resolved_runtime_context"]),
        (
            "ACTUAL OFFLINE CONTROL RESULTS",
            _compact_review_controls(controls),
        ),
        ("REVIEW RESPONSE CONTRACT", context["response_contract"]),
    )
    packet = PromptPacket(
        stage="artifact_review",
        version=ARTIFACT_REVIEW_PROMPT_VERSION,
        system=template.system,
        user=_render_sections(sections),
        payload={
            "interface": "artifact-authoring-v2",
            "stage": "artifact_review",
            "evidence_packet_interface": deepcopy(context["evidence_packet_interface"]),
            "candidate_metadata": deepcopy(metadata),
            "candidate_python_source": context["candidate_python_source"],
            "control_results": _compact_review_controls(controls),
        },
    )
    _enforce_context_budget(
        packet,
        context_window_tokens=AUTHORING_CONTEXT_WINDOW_TOKENS,
        max_completion_tokens=AUTHORING_MAX_COMPLETION_TOKENS,
    )
    return packet


def _dispatch_transport(
    *,
    packet: PromptPacket,
    profile: Any,
) -> tuple[
    bytes,
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    PrivateModelAuthoringTransport,
]:
    """Dispatch through the pinned private profile with retries disabled."""

    transport = PrivateModelAuthoringTransport(
        base_url=profile.base_url,
        api_key=profile.api_key,
        model=profile.model,
        profile_name=profile.name,
        temperature=0.0,
        extra_body=deepcopy(AUTHORING_THINKING_EXTRA_BODY),
        max_completion_tokens=AUTHORING_MAX_COMPLETION_TOKENS,
        context_window_tokens=AUTHORING_CONTEXT_WINDOW_TOKENS,
    )
    response = transport.complete(packet)
    return (*_response_parts(response), transport)


def dispatch_correction(ledger: Any, *, live: bool = False) -> dict[str, Any] | None:
    """Guard and optionally execute the sole correction dispatch."""

    ensure_dispatch_slot_available(ledger, "correction")
    if not live:
        raise DispatchModeStub("correction dispatch requires live mode")
    return _run_live_correction()


def dispatch_review(ledger: Any, *, live: bool = False) -> dict[str, Any] | None:
    """Guard and optionally execute the sole conditional review dispatch."""

    ensure_dispatch_slot_available(ledger, "review")
    if not live:
        raise DispatchModeStub("review dispatch requires live mode")
    return _run_live_review()


def _load_live_profile() -> Any:
    """Load and validate the owner-approved private profile without logging it."""

    profile = load_authoring_profile(PROFILES_PATH, LIVE_PROFILE_NAME)
    if profile.model != LIVE_MODEL:
        raise LiveGateFailure(
            "profile",
            "gemma4-oc does not resolve to the pinned gemma-4-26b-a4b-it model",
        )
    return profile


def _reserve_live_budget(
    *,
    reconciliation: dict[str, Any],
    role: str,
) -> AuthoringBudget:
    """Reserve one role against the reconciled shared counters."""

    budget = _live_budget_from_snapshot(reconciliation["resulting_budget"])
    budget.reserve(LIVE_TASK_ID, role=role)
    return budget


def _record_response(
    *,
    state: dict[str, Any],
    record: dict[str, Any],
    response: tuple[
        bytes,
        dict[str, Any] | None,
        dict[str, Any] | None,
        dict[str, Any] | None,
        PrivateModelAuthoringTransport,
    ],
) -> bytes:
    """Persist raw provider bytes and controls before any parsing."""

    raw, usage, supplied_controls, response_capture, transport = response
    raw_key = f"dispatch:{record['dispatch_index']}"
    effective = _live_controls(transport=transport, supplied=supplied_controls)
    record["status"] = "returned"
    record["raw_response_key"] = raw_key
    record["raw_response"] = raw_response_record(raw)
    record["usage"] = metadata_record(
        usage if usage else None,
        unavailable_reason="provider_did_not_report_usage",
    )
    record["controls"] = effective
    if response_capture is not None:
        record["response_capture"] = deepcopy(response_capture)
    record["raw_response_sha256"] = _sha256(raw)
    record["raw_response_bytes"] = len(raw)
    state["budget"] = _live_budget_from_snapshot(state["budget"]).snapshot(LIVE_TASK_ID)
    if record.get("dispatch_slot") == "review":
        state["review"]["attempt"] = record
    else:
        state["correction"]["attempt"] = record
    _write_live_state(Path(state["output_dir"]), state)
    return raw


def _run_live_correction() -> dict[str, Any]:
    """Spend the one correction slot and run the ordered local gates."""

    ledger, directory = _latest_dry_run()
    packet = _inspected_correction_packet(directory)
    reconciliation = _read_json(directory / "budget-reconciliation.json")
    profile = _load_live_profile()
    budget = _reserve_live_budget(reconciliation=reconciliation, role="author")
    state = _new_live_state(
        directory=directory,
        ledger=ledger,
        packet=packet,
        budget=budget,
    )
    state["budget"] = budget.snapshot(LIVE_TASK_ID)
    state["correction"]["attempt"]["controls"] = {
        "profile": LIVE_PROFILE_NAME,
        "model": LIVE_MODEL,
        "thinking": False,
        "temperature": 0.0,
        "max_completion_tokens": AUTHORING_MAX_COMPLETION_TOKENS,
        "context_window_tokens": AUTHORING_CONTEXT_WINDOW_TOKENS,
        "max_retries": 0,
        "extra_body": deepcopy(AUTHORING_THINKING_EXTRA_BODY),
    }
    _write_live_state(directory, state)
    transport: PrivateModelAuthoringTransport | None = None
    try:
        result = _dispatch_transport(packet=packet, profile=profile)
        raw, usage, supplied_controls, response_capture, transport = result
        _record_response(
            state=state,
            record=state["correction"]["attempt"],
            response=(raw, usage, supplied_controls, response_capture, transport),
        )
    except Exception as exc:
        detail = _safe_error(exc)
        record = state["correction"]["attempt"]
        record["status"] = "transport_failure"
        record["error"] = detail
        record["raw_response"] = raw_response_record(b"", reason="provider_failure")
        record["usage"] = metadata_record(None, unavailable_reason="provider_failure")
        record["controls"] = state["correction"]["attempt"]["controls"]
        _mark_live_failure(
            state,
            gate="correction_dispatch",
            detail=detail,
            finding={"code": "transport_failure", "detail": detail, "path": "correction"},
        )
        return state

    prepared = prepare_o03_authoring_inputs()
    plan, _, _ = _extract_authorities()
    parsed, findings = _parser_schema_plan_gate(
        raw=raw,
        plan=plan,
        prepared=prepared,
    )
    attempt = state["correction"]["attempt"]
    if parsed is not None:
        candidate_digest = _candidate_digest(parsed.metadata, parsed.python_bytes)
        attempt["candidate_sha256"] = candidate_digest
        attempt["candidate_metadata_sha256"] = _mapping_sha256(parsed.metadata)
        attempt["candidate_python_sha256"] = _sha256(parsed.python_bytes)
        attempt["candidate_python_byte_length"] = len(parsed.python_bytes)
        state["correction"]["candidate_sha256"] = candidate_digest
        state["correction"]["metadata"] = deepcopy(parsed.metadata)
        state["correction"]["python_sha256"] = _sha256(parsed.python_bytes)
    if findings:
        attempt["gate"] = {
            "layer": "parser_schema_plan",
            "findings": findings,
        }
        _mark_live_failure(
            state,
            gate="parser_schema_plan",
            detail=json.dumps(findings, ensure_ascii=False, sort_keys=True),
            finding={"code": "parser_schema_plan", "findings": findings},
        )
        return state
    assert parsed is not None
    attempt["gate"] = {"layer": "parser_schema_plan", "status": "passed"}
    fixture_record = _read_json(directory / "fixture-sha256.json")
    current_fixture_sha256 = _sha256(FIXTURE_PATH.read_bytes())
    if fixture_record.get("sha256") != current_fixture_sha256:
        _mark_live_failure(
            state,
            gate="frozen_controls",
            detail="control fixture sha256 differs from the inspected dry-run fixture",
            finding={"code": "fixture_digest_changed", "path": "fixture_sha256"},
        )
        return state
    cases = load_control_cases()
    control_findings, control_records = run_detector_controls(
        parsed.python_bytes,
        cases=cases,
    )
    state["replacement_controls"] = {
        "detector_sha256": _sha256(parsed.python_bytes),
        "fixture_sha256": current_fixture_sha256,
        "engine": "docker",
        "image": "python:3.12-slim",
        "findings": control_findings,
        "records": control_records,
        "passed_rows": [
            record["name"] for record in control_records if record["status"] == "passed"
        ],
        "failing_rows": [
            record["name"] for record in control_records if record["status"] != "passed"
        ],
    }
    _write_live_state(directory, state)
    if control_findings or any(record.get("status") != "passed" for record in control_records):
        detail = json.dumps(
            {
                "findings": control_findings,
                "failing_rows": state["replacement_controls"]["failing_rows"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        _mark_live_failure(
            state,
            gate="frozen_controls",
            detail=detail,
            finding={"code": "frozen_control_failure", "detail": detail},
        )
        return state
    state["correction"]["status"] = "passed"
    state["status"] = "ready_for_review"
    state["budget"] = budget.snapshot(LIVE_TASK_ID)
    _write_live_state(directory, state)
    return state


def _run_live_review() -> dict[str, Any]:
    """Spend the one review slot only after the correction gates pass."""

    ledger, directory = _latest_dry_run()
    evidence_path = directory / LIVE_EVIDENCE_NAME
    if not evidence_path.is_file():
        raise LiveGateFailure(
            "review_precondition",
            "correction evidence is unavailable; review was not dispatched",
        )
    state = _read_json(evidence_path)
    if state.get("correction", {}).get("status") != "passed":
        raise LiveGateFailure(
            "review_precondition",
            "correction did not pass parser/schema/plan and frozen-control gates",
        )
    prepared = prepare_o03_authoring_inputs()
    plan, _, _ = _extract_authorities()
    correction_attempt = state["correction"]["attempt"]
    raw_record = correction_attempt.get("raw_response")
    if not isinstance(raw_record, dict) or raw_record.get("availability") != "available":
        raise LiveGateFailure("review_precondition", "correction raw response is unavailable")
    raw = base64.b64decode(raw_record["base64"], validate=True)
    parsed, findings = _parser_schema_plan_gate(
        raw=raw,
        plan=plan,
        prepared=prepared,
    )
    if parsed is None or findings:
        raise LiveGateFailure(
            "review_precondition",
            "saved correction evidence no longer passes the parser/schema/plan gate",
        )
    controls = state.get("replacement_controls", {}).get("records", [])
    review_packet = _bounded_artifact_review_packet(
        prepared=prepared,
        plan=plan,
        metadata=parsed.metadata,
        python_bytes=parsed.python_bytes,
        controls=controls,
    )
    state["review_request"] = {
        "version": review_packet.version,
        "sha256": review_packet.sha256,
        "byte_size": review_packet.byte_size,
        "contains_evidence_interface": "RUNTIME EVIDENCE INTERFACE\n" in review_packet.user,
        "interface_sha256": _sha256(
            (
                "RUNTIME EVIDENCE INTERFACE\n"
                + review_packet.user.split("RUNTIME EVIDENCE INTERFACE\n", 1)[1].split("\n\n", 1)[
                    0
                ]
            ).encode("utf-8")
        ),
    }
    reconciliation = {
        "resulting_budget": state["budget"],
    }
    profile = _load_live_profile()
    budget = _reserve_live_budget(reconciliation=reconciliation, role="reviewer")
    dispatches = list(ledger.get("dispatches", []))
    review_index = len(dispatches) + 1
    review_record = {
        "dispatch_slot": "review",
        "dispatch_index": review_index,
        "attempt_index": 1,
        "role": "reviewer",
        "stage": "artifact_review",
        "task_id": LIVE_TASK_ID,
        "status": "pending",
        "prompt": {
            "version": review_packet.version,
            "sha256": review_packet.sha256,
            "system": review_packet.system,
            "user": review_packet.user,
        },
        "prompt_sha256": review_packet.sha256,
        "reviewed_input_sha256": review_packet.sha256,
        "reviewed_candidate_sha256": _candidate_digest(parsed.metadata, parsed.python_bytes),
        "raw_response": raw_response_record(b"", reason="not_returned"),
        "usage": metadata_record(None, unavailable_reason="not_returned"),
        "controls": {
            "profile": LIVE_PROFILE_NAME,
            "model": LIVE_MODEL,
            "thinking": False,
            "temperature": 0.0,
            "max_completion_tokens": AUTHORING_MAX_COMPLETION_TOKENS,
            "context_window_tokens": AUTHORING_CONTEXT_WINDOW_TOKENS,
            "max_retries": 0,
            "extra_body": deepcopy(AUTHORING_THINKING_EXTRA_BODY),
        },
        "terminal_status": "in_progress",
    }
    dispatches.append(review_record)
    state["ledger"] = {
        **ledger,
        "dispatches": dispatches,
        "model_requests": sum(
            1 for item in dispatches if item.get("status") not in {"not_run", "skipped"}
        ),
        "latest_dry_run_directory": str(directory),
        "latest_request": {
            "system": str(directory / "correction.system.txt"),
            "user": str(directory / "correction.user.txt"),
            "rendered": str(directory / "rendered-correction.json"),
        },
        "note": "live completion in progress; correction and review slots are append-only",
    }
    state["budget"] = budget.snapshot(LIVE_TASK_ID)
    state["review"] = {"status": "pending", "attempt": review_record}
    _write_live_state(directory, state)
    try:
        response = _dispatch_transport(packet=review_packet, profile=profile)
        raw_review, usage, supplied_controls, response_capture, transport = response
        _record_response(
            state=state,
            record=review_record,
            response=(raw_review, usage, supplied_controls, response_capture, transport),
        )
    except Exception as exc:
        detail = _safe_error(exc)
        review_record["status"] = "transport_failure"
        review_record["error"] = detail
        review_record["raw_response"] = raw_response_record(b"", reason="provider_failure")
        _mark_live_failure(
            state,
            gate="review_dispatch",
            detail=detail,
            finding={"code": "transport_failure", "detail": detail, "path": "artifact_review"},
        )
        return state
    try:
        review = parse_review_response(raw_review)
    except ReviewResponseError as exc:
        detail = _safe_error(exc)
        review_record["review_error"] = [item.to_dict() for item in exc.findings]
        _mark_live_failure(
            state,
            gate="review_parse",
            detail=detail,
            finding={"code": "review_parse_error", "detail": detail, "path": "artifact_review"},
        )
        return state
    review_payload = {
        "decision": review.decision,
        "summary": review.summary,
        "findings": [dict(item) for item in review.findings],
    }
    review_record["review"] = {
        "status": "accepted" if review.decision == "accept" else review.decision,
        **review_payload,
    }
    review_record["decision"] = review.decision
    review_record["summary"] = review.summary
    review_record["findings"] = deepcopy(review_payload["findings"])
    state["review"] = {"status": review.decision, **review_payload, "attempt": review_record}
    _write_live_state(directory, state)
    if review.decision != "accept":
        detail = json.dumps(review_payload, ensure_ascii=False, sort_keys=True)
        _mark_live_failure(
            state,
            gate="review_decision",
            detail=detail,
            finding={"code": "review_not_accepted", "detail": detail, "path": "artifact_review"},
        )
        return state

    state["review"]["status"] = "accepted"
    state["status"] = "review_accepted"
    candidate_digest = _candidate_digest(parsed.metadata, parsed.python_bytes)
    plan_review = _read_json(FROZEN_EVIDENCE["accepted_plan"][0])
    accepted_plan_review = next(
        (
            deepcopy(attempt["review"])
            for attempt in plan_review.get("attempts", [])
            if isinstance(attempt, dict)
            and attempt.get("stage") == "plan_review"
            and isinstance(attempt.get("review"), dict)
        ),
        {"status": "accepted", "decision": "accept", "findings": []},
    )
    artifact = {
        **parsed.metadata,
        "setup_recipe": plan["setup_recipe"],
        "runtime_bindings": plan["runtime_bindings"],
        "prerequisites": plan["prerequisites"],
        "required_observations": plan["required_observations"],
    }
    continuation = {
        "mode": LIVE_SCHEMA_VERSION,
        "dry_run_directory": str(directory),
        "accepted_plan_sha256": ACCEPTED_PLAN_SHA256,
        "fixture_sha256": state["replacement_controls"]["fixture_sha256"],
        "reviewed_candidate_sha256": candidate_digest,
        "intervening_dispatches": _read_json(directory / "budget-reconciliation.json")[
            "intervening_dispatches"
        ],
        "live_requests": {"correction": 1, "review": 1},
    }
    package_ledger = []
    for record in state["ledger"]["dispatches"]:
        package_record = deepcopy(record)
        package_record.pop("prompt", None)
        package_ledger.append(package_record)
    try:
        package = _package_from_responses(
            view=prepared.input_view,
            plan=plan,
            artifact=artifact,
            task_id=LIVE_TASK_ID,
            ledger=package_ledger,
            raw_responses={
                "dispatch:1": raw,
                "correction": raw,
                "dispatch:2": raw_review,
                "artifact_review": raw_review,
            },
            decoded_responses={
                "correction": parsed.metadata,
                "artifact_review": review_payload,
            },
            prompt_packets={
                "artifact_review": review_packet,
            },
            transformations=[],
            inventory=prepared.inventory,
            runtime_contract=prepared.runtime_contract,
            continuation=continuation,
            detector_bytes=parsed.python_bytes,
            interface_version="artifact-authoring-v2",
            review_status={"plan": "accepted", "artifact": "accepted"},
            preserved_reviews={"plan": accepted_plan_review, "artifact": review_payload},
            terminal_status="accepted",
            budget=budget.snapshot(LIVE_TASK_ID),
        )
        package_path = write_package(directory / "package", package)
        loaded = load_package(package_path)
    except Exception as exc:
        detail = _safe_error(exc)
        _mark_live_failure(
            state,
            gate="package",
            detail=detail,
            finding={"code": "package_verification_failed", "detail": detail, "path": "package"},
        )
        return state
    packaged_digest = _candidate_digest(parsed.metadata, loaded.members["detector.py"])
    package_bytes = b"".join(loaded.members.values())
    package_checks = {
        "load_package": "passed",
        "manifest_digest": loaded.manifest.manifest_digest,
        "reviewed_candidate_sha256": candidate_digest,
        "packaged_candidate_sha256": packaged_digest,
        "reviewed_digest_equals_packaged_digest": packaged_digest == candidate_digest,
        "accepted_status": loaded.manifest.authoring.get("status") == "accepted",
        "literal_slot_preserved": b"{{setup_draft_id}}" in loaded.members["stimulus.json"],
        "synthetic_values_leaked": b"SYN-" in package_bytes,
    }
    if (
        not package_checks["reviewed_digest_equals_packaged_digest"]
        or not package_checks["accepted_status"]
        or not package_checks["literal_slot_preserved"]
        or package_checks["synthetic_values_leaked"]
    ):
        detail = json.dumps(package_checks, ensure_ascii=False, sort_keys=True)
        _mark_live_failure(
            state,
            gate="package",
            detail=detail,
            finding={"code": "package_invariant_failure", "detail": detail, "path": "package"},
        )
        return state
    cases = load_control_cases()
    check = execute_detector(package_path, cases[0].evidence)
    check_record = {
        "status": check.status,
        "result": check.result,
        "failure": check.failure,
        "package_digest": check.package_digest,
        "package_digest_after": check.package_digest_after,
        "detector_sha256": check.detector_sha256,
        "detector_sha256_after": check.detector_sha256_after,
    }
    package_checks["check"] = check_record
    state["package"] = {
        "status": "accepted",
        "path": str(package_path),
        "manifest_digest": loaded.manifest.manifest_digest,
        "checks": package_checks,
    }
    state["status"] = "accepted"
    state["failed_gate"] = None
    state["budget"] = budget.snapshot(LIVE_TASK_ID)
    for record in state["ledger"]["dispatches"]:
        record["terminal_status"] = "accepted"
    _write_live_state(directory, state)
    return state


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
    previous_ledger = load_dispatch_ledger(RUNS_ROOT)
    ledger = {
        "schema_version": "authoring-dispatch-ledger-v1",
        "model_requests": 0,
        "dispatches": [],
        "prior_dispatches": previous_ledger["dispatches"],
        "dry_run_directories": previous_ledger["dry_run_directories"],
        "source_ledgers": previous_ledger["source_ledgers"],
        "latest_dry_run_directory": str(output),
        "latest_request": {
            "system": str(output / "correction.system.txt"),
            "user": str(output / "correction.user.txt"),
            "rendered": str(output / "rendered-correction.json"),
        },
        "guard_scope": "all O03 artifact-completion dry-run directories",
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
            "latest_request": ledger["latest_request"],
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


def _recorded_dispatch(ledger: dict[str, Any], slot: str) -> dict[str, Any] | None:
    """Return a redacted summary of an already-spent slot."""

    for record in ledger.get("dispatches", []):
        if record.get("dispatch_slot") == slot:
            return {
                "dispatch_slot": slot,
                "dispatch_index": record.get("dispatch_index"),
                "stage": record.get("stage"),
                "status": record.get("status"),
                "terminal_status": record.get("terminal_status"),
                "prompt_sha256": record.get("prompt_sha256"),
                "raw_response_sha256": record.get("raw_response_sha256"),
                "candidate_sha256": record.get("candidate_sha256"),
                "decision": record.get("decision"),
            }
    return None


def _record_pre_dispatch_failure(gate: LiveGateFailure) -> None:
    """Persist a gate failure that occurs before a dispatch reservation."""

    try:
        _, directory = _latest_dry_run()
    except Exception:
        return
    path = directory / LIVE_EVIDENCE_NAME
    if path.is_file():
        state = _read_json(path)
    else:
        ledger = _read_json(directory / "ledger.json")
        state = {
            "schema_version": LIVE_SCHEMA_VERSION,
            "status": "failed",
            "output_dir": str(directory),
            "task_id": LIVE_TASK_ID,
            "ledger": ledger,
            "budget": _read_json(directory / "budget-reconciliation.json")["resulting_budget"],
            "correction": {"status": "not_reached"},
            "review": {"status": "not_reached"},
            "package": {"status": "not_reached"},
            "findings": [],
        }
    state["status"] = "failed"
    state["failed_gate"] = {"layer": gate.gate, "detail": gate.detail}
    state.setdefault("findings", []).append(
        {"code": "pre_dispatch_gate", "detail": gate.detail, "path": gate.gate}
    )
    _write_live_state(directory, state)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.dry_run:
        output = run_dry_run(args.output_dir)
        print(output)
        return 0
    ledger: Any = load_dispatch_ledger(args.ledger)
    slot = "correction" if args.dispatch_correction else "review"
    try:
        if args.dispatch_correction:
            state = dispatch_correction(ledger, live=True)
        else:
            state = dispatch_review(ledger, live=True)
    except DispatchSlotSpent:
        print(
            json.dumps(
                {
                    "status": "already_spent",
                    "dispatch_slot": slot,
                    "recorded": _recorded_dispatch(ledger, slot),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except LiveGateFailure as exc:
        _record_pre_dispatch_failure(exc)
        print(
            json.dumps(
                {
                    "status": "failed",
                    "failed_gate": {"layer": exc.gate, "detail": exc.detail},
                    "dispatch_slot": slot,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    except DispatchModeStub as exc:
        raise SystemExit(str(exc)) from exc
    assert isinstance(state, dict)
    print(
        json.dumps(
            {
                "status": state.get("status"),
                "output_dir": state.get("output_dir"),
                "failed_gate": state.get("failed_gate"),
                "correction": {
                    "status": state.get("correction", {}).get("status"),
                    "candidate_sha256": state.get("correction", {}).get("candidate_sha256"),
                },
                "review": {
                    "status": state.get("review", {}).get("status"),
                    "decision": state.get("review", {}).get("decision"),
                },
                "package": {
                    "status": state.get("package", {}).get("status"),
                    "path": state.get("package", {}).get("path"),
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if state.get("status") == "accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
