"""The ``tool-bundle-v1`` schema: what every adapter's ``bundle.json`` must satisfy.

The schema is the ``schema.json`` of the installed ``tool-bundle-v1`` contract, and
``manifest_errors`` reads a manifest through ``asago_bundle_core.bundle_contract`` before it
applies the schema, so a reader refuses what the contract's spec and cases say it must.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import Any

import jsonschema
from asago_contracts_core import ContractEnvelopeError

from asago_bundle_core.bundle_contract import read_bundle

SCHEMA_VERSION = "tool-bundle-v1"
SCHEMA_FILE = "contracts/tool-bundle/tool-bundle-v1/schema.json"


@cache
def _schema_text() -> str:
    return files("asago_bundle_core").joinpath(SCHEMA_FILE).read_text(encoding="utf-8")


def load_schema() -> dict[str, Any]:
    """Return a fresh copy of the schema document."""

    return json.loads(_schema_text())


def manifest_errors(manifest: Any) -> list[str]:
    """Return the reason the reader refuses ``manifest``; empty when it accepts it.

    ``read_bundle`` selects the installed spec the document names, so a wrong version, an
    unknown top-level field, an undeclared ``requires`` entry or a mismatched ``spec_digest``
    is refused here before the schema runs. The schema still decides every other field.
    """

    try:
        read_bundle(manifest)
    except ContractEnvelopeError as error:
        return [str(error)]
    return []


def validate_manifest(manifest: Any) -> None:
    """Raise ``jsonschema.ValidationError`` when ``manifest`` breaks the schema."""

    jsonschema.Draft202012Validator(load_schema()).validate(manifest)
