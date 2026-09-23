"""Durable shared authoring budget and per-call timing transport."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring import (
    AuthoringBudget,
    PromptPacket,
    TransportResponse,
)

from ._storage import _utc_now, _write_json_atomic

_BUDGET_LIMITS = {
    "aggregate_limit": 40,
    "task_limit": 8,
    "author_limit": 4,
    "review_limit": 4,
}

_BUDGET_SCHEMA_VERSION = "fresh-five-case-authoring-budget-v1"


class PersistedAuthoringBudget(AuthoringBudget):
    """The normal budget guard with a durable reservation ledger."""

    def __init__(
        self,
        path: str | Path,
        *,
        total_dispatched: int = 0,
        dispatched_by_task: dict[str, int] | None = None,
        dispatched_by_task_role: dict[str, dict[str, int]] | None = None,
        reservations: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            **_BUDGET_LIMITS,
            total_dispatched=total_dispatched,
            dispatched_by_task=dict(dispatched_by_task or {}),
            dispatched_by_task_role={
                task_id: dict(roles) for task_id, roles in (dispatched_by_task_role or {}).items()
            },
        )
        self.path = Path(path)
        self.reservations = [dict(item) for item in (reservations or [])]

    @classmethod
    def load(cls, path: str | Path) -> PersistedAuthoringBudget:
        """Load existing counters or create the one fixed 40/8/4/4 ledger."""

        ledger_path = Path(path)
        if not ledger_path.exists():
            budget = cls(ledger_path)
            budget._persist()
            return budget
        try:
            saved = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid authoring budget ledger: {ledger_path}") from exc
        if not isinstance(saved, dict):
            raise ValueError("authoring budget ledger must be an object")
        if saved.get("schema_version") != _BUDGET_SCHEMA_VERSION:
            raise ValueError("unknown authoring budget ledger schema version")
        if saved.get("limits") != _BUDGET_LIMITS:
            raise ValueError("authoring budget limits differ from the frozen 40/8/4/4 limits")
        reservations = saved.get("reservations")
        if not isinstance(reservations, list) or any(
            not isinstance(item, dict) for item in reservations
        ):
            raise ValueError("authoring budget reservations must be a list of objects")
        for reservation in reservations:
            if (
                not isinstance(reservation.get("task_id"), str)
                or not reservation["task_id"].strip()
                or reservation.get("role") not in {"author", "reviewer"}
                or reservation.get("status") not in {"reserved", "completed", "failed"}
            ):
                raise ValueError("authoring budget contains an invalid reservation")
        budget = cls(
            ledger_path,
            total_dispatched=saved.get("total_dispatched", 0),
            dispatched_by_task=saved.get("dispatched_by_task", {}),
            dispatched_by_task_role=saved.get("dispatched_by_task_role", {}),
            reservations=reservations,
        )
        task_counts: dict[str, int] = {}
        role_counts: dict[str, dict[str, int]] = {}
        for reservation in reservations:
            task_id = reservation["task_id"]
            role = reservation["role"]
            task_counts[task_id] = task_counts.get(task_id, 0) + 1
            roles = role_counts.setdefault(task_id, {})
            roles[role] = roles.get(role, 0) + 1
        if (
            budget.total_dispatched != len(reservations)
            or budget.dispatched_by_task != task_counts
            or budget.dispatched_by_task_role != role_counts
        ):
            raise ValueError("authoring budget counters do not match durable reservations")
        return budget

    def reserve(self, task_id: str, *, role: str = "author") -> int:
        """Persist the consumed slot before the orchestrator contacts transport."""

        dispatch_index = super().reserve(task_id, role=role)
        reservation = {
            "dispatch_index": dispatch_index,
            "task_id": task_id,
            "role": role,
            "reserved_at": _utc_now(),
            "status": "reserved",
            "outcome": None,
        }
        self.reservations.append(reservation)
        self._persist()
        return dispatch_index

    def record_outcome(
        self,
        task_id: str,
        *,
        stage: str,
        outcome: str,
        started_at: str,
        ended_at: str,
        elapsed_ms: float,
    ) -> None:
        """Flush the latest reserved call outcome after transport returns."""

        reservation = next(
            (
                item
                for item in reversed(self.reservations)
                if item.get("task_id") == task_id and item.get("status") == "reserved"
            ),
            None,
        )
        if reservation is None:
            raise ValueError(f"no outstanding authoring reservation for {task_id}")
        reservation.update(
            {
                "stage": stage,
                "status": "completed" if outcome == "response_returned" else "failed",
                "outcome": outcome,
                "started_at": started_at,
                "ended_at": ended_at,
                "elapsed_ms": elapsed_ms,
            }
        )
        self._persist()

    def has_reservation(self, task_id: str) -> bool:
        """Return whether this case has any durable request reservation."""

        return self.dispatched_by_task.get(task_id, 0) > 0 or any(
            item.get("task_id") == task_id for item in self.reservations
        )

    def _persist(self) -> None:
        _write_json_atomic(
            self.path,
            {
                "schema_version": _BUDGET_SCHEMA_VERSION,
                "limits": dict(_BUDGET_LIMITS),
                "total_dispatched": self.total_dispatched,
                "dispatched_by_task": dict(self.dispatched_by_task),
                "dispatched_by_task_role": {
                    task_id: dict(roles) for task_id, roles in self.dispatched_by_task_role.items()
                },
                "reservations": self.reservations,
            },
        )


class TimedAuthoringTransport:
    """Record exact-call timing while forwarding each packet unchanged."""

    max_retries = 0

    def __init__(
        self,
        transport: Any,
        *,
        timings_path: str | Path,
        budget: PersistedAuthoringBudget,
        task_id: str,
    ) -> None:
        self.transport = transport
        self.timings_path = Path(timings_path)
        self.budget = budget
        self.task_id = task_id
        self.model = getattr(transport, "model", None)
        self.profile_name = getattr(transport, "profile_name", None)

    def preflight_context_budget(self, packet: PromptPacket) -> dict[str, Any] | None:
        """Forward a configured context preflight without reserving a dispatch."""

        preflight = getattr(self.transport, "preflight_context_budget", None)
        if not callable(preflight):
            return None
        return preflight(packet)

    def complete(self, packet: PromptPacket) -> TransportResponse | str | bytes:
        """Forward the same packet object and append one flushed timing record."""

        started_clock = time.perf_counter()
        started_at = _utc_now()
        outcome = "transport_error"
        try:
            response = self.transport.complete(packet)
            outcome = "response_returned"
            return response
        finally:
            ended_at = _utc_now()
            elapsed_ms = round((time.perf_counter() - started_clock) * 1000, 3)
            record = {
                "task_id": self.task_id,
                "stage": packet.stage,
                "prompt_version": packet.version,
                "prompt_sha256": packet.sha256,
                "start": started_at,
                "end": ended_at,
                "elapsed_ms": elapsed_ms,
                "outcome": outcome,
            }
            self._append_timing(record)
            self.budget.record_outcome(
                self.task_id,
                stage=packet.stage,
                outcome=outcome,
                started_at=started_at,
                ended_at=ended_at,
                elapsed_ms=elapsed_ms,
            )

    def _append_timing(self, record: dict[str, Any]) -> None:
        self.timings_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        with self.timings_path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
