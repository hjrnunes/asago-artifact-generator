"""Atomic persistence for unsuccessful target-free authoring attempts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

FAILURE_EVIDENCE_SCHEMA_VERSION = "authoring-failure-evidence-v1"
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "endpoint",
    "password",
    "secret",
    "token",
}
_URL_KEYS = {"base_url", "url", "uri"}


def failure_evidence_path(destination: str | Path) -> Path:
    """Return the sibling sidecar path for a package destination."""

    destination = Path(destination)
    return destination.with_name(f"{destination.name}.failure-evidence.json")


def new_failure_evidence(task_id: str, destination: str | Path) -> dict[str, Any]:
    """Create the durable document written before the first provider call."""

    return {
        "schema_version": FAILURE_EVIDENCE_SCHEMA_VERSION,
        "status": "in_progress",
        "task_id": task_id,
        "package_path": str(destination),
        "attempts": [],
        "findings": [],
    }


def write_failure_evidence(path: str | Path, document: dict[str, Any]) -> Path:
    """Atomically replace a failure sidecar and flush its bytes before return."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def load_failure_evidence(path: str | Path) -> dict[str, Any]:
    """Reload and minimally validate a completed or interrupted sidecar."""

    path = Path(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid authoring failure evidence: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("authoring failure evidence must be an object")
    if document.get("schema_version") != FAILURE_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("unknown authoring failure evidence schema version")
    if not isinstance(document.get("attempts"), list):
        raise ValueError("authoring failure evidence attempts must be a list")
    return document


def raw_response_record(raw: bytes, *, reason: str | None = None) -> dict[str, Any]:
    """Represent raw bytes exactly while keeping them out of text scans."""

    if reason is not None:
        return {"availability": "unavailable", "reason": reason}
    return {
        "availability": "available",
        "base64": base64.b64encode(raw).decode("ascii"),
        "byte_length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def metadata_record(value: Any, *, unavailable_reason: str) -> dict[str, Any]:
    """Represent provider metadata with an explicit absence state."""

    if value is None:
        return {"availability": "unavailable", "reason": unavailable_reason}
    return {"availability": "available", "value": redact_metadata(value)}


def redact_metadata(value: Any) -> Any:
    """Remove secret and endpoint values from persisted provider metadata."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if lowered in _SENSITIVE_KEYS or lowered in _URL_KEYS:
                result[key_text] = "<redacted>"
            else:
                result[key_text] = redact_metadata(item)
        return result
    if isinstance(value, list):
        return [redact_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [redact_metadata(item) for item in value]
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return "<redacted>"
    return value


__all__ = [
    "FAILURE_EVIDENCE_SCHEMA_VERSION",
    "failure_evidence_path",
    "load_failure_evidence",
    "metadata_record",
    "new_failure_evidence",
    "raw_response_record",
    "redact_metadata",
    "write_failure_evidence",
]
