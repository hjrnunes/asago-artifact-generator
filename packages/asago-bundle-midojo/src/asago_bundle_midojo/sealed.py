"""parse: turn one episode's sealed snapshot and its verdict into ``execution-receipt-v1``.

The runner opens the episode ``compile`` wrote (``midojo.open_episode``), seals it, and
grades the sealed snapshot with ``midojo.grade``, MiDojo's pure grade function. It leaves
two files: the snapshot as MiDojo's ``GET .../snapshot`` returns it (``SnapshotResponse``:
``evaluation_id``, ``snapshot_id``, the turns, the call records, the environments, the
``evidence_status`` fixed at the seal) and the verdict (``Verdict``: ``evaluation_id``,
``snapshot_id`` and ``grade``, a ``GradeResponse``). This module reads them; it grades
nothing.

MiDojo's ``security`` grade is three-valued, and so is the receipt: True is
``detected``, False ``not_detected``, and None, a check that could not be decided from the
evidence (``grade_validity`` ``invalid``), ``inconclusive``. An unknown grade is never made
a pass or a fail. The receipt's ``episode`` block names the route that drove the run
(``route.py``), the evaluation and the snapshot graded, the snapshot's evidence status
and the verdict as MiDojo gave it.

The snapshot must be the one MiDojo sealed: its ``snapshot_id`` is MiDojo's digest of its
content, recomputed here (``snapshot_digest.py``), so a snapshot whose content changed
under its ID is refused. The verdict must name the snapshot's evaluation and the snapshot
itself, its validity must agree with its grade, and its evidence status must be exactly
the snapshot's (``grading.grade`` keeps the snapshot's). The snapshot's turns must be the
episode's, in order, and each call must name one of them and be the host interceptor's
record (provenance ``interceptor``): the episode intercepts on the host, so a call the
agent's side recorded means the target held a session it never should. Anything else yields a
``failed`` receipt whose ``incomplete_reason`` names what was wrong, and the receipt is
still written. A turn the agent did not answer (no reply, or the conversation ends early)
fails the receipt with ``turn_failed:<position>`` (counted from 1); its attempt is kept,
with MiDojo's verdict unchanged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asago_bundle_core.errors import BundleError
from asago_bundle_core.mcp_records import decoded, read_json, text
from asago_bundle_core.text import canonical_text

from .episode_compile import COMMAND_ATTEMPT, EPISODE_FILE, REPLY, SCHEMA_VERSION, TOOL
from .route import DEFAULT_ROUTE, ROUTES
from .snapshot_digest import CONTENT_FIELDS, snapshot_digest

RECEIPT_SCHEMA_VERSION = "execution-receipt-v1"
DETECTORS = {COMMAND_ATTEMPT: "ToolCallCondition", REPLY: "RubricJudge"}
OUTCOMES = {True: "detected", False: "not_detected", None: "inconclusive"}
JUDGE_VERDICTS = {True: "supported", False: "contradicted", None: "unresolved"}
EVIDENCE_STATUSES = frozenset({"complete", "incomplete", "failed", "not_collected"})
VALIDITIES = frozenset({"valid", "invalid", "not_applicable"})
UNFINISHED = "unfinished"
SNAPSHOT_TEXT = ("evaluation_id", "snapshot_id", "agent_output", "sealed_at")
SNAPSHOT_MAPPINGS = ("pre_environment", "environment", "observations", "evidence_status")
SNAPSHOT_LISTS = ("function_calls", "turns", "capture_failures")
SNAPSHOT_OPTIONAL_TEXT = ("run_id", "user_task_id", "injection_task_id")
SNAPSHOT_FIELDS = frozenset({*CONTENT_FIELDS, "snapshot_id"})
CALL_FIELDS = frozenset(
    {
        "call_id",
        "function",
        "args",
        "result",
        "error",
        "blocked",
        "disposition",
        "complete",
        "provenance",
        "timestamp",
        "turn",
        "pre_environment",
        "post_environment",
    }
)
DISPOSITIONS = frozenset({"forwarded", "denied", "simulated", "blocked", None})
INTERCEPTOR = "interceptor"
PROVENANCES = frozenset({"host", INTERCEPTOR, "agent", None})
TURN_FIELDS = frozenset({"index", "message", "reply", "started_at", "ended_at"})
FAILURE_FIELDS = frozenset({"call_id", "function", "phase", "reason", "stage", "by", "at"})
CAPTURE_PHASES = frozenset({"start", "finish"})
VERDICT_FIELDS = frozenset({"evaluation_id", "snapshot_id", "grade"})
GRADED = ("utility", "security")
GRADE_FIELDS = frozenset(
    {*GRADED, "utility_reason", "security_reason", "grade_validity", "evidence_status"}
)
EPISODE_KEYS = frozenset({"package_id", "scenario_id", "package_digest", "claim_level", "episode"})


class ParseError(BundleError):
    """The episode cannot be read, so no receipt can name its package."""


@dataclass(frozen=True)
class Compiled:
    """What ``compile`` wrote: the directory and its ``episode.json``."""

    root: Path
    document: dict[str, Any]

    @property
    def claim_level(self) -> str:
        return self.document["claim_level"]

    @property
    def turns(self) -> list[str]:
        return self.document["episode"]["turns"]


def parse_sealed(
    episode_dir: str | Path,
    snapshot_path: str | Path,
    verdict_path: str | Path,
    out_path: str | Path,
    *,
    tool_revision: str | None = None,
    adapter_revision: str | None = None,
    route: str = DEFAULT_ROUTE,
) -> dict[str, Any]:
    """Write the receipt of one sealed episode to ``out_path`` and return it.

    ``route`` names the client that drove this run of the episode (one of ``ROUTES``).
    """

    if route not in ROUTES:
        raise ParseError(f"unknown route {route!r}; the routes are {list(ROUTES)}")
    compiled = read_compiled(Path(episode_dir))
    out = Path(out_path)
    snapshot, reason = read_document(Path(snapshot_path), "snapshot", snapshot_shape)
    verdict, verdict_reason = read_document(Path(verdict_path), "verdict", verdict_shape)
    reason = reason or verdict_reason
    attempt = None
    if reason is None:
        reason = sealed_failure(compiled, snapshot, verdict)  # type: ignore[arg-type]
    if reason is None or reason.startswith(TURN_FAILED):
        attempt = attempt_of(compiled, snapshot, verdict)  # type: ignore[arg-type]
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "tool": {"id": TOOL, "revision": tool_revision, "adapter_revision": adapter_revision},
        "package": {
            "id": compiled.document["scenario_id"],
            "digest": compiled.document["package_digest"],
        },
        "bundle_digest": compiled_digest(compiled.root),
        "execution_status": "failed" if reason else "completed",
        "runtime_status": runtime_status(reason),
        "incomplete_reason": reason,
        "attempts": [attempt] if attempt else [],
        "native": native_files((Path(snapshot_path), Path(verdict_path)), out.parent),
        "episode": episode_block(route, snapshot, verdict),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(canonical_text(receipt), encoding="utf-8")
    return receipt


def read_compiled(root: Path) -> Compiled:
    document = read_json(root / EPISODE_FILE)
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise ParseError(f"{root / EPISODE_FILE} is not a {SCHEMA_VERSION} document")
    missing = EPISODE_KEYS - document.keys()
    if missing:
        raise ParseError(f"the episode document lacks {sorted(missing)}")
    if document["claim_level"] not in DETECTORS:
        raise ParseError(f"the episode's claim level is not graded: {document['claim_level']!r}")
    return Compiled(root, document)


def read_document(path: Path, prefix: str, shape: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, f"{prefix}_missing"
    document = read_json(path)
    if not (isinstance(document, dict) and shape(document)):
        return None, f"{prefix}_malformed"
    return document, None


def snapshot_shape(snapshot: dict[str, Any]) -> bool:
    """The snapshot is MiDojo's ``SnapshotResponse``: exactly its fields, each of its type.

    A call record is ``FunctionCallRecord``, a turn ``TurnResponse`` and a capture failure
    ``CaptureFailureResponse`` (``control_plane/models.py``, ``types.py``), field for field.
    """

    return (
        set(snapshot) == SNAPSHOT_FIELDS
        and all(isinstance(snapshot[key], str) for key in SNAPSHOT_TEXT)
        and all(_optional_text(snapshot[key]) for key in SNAPSHOT_OPTIONAL_TEXT)
        and all(isinstance(snapshot[key], dict) for key in SNAPSHOT_MAPPINGS)
        and all(isinstance(snapshot[key], list) for key in SNAPSHOT_LISTS)
        and all(isinstance(observed, dict) for observed in snapshot["observations"].values())
        and evidence_shape(snapshot["evidence_status"])
        and all(_call_shape(call) for call in snapshot["function_calls"])
        and all(_turn_shape(turn) for turn in snapshot["turns"])
        and all(_failure_shape(failure) for failure in snapshot["capture_failures"])
    )


def _optional_text(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _call_shape(call: Any) -> bool:
    return (
        isinstance(call, dict)
        and set(call) == CALL_FIELDS
        and all(isinstance(call[key], str) for key in ("call_id", "function", "timestamp"))
        and isinstance(call["args"], dict)
        and _optional_text(call["result"])
        and _optional_text(call["error"])
        and all(type(call[key]) is bool for key in ("blocked", "complete"))
        and call["disposition"] in DISPOSITIONS
        and call["provenance"] in PROVENANCES
        and (call["turn"] is None or type(call["turn"]) is int)
        and all(
            call[key] is None or isinstance(call[key], dict)
            for key in ("pre_environment", "post_environment")
        )
    )


def _turn_shape(turn: Any) -> bool:
    return (
        isinstance(turn, dict)
        and set(turn) == TURN_FIELDS
        and type(turn["index"]) is int
        and isinstance(turn["message"], str)
        and _optional_text(turn["reply"])
        and isinstance(turn["started_at"], str)
        and _optional_text(turn["ended_at"])
    )


def _failure_shape(failure: Any) -> bool:
    return (
        isinstance(failure, dict)
        and set(failure) == FAILURE_FIELDS
        and all(isinstance(failure[key], str) for key in FAILURE_FIELDS)
        and failure["phase"] in CAPTURE_PHASES
    )


def evidence_shape(status: Any) -> bool:
    return isinstance(status, dict) and all(
        isinstance(key, str) and value in EVIDENCE_STATUSES for key, value in status.items()
    )


def verdict_shape(verdict: dict[str, Any]) -> bool:
    """The verdict is ``Verdict``, its grade MiDojo's ``GradeResponse``, field for field, and
    the security grade agrees with its validity: unknown exactly when ``invalid``."""

    grade = verdict.get("grade")
    if not (
        set(verdict) == VERDICT_FIELDS
        and isinstance(verdict["evaluation_id"], str)
        and isinstance(verdict["snapshot_id"], str)
        and isinstance(grade, dict)
        and set(grade) == GRADE_FIELDS
        and evidence_shape(grade["evidence_status"])
        and all(grade[key] is None or isinstance(grade[key], bool) for key in GRADED)
        and all(_optional_text(grade[f"{key}_reason"]) for key in GRADED)
        and _validity_shape(grade["grade_validity"])
    ):
        return False
    status = grade["grade_validity"]["security"]["status"]
    return status != "not_applicable" and (status == "invalid") == (grade["security"] is None)


def _validity_shape(validity: Any) -> bool:
    """``GradeValidity``: a ``CheckValidity`` per grade, each reason a ``ValidityReason``."""

    return (
        isinstance(validity, dict)
        and set(validity) == set(GRADED)
        and all(
            isinstance(check, dict)
            and set(check) == {"status", "reasons"}
            and check["status"] in VALIDITIES
            and isinstance(check["reasons"], list)
            and all(
                isinstance(reason, dict)
                and set(reason) == {"check", "reason"}
                and all(isinstance(text, str) for text in reason.values())
                for reason in check["reasons"]
            )
            for check in validity.values()
        )
    )


TURN_FAILED = "turn_failed:"


def sealed_failure(
    compiled: Compiled, snapshot: dict[str, Any], verdict: dict[str, Any]
) -> str | None:
    """Return the first reason the sealed result cannot be graded, or None."""

    if snapshot_digest(snapshot) != snapshot["snapshot_id"]:
        return "snapshot_digest_mismatch"
    if verdict["evaluation_id"] != snapshot["evaluation_id"]:
        return "evaluation_mismatch"
    if verdict["snapshot_id"] != snapshot["snapshot_id"]:
        return "snapshot_mismatch"
    if verdict["grade"]["evidence_status"] != snapshot["evidence_status"]:
        return "evidence_status_mismatch"
    sent = [turn["message"] for turn in snapshot["turns"]]
    if [turn["index"] for turn in snapshot["turns"]] != list(range(len(sent))):
        return "turns_malformed"
    if len(sent) > len(compiled.turns) or sent != compiled.turns[: len(sent)]:
        return "turn_input_mismatch"
    for call in snapshot["function_calls"]:
        turn = call.get("turn")
        if turn is None:
            return "call_turn_missing"
        if type(turn) is not int or not 0 <= turn < len(sent):
            return "call_turn_out_of_range"
        if call.get("provenance") != INTERCEPTOR:
            return "call_not_intercepted"
    return turn_failure(snapshot["turns"], len(compiled.turns))


def turn_failure(turns: list[dict[str, Any]], planned: int) -> str | None:
    """Return ``turn_failed:<position>`` for the first turn without a reply, else None.

    An empty reply is no reply, as Garak's ``EmptyReply``.
    """

    for turn in turns:
        if not text(turn.get("reply")):
            return f"{TURN_FAILED}{turn['index'] + 1}"
    if len(turns) < planned:
        return f"{TURN_FAILED}{len(turns) + 1}"
    return None


def runtime_status(reason: str | None) -> str | None:
    if reason is None:
        return "completed"
    return "failed" if reason.startswith(TURN_FAILED) else None


def attempt_of(
    compiled: Compiled, snapshot: dict[str, Any], verdict: dict[str, Any]
) -> dict[str, Any]:
    """Return the attempt: the turns sent, the calls recorded, and MiDojo's verdict."""

    grade = verdict["grade"]
    security = grade["security"]
    level = compiled.claim_level
    calls = [native_call(call) for call in snapshot["function_calls"]]
    replies = {turn["index"]: turn["reply"] for turn in snapshot["turns"] if text(turn["reply"])}
    result = {
        "outcome": OUTCOMES[security],
        "claim_level": level,
        "reason": grade["security_reason"],
    }
    return {
        "index": 0,
        "generation": {
            "request_count": None,
            "response_status": "completed" if len(replies) == len(compiled.turns) else None,
            "error_type": None,
        },
        "turns": [
            {
                "index": turn["index"],
                "request": {"messages": [{"role": "user", "content": turn["message"]}]},
                "response": {
                    "assistant_messages": [replies[turn["index"]]]
                    if turn["index"] in replies
                    else [],
                    "tool_call_indices": [
                        position
                        for position, call in enumerate(calls)
                        if call["turn_index"] == turn["index"]
                    ],
                },
            }
            for turn in snapshot["turns"]
        ],
        "result": result,
        "observation": {
            "source": "tool_native",
            "assistant_messages": [replies[index] for index in sorted(replies)],
            "tool_calls": calls,
        },
        "runtime_observations": {
            "source": None,
            **{
                name: []
                for name in (
                    "processes",
                    "network_calls_allowed",
                    "network_calls_blocked",
                    "files_created",
                    "files_modified",
                    "files_deleted",
                    "security_findings",
                )
            },
        },
        "detections": [
            {
                "detector": DETECTORS[level],
                "outcome": result["outcome"],
                "reason": result["reason"],
                "matched_calls": None,
            }
        ],
        "judge": judge_of(level, grade),
    }


def native_call(call: dict[str, Any]) -> dict[str, Any]:
    """Return a snapshot call record in the receipt's shape, in the turn MiDojo stamped.

    A call MiDojo holds as started and not finished (``complete: false``) has no result and
    the status ``unfinished``.
    """

    unfinished = call.get("complete") is False
    return {
        "name": call["function"],
        "arguments": call["args"],
        "result": None if unfinished else decoded(call.get("result")),
        "error": text(call.get("error")),
        "status": UNFINISHED if unfinished else None,
        "turn_index": call["turn"],
    }


def judge_of(level: str, grade: dict[str, Any]) -> dict[str, Any] | None:
    """Return a reply's judge record: the verdict the grade stands for, and MiDojo's reason.

    The grade carries no request count.
    """

    if level != REPLY:
        return None
    return {
        "verdict": JUDGE_VERDICTS[grade["security"]],
        "reason": grade["security_reason"],
        "request_count": None,
    }


def episode_block(
    route: str, snapshot: dict[str, Any] | None, verdict: dict[str, Any] | None
) -> dict[str, Any]:
    """Return what the receipt names of the episode's run; a file that cannot be read is null."""

    grade = verdict["grade"] if verdict else None
    return {
        "route": route,
        "evaluation_id": snapshot["evaluation_id"] if snapshot else None,
        "snapshot_id": snapshot["snapshot_id"] if snapshot else None,
        "evidence_status": snapshot["evidence_status"] if snapshot else None,
        "verdict": None
        if grade is None
        else {
            "evaluation_id": verdict["evaluation_id"],  # type: ignore[index]
            "snapshot_id": verdict["snapshot_id"],  # type: ignore[index]
            "security": grade["security"],
            "reason": grade["security_reason"],
            "validity": grade["grade_validity"]["security"]["status"],
        },
    }


def compiled_digest(root: Path) -> str:
    """Return the SHA-256 of what ``compile`` wrote: each file's digest, by its path."""

    found = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }
    return hashlib.sha256(canonical_text(found).encode("utf-8")).hexdigest()


def native_files(paths: tuple[Path, ...], receipt_dir: Path) -> list[dict[str, str]]:
    """List the snapshot and verdict files that exist, relative to the receipt's directory."""

    found = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            relative = path.resolve().relative_to(receipt_dir.resolve())
        except ValueError as exc:
            raise ParseError(
                "the snapshot and verdict must lie inside the receipt's directory"
            ) from exc
        found.append(
            {"path": relative.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
    return found
