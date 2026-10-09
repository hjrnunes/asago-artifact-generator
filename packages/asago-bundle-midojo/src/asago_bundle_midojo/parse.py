"""parse: turn MiDojo's control-plane records for one bundle into ``execution-receipt-v1``.

The run script left the control plane's run and evaluation records in the
bundle, and the verifier left its verdict. A usable run holds exactly one graded
evaluation whose verdict and ``security`` flag agree. Anything else yields a
``failed`` receipt whose ``incomplete_reason`` names what was wrong; the receipt
is still written.

The control plane's flag has two values and the contract three: ``security`` is
true exactly when the verdict is ``detected``, and the verdict file tells
``not_detected`` from ``inconclusive``. When orch's boundary services recorded
the package, their calls replace the control plane's, and the two counts must
agree.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asago_bundle_core.errors import BundleError
from asago_bundle_core.text import canonical_text

from .compiler import (
    BUNDLE_CODE,
    BUNDLE_FILE,
    CLAIM_LEVEL,
    EVALUATION_DUMP,
    NATIVE_OUTPUTS,
    RUN_DUMP,
    SUITE_FILE,
    TOOL,
    VERDICT_FILE,
)
from .records import Records, native_call, read_json, read_records, text

SCHEMA_VERSION = "execution-receipt-v1"
DETECTOR = "ToolCallCondition"
OUTCOMES = ("detected", "not_detected", "inconclusive")
DIGESTED = (BUNDLE_FILE, SUITE_FILE, *BUNDLE_CODE)
MANIFEST_KEYS = frozenset({"scenario_id", "package_digest", "target_mode", "claim_level"})
RUNTIME_LISTS = (
    "processes",
    "network_calls_allowed",
    "network_calls_blocked",
    "files_created",
    "files_modified",
    "files_deleted",
    "security_findings",
)


class ParseError(BundleError):
    """The bundle cannot be read, so no receipt can name its package."""


@dataclass(frozen=True)
class Bundle:
    root: Path
    manifest: dict[str, Any]
    prompt: str


@dataclass(frozen=True)
class Evidence:
    """The three native documents of one run."""

    run: dict[str, Any]
    evaluation: dict[str, Any]
    verdict: dict[str, Any]


def parse_bundle(
    bundle_dir: str | Path,
    out_path: str | Path,
    *,
    records_dir: str | Path | None = None,
    tool_revision: str | None = None,
    adapter_revision: str | None = None,
) -> dict[str, Any]:
    """Write the receipt for one bundle's control-plane records to ``out_path`` and return it."""

    bundle = read_bundle(Path(bundle_dir))
    out = Path(out_path)
    records = read_records(Path(records_dir)) if records_dir else Records()
    attempt, reason = build_attempt(bundle, records)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"id": TOOL, "revision": tool_revision, "adapter_revision": adapter_revision},
        "package": {
            "id": bundle.manifest["scenario_id"],
            "digest": bundle.manifest["package_digest"],
        },
        "bundle_digest": bundle_digest(bundle.root),
        "target": {
            "mode": bundle.manifest["target_mode"],
            "image_digest": None,
            "policy_digest": None,
            "sandbox_id": None,
        },
        "execution_status": "failed" if reason else "completed",
        "runtime_status": None if reason else "completed",
        "incomplete_reason": reason,
        "attempts": [attempt] if attempt else [],
        "native": native_files(bundle.root, out.parent),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_text(receipt), encoding="utf-8")
    return receipt


def read_bundle(root: Path) -> Bundle:
    try:
        manifest = json.loads((root / BUNDLE_FILE).read_text(encoding="utf-8"))
        suite = json.loads((root / SUITE_FILE).read_text(encoding="utf-8"))
        prompt = suite["user_tasks"][0]["prompt"]
        missing = MANIFEST_KEYS - manifest.keys()
    except (OSError, IndexError, KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ParseError(f"bundle is unreadable: {exc}") from exc
    if missing:
        raise ParseError(f"bundle manifest lacks {sorted(missing)}")
    return Bundle(root, manifest, prompt)


def build_attempt(bundle: Bundle, records: Records) -> tuple[dict[str, Any] | None, str | None]:
    """Return the run's one attempt, or why the run cannot be graded."""

    evidence, reason = read_evidence(bundle.root)
    if evidence is None:
        return None, reason
    reason = evidence_failure(evidence, bundle, records)
    if reason:
        return None, reason
    return attempt_of(evidence, bundle, records), None


def read_evidence(root: Path) -> tuple[Evidence | None, str | None]:
    documents = []
    for name, prefix in ((RUN_DUMP, "control_plane"), (EVALUATION_DUMP, "control_plane")):
        document, reason = read_document(root / name, prefix)
        if document is None:
            return None, reason
        documents.append(document)
    verdict, reason = read_document(root / VERDICT_FILE, "verdict")
    if verdict is None:
        # A grade that raised leaves no verdict; the ungraded evaluation is the cause.
        return None, reason if is_graded(documents[1]) else "evaluation_ungraded"
    return Evidence(documents[0], documents[1], verdict), None


def read_document(path: Path, prefix: str) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, f"{prefix}_missing"
    document = read_json(path)
    if not isinstance(document, dict):
        return None, f"{prefix}_malformed"
    return document, None


Check = Callable[[Evidence, Bundle, Records], "str | None"]


def evidence_failure(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    """Return the first reason the evidence cannot be graded, or None."""

    for check in CHECKS:
        reason = check(evidence, bundle, records)
        if reason:
            return reason
    return None


def _evaluation_count(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    evaluations = evidence.run.get("evaluations")
    ok = isinstance(evaluations, list) and len(evaluations) == 1
    return None if ok else "evaluation_count_mismatch"


def _evaluation_identity(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    listed = evidence.run["evaluations"][0]
    same = isinstance(listed, dict) and listed.get("id") == evidence.evaluation.get("id")
    return None if same else "evaluation_mismatch"


def is_graded(evaluation: dict[str, Any]) -> bool:
    return evaluation.get("completed") is True and isinstance(evaluation.get("security"), bool)


def _graded(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    return None if is_graded(evidence.evaluation) else "evaluation_ungraded"


def _verdict_shape(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    verdict = evidence.verdict
    matched = verdict.get("matched_calls")
    ok = (
        verdict.get("outcome") in OUTCOMES
        and verdict.get("claim_level") == bundle.manifest["claim_level"] == CLAIM_LEVEL
        and text(verdict.get("reason")) is not None
        and isinstance(matched, list)
        and all(type(index) is int and index >= 0 for index in matched)
    )
    return None if ok else "verdict_malformed"


def _verdict_agrees(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    detected = evidence.verdict["outcome"] == "detected"
    return None if evidence.evaluation["security"] is detected else "verdict_disagrees"


def _prompt_matches(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    same = evidence.evaluation.get("agent_input") == bundle.prompt
    return None if same else "agent_input_mismatch"


def _calls_shape(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    calls = evidence.evaluation.get("function_calls")
    ok = isinstance(calls, list) and all(isinstance(call, dict) for call in calls)
    return None if ok else "function_calls_malformed"


def _records_agree(evidence: Evidence, bundle: Bundle, records: Records) -> str | None:
    if records.malformed:
        return "records_malformed"
    if records.undelivered:
        return "sink_failed"
    if records.calls is not None and len(records.calls) != len(
        evidence.evaluation["function_calls"]
    ):
        return "tool_call_count_mismatch"
    return None


CHECKS: tuple[Check, ...] = (
    _evaluation_count,
    _evaluation_identity,
    _graded,
    _verdict_shape,
    _verdict_agrees,
    _prompt_matches,
    _calls_shape,
    _records_agree,
)


def attempt_of(evidence: Evidence, bundle: Bundle, records: Records) -> dict[str, Any]:
    evaluation, verdict = evidence.evaluation, evidence.verdict
    source, calls = observed_calls(evaluation["function_calls"], records)
    output = text(evaluation.get("agent_output"))
    replies = [output] if output else []
    return {
        "index": 0,
        "generation": {
            "request_count": records.request_count,
            "response_status": "completed",
            "error_type": None,
        },
        "turns": [
            {
                "index": 0,
                "request": {"messages": [{"role": "user", "content": bundle.prompt}]},
                "response": {
                    "assistant_messages": replies,
                    "tool_call_indices": list(range(len(calls))),
                },
            }
        ],
        "result": {
            "outcome": verdict["outcome"],
            "claim_level": verdict["claim_level"],
            "reason": verdict["reason"],
        },
        "observation": {"source": source, "assistant_messages": replies, "tool_calls": calls},
        "runtime_observations": {"source": None, **{name: [] for name in RUNTIME_LISTS}},
        "detections": [
            {
                "detector": DETECTOR,
                "outcome": verdict["outcome"],
                "reason": verdict["reason"],
                "matched_calls": verdict["matched_calls"],
            }
        ],
        "judge": None,
    }


def observed_calls(
    function_calls: list[dict[str, Any]], records: Records
) -> tuple[str, list[dict[str, Any]]]:
    if records.calls is not None:
        return "mcp_recording_proxy", records.calls
    return "tool_native", [native_call(call) for call in function_calls]


def bundle_digest(root: Path) -> str:
    files = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in DIGESTED
        if (root / name).is_file()
    }
    return hashlib.sha256(canonical_text(files).encode("utf-8")).hexdigest()


def native_files(root: Path, receipt_dir: Path) -> list[dict[str, str]]:
    """List the native outputs that exist, relative to the receipt's directory."""

    found = []
    for name in NATIVE_OUTPUTS:
        path = root / name
        if not path.is_file():
            continue
        try:
            relative = path.resolve().relative_to(receipt_dir.resolve())
        except ValueError as exc:
            raise ParseError("the native outputs must lie inside the receipt's directory") from exc
        found.append(
            {"path": relative.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    return found
