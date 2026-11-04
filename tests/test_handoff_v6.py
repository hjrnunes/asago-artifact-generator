"""The adapter reads scenario-handoff-v6 beside v5, and keeps v5 reading.

``scenario-handoff-v6`` states the controlling party as
``party_relation.controlling_party`` and requires the self-identifying envelope.
The mirrored kit publishes both versions, and the adapter selects the version
from the document's own ``schema_version`` before it reads the digest in that
version's domain. A v6 document without the envelope is refused; a document the
version's schema rejects reports the producer's codes, as a v5 document does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from asago_handoff.envelope import (
    ENVELOPE_FIELD,
    ContractSpec,
    MissingEnvelopeError,
    load_specs,
)

from asago_artifact_generator.input_adapter import (
    HandoffSchemaInvalid,
    InputKind,
    InputSourceError,
    ShapeVersionMalformed,
    _framed_digest,
    load_input,
)

_V5 = "scenario-handoff-v5"
_V6 = "scenario-handoff-v6"
_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff"
_KIT = _CONTRACT / _V6 / "cases"
_EXPECTED = json.loads((_KIT / "expected-violations.json").read_text(encoding="utf-8"))
_INDIRECT = "adversarial-indirect-listing.json"


def _case(relative: str) -> dict[str, Any]:
    return json.loads((_KIT / relative).read_text(encoding="utf-8"))


def _spec(version: str) -> ContractSpec:
    return load_specs(_CONTRACT.parent).spec("scenario-handoff", version)


def _published(document: dict[str, Any]) -> dict[str, Any]:
    """Return a v6 document with the envelope the version requires.

    The helper writes the envelope itself rather than through
    ``write_envelope``, because that writer validates the body it stamps and
    the invalid kit cases must still reach the reader's code walk.
    """

    spec = _spec(_V6)
    envelope = {
        "id": spec.contract,
        "version": spec.version,
        "spec_digest": spec.spec_digest,
        "requires": [],
        "provenance": {
            "writer": "tests.handoff_v6",
            "writer_version": "0.1.0",
            "validator_version": "asago-contracts-core 0.1.0",
        },
    }
    return {**document, ENVELOPE_FIELD: envelope}


def _written(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_an_enveloped_v6_document_reads_with_the_renamed_party(tmp_path: Path) -> None:
    document = _case(f"valid/{_INDIRECT}")
    relation = document["attack_shape"]["indirect"]["party_relation"]

    view = load_input(_written(tmp_path, _published(document)))

    assert view.kind is InputKind.SCENARIO_HANDOFF_V6
    assert view.payload[ENVELOPE_FIELD]["version"] == _V6
    assert view.attack_shape["indirect"]["party_relation"] == {
        "controlling_party": relation["controlling_party"],
        "benign_user_actor_ref": relation["benign_user_actor_ref"],
    }


def test_a_v6_document_without_the_envelope_is_refused(tmp_path: Path) -> None:
    path = _written(tmp_path, _case(f"valid/{_INDIRECT}"))

    with pytest.raises(InputSourceError, match="self-identifying envelope") as raised:
        load_input(path)

    assert isinstance(raised.value.__cause__, MissingEnvelopeError)


def test_a_v6_document_signed_in_the_v5_domain_is_refused(tmp_path: Path) -> None:
    document = {
        key: value for key, value in _case(f"valid/{_INDIRECT}").items() if key != "content_digest"
    }
    document["content_digest"] = _framed_digest(_V5, document)

    with pytest.raises(InputSourceError, match="content_digest does not match"):
        load_input(_written(tmp_path, _published(document)))


@pytest.mark.parametrize(
    "case", sorted((_KIT / "valid").glob("*.json")), ids=lambda path: path.stem
)
def test_every_valid_v6_case_reads_once_enveloped(tmp_path: Path, case: Path) -> None:
    document = json.loads(case.read_text(encoding="utf-8"))

    view = load_input(_written(tmp_path, _published(document)))

    assert view.kind is InputKind.SCENARIO_HANDOFF_V6
    assert view.scenario_id == document["scenario_id"]


@pytest.mark.parametrize("relative", sorted(_EXPECTED))
def test_every_invalid_v6_case_reports_the_kits_codes(tmp_path: Path, relative: str) -> None:
    document = _published(_case(relative))

    with pytest.raises(HandoffSchemaInvalid) as raised:
        load_input(_written(tmp_path, document))

    assert raised.value.codes == tuple(_EXPECTED[relative])


def test_a_v6_shape_break_is_a_typed_malformed_shape(tmp_path: Path) -> None:
    document = _case("invalid/shape-plan-length-mismatch.json")

    with pytest.raises(ShapeVersionMalformed) as raised:
        load_input(_written(tmp_path, _published(document)))

    assert raised.value.code == "shape_version_malformed"
    assert raised.value.schema_path.startswith("attack_shape")
