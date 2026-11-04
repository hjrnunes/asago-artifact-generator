"""The installed ``runtime-contract`` spec: the reader every runtime contract goes through.

``asago_package`` carries the authority copy of the contract as package data, so the version
directory this module loads is also the installed reader copy. The orchestrator writes one
contract per target, the producer's target-discovery builder emits a declared target's
contract, and ``generate --runtime-contract`` reads one; all three read it here.

``artifact-runtime-contract-v2`` is published with ``"envelope": "optional"``, because every
writer predates the envelope; a document without one is selected by its ``schema_version``.
``artifact-runtime-contract-v3`` is published with ``"envelope": "required"``, and every
writer stamps one through :func:`write_runtime_contract`. A reader accepts both versions,
and a document with an envelope must name an installed spec whose ``spec_digest`` is exactly
the installed spec's.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any

from asago_contracts_core import ContractSpec, SpecSet, load_specs, write_envelope

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
CONTRACT = "runtime-contract"
LEGACY_VERSION = "artifact-runtime-contract-v2"
VERSION = "artifact-runtime-contract-v3"
VERSIONS = (LEGACY_VERSION, VERSION)


@cache
def installed_specs() -> SpecSet:
    """Return every contract spec this library ships."""

    return load_specs(CONTRACTS_DIR)


def read_runtime_contract(document: Any) -> ContractSpec:
    """Return the installed spec a runtime contract names, or raise why not.

    The error is a ``ContractEnvelopeError``: an unsupported version lists the supported
    ones, an unknown ``requires`` entry names what the spec supports, an unknown field names
    the declared extensions, and a mismatched ``spec_digest`` names the status it broke. A
    v3 document without the envelope raises ``MissingEnvelopeError``.
    """

    return installed_specs().read(document)


def write_runtime_contract(
    document: Any,
    *,
    writer: str,
    writer_version: str,
    requires: Iterable[str] = (),
) -> dict[str, Any]:
    """Return ``document`` with the v3 self-identifying envelope around it."""

    return write_envelope(
        document,
        installed_specs().spec(CONTRACT, VERSION),
        writer=writer,
        writer_version=writer_version,
        requires=requires,
    )
