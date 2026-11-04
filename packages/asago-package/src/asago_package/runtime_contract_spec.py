"""The installed ``runtime-contract`` spec: the reader every runtime contract goes through.

``asago_package`` carries the authority copy of the contract as package data, so the version
directory this module loads is also the installed reader copy. The orchestrator writes one
contract per target, the producer's target-discovery builder emits a declared target's
contract, and ``generate --runtime-contract`` reads one; all three read it here.

The contract is published with ``"envelope": "optional"``, because every writer predates the
envelope. A document without one is selected by its ``schema_version``; a document with one
must name an installed spec, and its ``spec_digest`` must be exactly the installed spec's.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

from asago_contracts_core import ContractSpec, SpecSet, load_specs

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
CONTRACT = "runtime-contract"
VERSION = "artifact-runtime-contract-v2"


@cache
def installed_specs() -> SpecSet:
    """Return every contract spec this library ships."""

    return load_specs(CONTRACTS_DIR)


def read_runtime_contract(document: Any) -> ContractSpec:
    """Return the installed spec a runtime contract names, or raise why not.

    The error is a ``ContractEnvelopeError``: an unsupported version lists the supported
    ones, an unknown ``requires`` entry names what the spec supports, an unknown field names
    the declared extensions, and a mismatched ``spec_digest`` names the status it broke.
    """

    return installed_specs().read(document)
