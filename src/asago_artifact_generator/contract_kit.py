"""Digests, canonical JSON, and lock checks shared by the vendored-contract readers."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any


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
