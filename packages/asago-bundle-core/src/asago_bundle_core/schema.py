"""The ``tool-bundle`` schemas: what every adapter's ``bundle.json`` must satisfy.

``schema.json`` of each installed version is the schema, and ``manifest_errors`` reads a
manifest through ``asago_bundle_core.bundle_contract`` before it applies the schema, so a
reader refuses what the contract's spec and cases say it must. ``validate_manifest`` stays a
pure jsonschema check of the current version's payload.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

import jsonschema
from asago_contracts_core import ContractEnvelopeError

from asago_bundle_core.bundle_contract import LEGACY_VERSION, VERSION, read_bundle

SCHEMA_VERSION = VERSION
LEGACY_SCHEMA_VERSION = LEGACY_VERSION
SCHEMA_VERSIONS = (LEGACY_SCHEMA_VERSION, SCHEMA_VERSION)
SCHEMA_DIR = "contracts/tool-bundle"
SCHEMA_FILE = f"{SCHEMA_DIR}/{SCHEMA_VERSION}/schema.json"


@cache
def _schema_text(version: str) -> str:
    path = f"{SCHEMA_DIR}/{version}/schema.json"
    return files("asago_bundle_core").joinpath(path).read_text(encoding="utf-8")


def load_schema(version: str = SCHEMA_VERSION) -> dict[str, Any]:
    """Return a fresh copy of ``version``'s schema document, the current one by default."""

    if version not in SCHEMA_VERSIONS:
        raise ValueError(f"no tool-bundle schema for {version}: expected one of {SCHEMA_VERSIONS}")
    return json.loads(_schema_text(version))


def manifest_errors(manifest: Any) -> list[str]:
    """Return the reason the reader refuses ``manifest``; empty when it accepts it.

    ``read_bundle`` selects the installed spec the document names, so a wrong version, a
    missing v2 envelope, an unknown top-level field, an undeclared ``requires`` entry or a
    mismatched ``spec_digest`` is refused here before the schema runs. The schema still
    decides every other field.
    """

    try:
        read_bundle(manifest)
    except ContractEnvelopeError as error:
        return [str(error)]
    return []


def validate_manifest(manifest: Any) -> None:
    """Raise ``jsonschema.ValidationError`` when ``manifest`` breaks the current schema.

    This is the pure schema check of a payload: it does not read the envelope and does not
    select a version. Use ``manifest_errors`` to read a document a writer produced.
    """

    jsonschema.Draft202012Validator(load_schema()).validate(manifest)
