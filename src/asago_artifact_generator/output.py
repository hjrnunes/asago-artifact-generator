"""Atomic persistence for STPA readiness, plans, artifacts and manifests."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models.execution_case import BoundExecutionCase, ExecutionCaseExclusion
from .models.readiness import ExecutionPlanResult, ReadyExecutionPlan
from .platforms.base import CompiledArtifact


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> Path:
    """Write one JSON sidecar atomically in the destination directory."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def readiness_document(result: ExecutionPlanResult) -> dict[str, Any]:
    """Serialize a typed ``ExecutionPlanResult`` without changing it."""

    return result.model_dump(mode="json")


def plan_document(plan: ReadyExecutionPlan) -> dict[str, Any]:
    """Serialize the one compiler-facing ready plan for human inspection."""

    if not isinstance(plan, ReadyExecutionPlan):
        raise TypeError("execution-plan output requires a ReadyExecutionPlan")
    return plan.model_dump(mode="json")


def compiled_documents(
    compiled: CompiledArtifact,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return artifact, trace and validation sidecars in write order."""

    return dict(compiled.artifact), dict(compiled.trace), dict(compiled.validation)


def write_entry_outputs(
    run_dir: Path,
    scenario_id: str,
    readiness: ExecutionPlanResult,
    *,
    execution_case: BoundExecutionCase | None = None,
    plan: ReadyExecutionPlan | None = None,
    compiled: CompiledArtifact | None = None,
) -> dict[str, str]:
    """Write one entry's sidecars in the contract-prescribed order."""

    entry_dir = Path(run_dir) / scenario_id
    paths: dict[str, str] = {}
    if execution_case is not None:
        execution_case.assert_integrity()
        paths["execution_case"] = str(
            atomic_write_json(
                entry_dir / "bound-execution-case.json",
                execution_case.model_dump(mode="json"),
            )
        )
    paths["readiness"] = str(
        atomic_write_json(entry_dir / "readiness.json", readiness_document(readiness))
    )
    if plan is None:
        return paths
    paths["plan"] = str(atomic_write_json(entry_dir / "execution-plan.json", plan_document(plan)))
    if compiled is None:
        return paths
    artifact, trace, validation = compiled_documents(compiled)
    paths["artifact"] = str(
        atomic_write_json(entry_dir / "executable-conversation.json", artifact)
    )
    paths["validation"] = str(atomic_write_json(entry_dir / "validation.json", validation))
    paths["trace"] = str(atomic_write_json(entry_dir / "artifact-trace.json", trace))
    return paths


def write_case_exclusion(
    run_dir: Path, scenario_id: str, exclusion: ExecutionCaseExclusion
) -> dict[str, str]:
    """Persist a semantic case exclusion without creating runtime output."""

    entry_dir = Path(run_dir) / scenario_id
    return {
        "execution_case_exclusion": str(
            atomic_write_json(
                entry_dir / "execution-case-exclusion.json",
                exclusion.model_dump(mode="json"),
            )
        )
    }


def write_manifest(run_dir: Path, manifest: Mapping[str, Any]) -> Path:
    """Atomically publish the batch manifest last."""

    return atomic_write_json(Path(run_dir) / "artifact-manifest.json", manifest)


__all__ = [
    "atomic_write_json",
    "compiled_documents",
    "plan_document",
    "readiness_document",
    "write_entry_outputs",
    "write_case_exclusion",
    "write_manifest",
]
