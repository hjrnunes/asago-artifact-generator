"""The installed ``midojo-delivery`` contract: select the spec a delivery document names.

The consumer owns the contract at ``contracts/midojo-delivery/``. This package ships the
copy its reader enforces, under ``contracts/midojo-delivery/``, and
``tests/test_delivery_contract.py`` refuses a copy that differs from the authority.

A reader selects one of its installed specs by the document's contract id and version.
It never fetches or trusts a schema the document points to: ``spec.json`` names
``schema.json`` beside it. A v1 document without the envelope is a legacy document and is
read by its ``schema_version``; a v2 document must carry the envelope, and its
``spec_digest`` must be exactly the installed spec's. Both versions stay installed, so a
delivery document recorded before the ``probe_key`` to ``slot_key`` rename still reads.

A writer whose document the MiDojo interpreter produced, such as the orchestrator's stamp
before the adapter reads it, hands it on through :func:`write_delivery`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

from asago_contracts_core import ContractSpec, SpecSet, load_specs, write_envelope

CONTRACTS_DIR = Path(__file__).resolve().parent / "contracts"
CONTRACT = "midojo-delivery"
VERSION = "midojo-delivery-v2"
LEGACY_VERSION = "midojo-delivery-v1"


@cache
def installed_specs() -> SpecSet:
    """Return every contract spec this reader ships."""

    return load_specs(CONTRACTS_DIR)


def write_delivery(
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


def read_delivery(document: Any) -> ContractSpec:
    """Return the installed spec a ``delivery.json`` document names, or raise why not.

    The error is a ``ContractEnvelopeError``: an unsupported version lists the supported
    ones, an unknown ``requires`` entry names what the spec supports, an unknown field names
    the declared extensions, and a mismatched ``spec_digest`` names the status it broke.
    """

    return installed_specs().read(document)
