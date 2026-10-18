"""Read the runtime contract that ``generate --runtime-contract`` supplies.

This repository owns ``contracts/runtime-contract/``. The orchestrator writes one
contract per target and keeps a byte-identical copy of the kit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .contract_kit import (
    contract_dir,
    first_schema_error,
    load_json_file,
    parse_document,
    verify_contract_lock,
)

SCHEMA_VERSION = "artifact-runtime-contract-v2"
_CONTRACT_ROOT = contract_dir("runtime-contract")


class RuntimeContractError(ValueError):
    """Raised when a runtime contract cannot be read or breaks its schema."""


def load_runtime_contract(path: Path) -> dict[str, Any]:
    """Return the JSON or YAML runtime contract at ``path`` once it passes its schema."""

    try:
        document = parse_document(path, path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise RuntimeContractError(f"cannot read runtime contract: {exc}") from exc
    fault = first_schema_error(_schema(), document)
    if fault is not None:
        raise RuntimeContractError(f"runtime contract schema invalid {fault}")
    return document


def _schema() -> dict[str, Any]:
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
    return load_json_file(
        _CONTRACT_ROOT / f"{SCHEMA_VERSION}.schema.json",
        RuntimeContractError,
        "cannot validate runtime contract",
    )
