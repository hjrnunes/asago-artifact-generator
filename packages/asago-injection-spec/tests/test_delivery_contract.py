"""The delivery reader ships both midojo-delivery specs it accepts.

The consumer authority is ``contracts/midojo-delivery/`` at the repository root; this
package carries the installed copy. The first test refuses a copy that differs, and the
rest read the recorded v2 documents and the v1 cases a run before the term.5a rename
wrote, with the refusals each version owes.
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
    MissingEnvelopeError,
    SpecDigestMismatchError,
    SpecSet,
    UnknownContractError,
    UnknownFieldError,
    UnknownRequirementError,
    UnsupportedVersionError,
    write_envelope,
)

from asago_injection_spec.delivery_contract import (
    CONTRACT,
    CONTRACTS_DIR,
    LEGACY_VERSION,
    VERSION,
    installed_specs,
    read_delivery,
    write_delivery,
)

_REPO = Path(__file__).resolve().parents[3]
_AUTHORITY = _REPO / "contracts" / "midojo-delivery"
_INSTALLED = CONTRACTS_DIR / "midojo-delivery"
_VERSIONS = (LEGACY_VERSION, VERSION)
# A run recorded after the term.5a rename: the Garak route's fixtures hold the runner's
# envelope.
_RECORDED_V2 = _REPO / "packages" / "asago-bundle-garak" / "tests" / "fixtures" / "ipi"


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _spec(version: str) -> ContractSpec:
    return installed_specs().spec(CONTRACT, version)


def _root(version: str) -> Path:
    return _AUTHORITY / version


def _case(version: str, name: str) -> dict[str, Any]:
    return _json(_root(version) / "cases" / "valid" / f"{name}.json")


def _cases(version: str) -> list[Path]:
    return sorted((_root(version) / "cases" / "valid").glob("*.json"))


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


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.parametrize("version", _VERSIONS)
def test_the_installed_copy_is_the_authority(version: str) -> None:
    assert _files(_INSTALLED / version) == _files(_AUTHORITY / version)


@pytest.mark.parametrize("version", _VERSIONS)
def test_the_installed_spec_is_the_published_version(version: str) -> None:
    spec = _spec(version)
    expected = "optional" if version == LEGACY_VERSION else "required"

    assert (spec.contract, spec.version, spec.status) == (CONTRACT, version, "published")
    assert (spec.envelope, spec.supports, spec.extensions) == (expected, (), ())


@pytest.mark.parametrize("version", _VERSIONS)
def test_every_version_declares_its_own_slot_key(version: str) -> None:
    schema = _json(_root(version) / "schema.json")
    required = schema["$defs"]["delivery_fact"]["required"]
    exposure = schema["$defs"]["instruction_exposure"]["required"]

    assert ("slot_key" if version == VERSION else "probe_key") in required
    assert ("slot_key" if version == VERSION else "probe_key") in exposure


@pytest.mark.parametrize("case", _cases(LEGACY_VERSION), ids=lambda path: path.stem)
def test_the_reader_accepts_every_v1_valid_case_as_a_legacy_document(case: Path) -> None:
    assert read_delivery(_json(case)).version == LEGACY_VERSION


@pytest.mark.parametrize("case", _cases(VERSION), ids=lambda path: path.stem)
def test_the_reader_accepts_every_v2_valid_case_with_its_envelope(case: Path) -> None:
    written = write_envelope(
        _json(case), _spec(VERSION), writer="tests.delivery_contract", writer_version="0.1.0"
    )

    assert read_delivery(written).version == VERSION


@pytest.mark.parametrize("case", _cases(VERSION), ids=lambda path: path.stem)
def test_a_v2_document_without_the_envelope_is_refused(case: Path) -> None:
    with pytest.raises(MissingEnvelopeError, match=CONTRACT):
        read_delivery(_json(case))


def test_the_writer_stamps_the_v2_envelope() -> None:
    spec = _spec(VERSION)

    written = write_delivery(
        _case(VERSION, "placed"), writer="tests.delivery_contract", writer_version="0.1.0"
    )

    assert written[ENVELOPE_FIELD] == {
        "id": CONTRACT,
        "version": VERSION,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.delivery_contract",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
    }
    assert read_delivery(written).version == VERSION


def test_the_reader_accepts_both_versions() -> None:
    v1 = _case(LEGACY_VERSION, "placed")
    v2 = write_envelope(
        _case(VERSION, "placed"),
        _spec(VERSION),
        writer="tests.delivery_contract",
        writer_version="0.1.0",
    )

    assert read_delivery(v1).version == LEGACY_VERSION
    assert read_delivery(v2).version == VERSION


@pytest.mark.parametrize(
    "case", sorted(_RECORDED_V2.glob("*/delivery.json")), ids=lambda path: path.parent.name
)
def test_the_reader_accepts_the_recorded_v2_runs(case: Path) -> None:
    document = _json(case)

    assert document[ENVELOPE_FIELD]["provenance"]["writer"] == (
        "asago-orch.qualification.midojo_episode_runner"
    )
    assert read_delivery(document).version == VERSION


@pytest.mark.parametrize("version", _VERSIONS)
def test_the_reader_reports_the_envelope_s_provenance(version: str) -> None:
    spec = _spec(version)
    written = write_envelope(
        _case(version, "emitted"),
        spec,
        writer="asago-orch.qualification.midojo_episode_runner",
        writer_version="0.1.0",
    )

    assert written[ENVELOPE_FIELD] == {
        "id": CONTRACT,
        "version": version,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "asago-orch.qualification.midojo_episode_runner",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
    }
    assert read_delivery(written).version == version


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unsupported_version_lists_the_supported_ones(version: str) -> None:
    with pytest.raises(UnsupportedVersionError) as raised:
        read_delivery(_enveloped(_spec(version), _case(version, "emitted"), version="v9"))

    assert "'v9'" in str(raised.value)
    for installed in _VERSIONS:
        assert installed in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_contract_lists_the_installed_one(version: str) -> None:
    with pytest.raises(UnknownContractError, match=CONTRACT):
        read_delivery(_enveloped(_spec(version), _case(version, "emitted"), id="not-a-contract"))


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_requires_entry_is_rejected(version: str) -> None:
    with pytest.raises(UnknownRequirementError, match="supports: none"):
        read_delivery(
            _enveloped(_spec(version), _case(version, "emitted"), requires=["midojo-delivery-v9"])
        )


@pytest.mark.parametrize("version", _VERSIONS)
def test_an_unknown_field_outside_an_extension_is_rejected(version: str) -> None:
    document = _enveloped(_spec(version), _case(version, "emitted"))
    document["undeclared"] = "a field neither version declares"

    with pytest.raises(UnknownFieldError, match="undeclared"):
        read_delivery(document)


@pytest.mark.parametrize("version", _VERSIONS)
def test_a_draft_with_a_mismatched_spec_digest_is_rejected(version: str) -> None:
    draft = replace(_spec(version), status="draft")
    other = "sha256:" + "0" * 64

    with pytest.raises(SpecDigestMismatchError) as raised:
        SpecSet((draft,)).read(_enveloped(draft, _case(version, "emitted"), spec_digest=other))

    assert other in str(raised.value)
    assert "draft" in str(raised.value)


@pytest.mark.parametrize("version", _VERSIONS)
def test_the_schema_rejects_a_malformed_document(version: str) -> None:
    document = _case(version, "emitted")
    del document["snapshot_id"]
    if version == VERSION:
        document = _enveloped(_spec(version), document)

    with pytest.raises(InvalidDocumentError, match="snapshot_id"):
        read_delivery(document)
