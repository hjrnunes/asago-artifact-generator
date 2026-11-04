"""Read the runtime contract that ``generate --runtime-contract`` supplies.

The ``asago-package`` member owns the runtime-contract kit and its installed reader. The
orchestrator writes one contract per target, and this loader takes every document through
that reader before the hand-written check that words a schema fault for the CLI:

``runtime contract schema invalid at <path>: <reason>``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from asago_contracts_core import (
    ENVELOPE_FIELD,
    ContractEnvelopeError,
    InvalidDocumentError,
    UnknownFieldError,
)

from asago_package.contract_kit import contract_dir, first_schema_error, load_json_file
from asago_package.runtime_contract_spec import VERSION, read_runtime_contract

from .contract_kit import parse_document, verify_contract_lock

SCHEMA_VERSION = VERSION
_CONTRACT_ROOT = contract_dir("runtime-contract")


class RuntimeContractError(ValueError):
    """Raised when a runtime contract cannot be read or breaks its schema."""


def load_runtime_contract(path: Path) -> dict[str, Any]:
    """Return the JSON or YAML runtime contract at ``path`` once it passes its schema."""

    try:
        document = parse_document(path, path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise RuntimeContractError(f"cannot read runtime contract: {exc}") from exc
    _verify_lock()
    try:
        read_runtime_contract(document)
    except (InvalidDocumentError, UnknownFieldError):
        # The hand check words the same schema fault the way the CLI always has. An
        # unknown top-level field is a schema fault here, but the reader names the
        # declared extensions instead, so both faults take this path.
        fault = first_schema_error(_schema(), _payload(document))
        if fault is None:
            raise
        raise RuntimeContractError(f"runtime contract schema invalid {fault}") from None
    except ContractEnvelopeError as exc:
        # The envelope's own refusals: an unsupported version, an unknown `requires`
        # entry or a mismatched `spec_digest`. Each names the rule it broke.
        raise RuntimeContractError(f"runtime contract schema invalid {exc}") from None
    return document


def _payload(document: Any) -> Any:
    """Return the document without its envelope, which the schema does not declare."""

    if not isinstance(document, Mapping) or not isinstance(document.get(ENVELOPE_FIELD), Mapping):
        return document
    return {key: value for key, value in document.items() if key != ENVELOPE_FIELD}


def _verify_lock() -> None:
    """Refuse a kit whose files differ from its lock, before any document is read."""

    verify_contract_lock(
        _CONTRACT_ROOT,
        RuntimeContractError,
        lock_label="runtime-contract contract lock",
        member_label="runtime-contract contract",
        metadata={
            "authority": "asago-artifact-generator",
            "contract": "runtime-contract",
            "schema_version": SCHEMA_VERSION,
        },
        metadata_message="runtime-contract contract lock metadata is invalid",
    )


def _schema() -> dict[str, Any]:
    return load_json_file(
        _CONTRACT_ROOT / SCHEMA_VERSION / "schema.json",
        RuntimeContractError,
        "cannot validate runtime contract",
    )
