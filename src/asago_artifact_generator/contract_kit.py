"""The consumer's contract helpers: the shared digests plus its own YAML and root.

The digest, canonical-JSON, schema-error and lock helpers live in the ``asago-package``
workspace member; this module re-exports the schema and lock helpers. The canonical
encoder and framed digest come from ``asago-contracts-core``, so the consumer and the
producer share one implementation. The consumer keeps the YAML document reader and its
own ``contract_dir``, because the consumer's remaining contract kits stay at the
repository root.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from asago_contracts_core import (
    canonical_json as _canonical_json,
)
from asago_contracts_core import (
    encoding_for,
    sha256_hex,
)
from asago_contracts_core import (
    framed_digest as _framed_digest,
)

from asago_package.contract_kit import (
    CLAIM_LEVELS,
    ClaimLevel,
    first_schema_error,
    load_json_file,
    verify_contract_lock,
)

_PACKAGE_DIR = Path(__file__).resolve().parent


def canonical_json(value: Any, *, nfc: bool = False, allow_nan: bool = True) -> str:
    """Encode one value with the consumer's persisted policy by default."""

    return _canonical_json(value, encoding=encoding_for(nfc=nfc, allow_nan=allow_nan))


def framed_digest(domain: str, value: Any, *, nfc: bool = False, allow_nan: bool = True) -> str:
    """Hash one payload with the consumer's persisted policy by default."""

    return _framed_digest(domain, value, encoding=encoding_for(nfc=nfc, allow_nan=allow_nan))


def contract_dir(name: str) -> Path:
    """Return the vendored contract ``name``: the wheel's packaged copy, else the checkout's.

    The wheel force-includes the repository's ``contracts/`` inside the package; a source
    checkout, editable install included, reads ``contracts/`` at the repository root.
    """

    packaged = _PACKAGE_DIR / "contracts"
    root = packaged if packaged.is_dir() else _PACKAGE_DIR.parents[1] / "contracts"
    return root / name


def parse_document(
    path: Path, content: bytes | str, *, parse_constant: Callable[[str], Any] | None = None
) -> Any:
    """Parse ``content`` as JSON when ``path`` ends in .json, otherwise as YAML.

    Callers translate the parser's exceptions into their own error types.
    """

    if path.suffix.lower() == ".json":
        return json.loads(content, parse_constant=parse_constant)
    return yaml.safe_load(content)


__all__ = [
    "CLAIM_LEVELS",
    "ClaimLevel",
    "canonical_json",
    "contract_dir",
    "first_schema_error",
    "framed_digest",
    "load_json_file",
    "parse_document",
    "sha256_hex",
    "verify_contract_lock",
]
