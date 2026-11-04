"""The installed ``execution-receipt`` contract: select the spec a receipt names.

The consumer owns the contract at ``contracts/execution-receipt/``. This library ships the
copy its reader enforces, under ``contracts/execution-receipt/``, and
``tests/test_receipt_contract.py`` refuses a copy that differs from the authority.

Both bundle adapters write ``execution-receipt.json`` and both read it back; this is the one
reader they share, and ``write_receipt`` is the one writer they stamp it with. A reader
selects one of its installed specs by the document's contract id and version. It never
fetches or trusts a schema the document points to: ``spec.json`` names ``schema.json`` beside
it. A v1 document without the envelope is a legacy document and is read by its
``schema_version``; a document with one must name an installed spec, and its ``spec_digest``
must be exactly the installed spec's. The current version (``VERSION``) requires the
envelope, so ``write_receipt`` stamps every document this library writes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

from asago_contracts_core import ContractSpec, SpecSet, load_specs, write_envelope

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
CONTRACT = "execution-receipt"
VERSION = "execution-receipt-v2"
LEGACY_VERSION = "execution-receipt-v1"


@cache
def installed_specs() -> SpecSet:
    """Return every contract spec this library ships."""

    return load_specs(CONTRACTS_DIR)


def write_receipt(
    document: Mapping[str, Any],
    *,
    writer: str,
    writer_version: str,
    requires: Iterable[str] = (),
) -> dict[str, Any]:
    """Return ``document`` with the envelope ``VERSION`` requires, before a writer hands it on.

    The envelope names ``writer`` (the module that composed the document) and
    ``writer_version``, its package's version.
    """

    spec = installed_specs().spec(CONTRACT, VERSION)
    return write_envelope(
        document,
        spec,
        writer=writer,
        writer_version=writer_version,
        requires=requires,
    )


def read_receipt(document: Any) -> ContractSpec:
    """Return the installed spec an ``execution-receipt`` document names, or raise why not.

    The error is a ``ContractEnvelopeError``: an unsupported version lists the supported
    ones, an unknown ``requires`` entry names what the spec supports, an unknown field names
    the declared extensions, and a mismatched ``spec_digest`` names the status it broke.
    """

    return installed_specs().read(document)
