"""The delivery reader ships the midojo-delivery spec it enforces.

The consumer authority is ``contracts/midojo-delivery/`` at the repository root; this
package carries the installed copy. The first test refuses a copy that differs, and the
rest read the recorded v1 documents and the envelope's negative cases.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from asago_contracts_core import (
    ENVELOPE_FIELD,
    ContractSpec,
    InvalidDocumentError,
    SpecDigestMismatchError,
    SpecSet,
    UnknownContractError,
    UnknownFieldError,
    UnknownRequirementError,
    UnsupportedVersionError,
    write_envelope,
)

from asago_injection_spec.delivery_contract import CONTRACTS_DIR, installed_specs, read_delivery

_REPO = Path(__file__).resolve().parents[3]
_AUTHORITY = _REPO / "contracts" / "midojo-delivery" / "midojo-delivery-v1"
_INSTALLED = CONTRACTS_DIR / "midojo-delivery" / "midojo-delivery-v1"
_VERSION = "midojo-delivery-v1"
_CASES = _AUTHORITY / "cases"
_RECORDED = _REPO / "packages" / "asago-bundle-garak" / "tests" / "fixtures" / "ipi"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _spec() -> ContractSpec:
    return installed_specs().spec("midojo-delivery", _VERSION)


def _case(name: str) -> dict[str, Any]:
    return _json(_CASES / "valid" / f"{name}.json")


def _enveloped(spec: ContractSpec, document: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    envelope = {
        "id": spec.contract,
        "version": spec.version,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.delivery_contract",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
        **overrides,
    }
    return {**document, ENVELOPE_FIELD: envelope}


def test_the_installed_copy_is_the_authority() -> None:
    def files(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    assert files(_INSTALLED) == files(_AUTHORITY)


def test_the_installed_spec_is_the_published_v1() -> None:
    spec = _spec()

    assert (spec.status, spec.envelope, spec.supports, spec.extensions) == (
        "published",
        "optional",
        (),
        (),
    )


@pytest.mark.parametrize("case", sorted((_CASES / "valid").glob("*.json")), ids=lambda p: p.stem)
def test_the_reader_accepts_every_valid_case_as_a_legacy_document(case: Path) -> None:
    assert read_delivery(_json(case)).version == _VERSION


@pytest.mark.parametrize(
    "case", sorted(_RECORDED.glob("*/delivery.json")), ids=lambda p: p.parent.name
)
def test_the_reader_accepts_the_recorded_v1_runs(case: Path) -> None:
    assert read_delivery(_json(case)).version == _VERSION


def test_the_reader_accepts_the_envelope_and_reports_its_provenance() -> None:
    spec = _spec()
    written = write_envelope(
        _case("emitted"),
        spec,
        writer="asago-orch.qualification.midojo_episode_runner",
        writer_version="0.1.0",
        requires=[],
    )

    assert written[ENVELOPE_FIELD] == {
        "id": "midojo-delivery",
        "version": _VERSION,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "asago-orch.qualification.midojo_episode_runner",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
    }
    assert read_delivery(written).version == _VERSION


def test_an_unsupported_version_lists_the_supported_ones() -> None:
    with pytest.raises(UnsupportedVersionError) as raised:
        read_delivery(_enveloped(_spec(), _case("emitted"), version="midojo-delivery-v9"))

    assert "midojo-delivery-v9" in str(raised.value)
    assert "midojo-delivery-v1" in str(raised.value)


def test_an_unknown_contract_lists_the_installed_one() -> None:
    with pytest.raises(UnknownContractError, match="midojo-delivery"):
        read_delivery(_enveloped(_spec(), _case("emitted"), id="execution-receipt"))


def test_an_unknown_requires_entry_is_rejected() -> None:
    with pytest.raises(UnknownRequirementError, match="midojo-delivery-v2"):
        read_delivery(_enveloped(_spec(), _case("emitted"), requires=["midojo-delivery-v2"]))


def test_an_unknown_field_outside_an_extension_is_rejected() -> None:
    document = _enveloped(_spec(), _case("emitted"))
    document["probe_key"] = "a field v1 never declared"

    with pytest.raises(UnknownFieldError, match="probe_key"):
        read_delivery(document)


def test_a_draft_with_a_mismatched_spec_digest_is_rejected() -> None:
    draft = replace(_spec(), status="draft")
    other = "sha256:" + "0" * 64

    with pytest.raises(SpecDigestMismatchError) as raised:
        SpecSet((draft,)).read(_enveloped(draft, _case("emitted"), spec_digest=other))

    assert other in str(raised.value)
    assert "draft" in str(raised.value)


def test_the_schema_rejects_a_malformed_document() -> None:
    document = _case("emitted")
    del document["snapshot_id"]

    with pytest.raises(InvalidDocumentError, match="snapshot_id"):
        read_delivery(document)
