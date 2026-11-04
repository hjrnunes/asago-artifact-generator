"""The installed ``tool-bundle`` contract: select the spec a bundle names.

The consumer owns the contract at ``contracts/tool-bundle/``. This library ships the copy
its readers enforce, under ``contracts/tool-bundle/``, and ``tests/test_bundle_contract.py``
refuses a copy that differs from the authority.

Both tool adapters write ``bundle.json`` and read it back; this is the one reader they share.
It selects one of its installed specs by the document's contract id and version. It never
fetches or trusts a schema the document points to: ``spec.json`` names ``schema.json`` beside
it. A v1 document without the envelope is a legacy document and is read by its
``schema_version``; a document with one must name an installed spec, and its ``spec_digest``
must be exactly the installed spec's.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

from asago_contracts_core import ContractSpec, SpecSet, load_specs

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
CONTRACT = "tool-bundle"
VERSION = "tool-bundle-v1"


@cache
def installed_specs() -> SpecSet:
    """Return every contract spec this library ships."""

    return load_specs(CONTRACTS_DIR)


def read_bundle(document: Any) -> ContractSpec:
    """Return the installed spec a ``tool-bundle`` document names, or raise why not.

    The error is a ``ContractEnvelopeError``: an unsupported version lists the supported
    ones, an unknown ``requires`` entry names what the spec supports, an unknown field names
    the declared extensions, and a mismatched ``spec_digest`` names the status it broke.
    """

    return installed_specs().read(document)
