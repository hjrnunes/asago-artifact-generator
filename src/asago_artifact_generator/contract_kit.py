"""Digests, canonical JSON, lock checks, and closed vocabulary shared by the contract readers."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class ClaimLevel(StrEnum):
    """The observation a scenario claim rests on, as the scenario-handoff contract names it."""

    COMMAND_ATTEMPT = "command_attempt"
    REPLY = "reply"
    RETURNED_RESULT = "returned_result"
    STATE_EFFECT = "state_effect"


CLAIM_LEVELS: tuple[str, ...] = tuple(level.value for level in ClaimLevel)


def sha256_hex(value: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of ``value``."""

    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any, *, nfc: bool = False, allow_nan: bool = True) -> str:
    """Return sorted, compact JSON; ``nfc`` first NFC-normalizes every string and key."""

    return json.dumps(
        _normalize_unicode(value) if nfc else value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=allow_nan,
    )


def framed_digest(domain: str, value: Any, *, nfc: bool = False, allow_nan: bool = True) -> str:
    """Digest ``value``'s canonical JSON framed by its domain and a NUL separator."""

    canonical = canonical_json(value, nfc=nfc, allow_nan=allow_nan)
    return sha256_hex(domain.encode("utf-8") + b"\0" + canonical.encode("utf-8"))


def first_schema_error(schema: dict[str, Any], value: Any) -> str | None:
    """Return ``at <location>: <message>`` for the first error in path order, or None."""

    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.path),
    )
    if not errors:
        return None
    location = ".".join(str(part) for part in errors[0].path) or "<root>"
    return f"at {location}: {errors[0].message}"


def verify_contract_lock(
    root: Path,
    error: type[Exception],
    *,
    lock_label: str,
    member_label: str,
    metadata: dict[str, str],
    metadata_message: str,
) -> None:
    """Check a vendored contract's lock metadata and every locked file digest.

    Each failure raises ``error`` with a message naming the contract, so each
    reader keeps its own exception type and wording.
    """

    try:
        lock = json.loads((root / "CONTRACT.lock").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise error(f"cannot read {lock_label}: {exc}") from exc
    if any(lock.get(key) != value for key, value in metadata.items()):
        raise error(metadata_message)
    for relative, expected in lock.get("files", {}).items():
        member = root / relative
        if not member.is_file() or sha256_hex(member.read_bytes()) != expected:
            raise error(f"{member_label} digest mismatch: {relative}")


def _normalize_unicode(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", str(key)): _normalize_unicode(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_unicode(item) for item in value]
    return value
