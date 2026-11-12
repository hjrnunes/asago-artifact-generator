"""The supported envelope and its pinned MiDojo document vectors.

The lock pins byte copies of MiDojo's injection-spec documents; the snapshot digest
vectors are read from the installed contracts rather than duplicated here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import warnings
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from midojo_spec.injection_spec_schema import INJECTION_SPEC_DOCUMENT_V1, load_schema

from asago_injection_spec.document import (
    DELIVERIES,
    ENVELOPE_FIELDS,
    ENVELOPE_SCHEMA,
    INJECTION_SPEC_SCHEMA,
    EnvelopeError,
    UnsupportedEnvelopeVersion,
    build_envelope,
    read_envelope,
)

VECTORS = Path(__file__).parent / "fixtures" / "midojo"
LOCK = Path(__file__).parent / "fixtures" / "midojo.lock"
DOCUMENTS = VECTORS / "injection_spec_document"
PINNED = json.loads(LOCK.read_text(encoding="utf-8"))
PINS: dict[str, str] = PINNED["files"]


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _envelope(episode: dict[str, Any]) -> dict[str, Any]:
    """Wrap ``episode`` in the envelope compile writes, for the tests that edit one."""

    return build_envelope(
        episode,
        package_id="SCN-101-SCN-101",
        scenario_id="SCN-101",
        package_digest="a" * 64,
        claim_level="command_attempt",
        delivery="single",
        placements=[],
        tool_revision_range=">=8e8208f",
    )


@pytest.mark.parametrize("name", sorted(PINS))
def test_the_copies_are_midojo_s_own(name: str) -> None:
    """Every copy's bytes are the ones the lock records."""

    assert hashlib.sha256((VECTORS / name).read_bytes()).hexdigest() == PINS[name]


def test_the_lock_names_the_revision_the_copies_come_from() -> None:
    """The lock names the revision this repository pins its contracts to."""

    pyproject = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    pin = re.search(
        r'midojo-spec = \{ git = "[^"]+",\s*subdirectory = "packages/midojo-spec",\s*'
        r'rev = "([0-9a-f]{40})"',
        pyproject,
    )

    assert pin, "pyproject.toml pins midojo-spec by git rev"
    assert PINNED["source"] == f"midojo local {pin.group(1)}"


def test_the_copies_are_the_vectors_and_nothing_else() -> None:
    copied = {
        path.relative_to(VECTORS).as_posix() for path in VECTORS.rglob("*") if path.is_file()
    }

    assert copied == set(PINS)


@pytest.mark.parametrize("name", sorted(path.stem for path in DOCUMENTS.glob("v1_*.json")))
def test_each_v1_vector_is_a_document_of_the_schema_this_adapter_validates_against(
    name: str,
) -> None:
    """MiDojo's published v1 documents validate against the schema its contracts ship."""

    jsonschema.validate(_read(DOCUMENTS / f"{name}.json"), load_schema())


def test_the_schema_constant_is_the_one_the_contracts_name() -> None:
    assert INJECTION_SPEC_SCHEMA == INJECTION_SPEC_DOCUMENT_V1 == "midojo-injection-spec-v1"


def test_build_envelope_writes_the_episode_as_given() -> None:
    episode = _read(DOCUMENTS / "v1_single.json")

    envelope = build_envelope(
        episode,
        package_id="SCN-101-SCN-101",
        scenario_id="SCN-101",
        package_digest="a" * 64,
        claim_level="command_attempt",
        delivery="single",
        placements=[{"instruction": 0, "slot": "piggybank.note.body"}],
        tool_revision_range=">=8e8208f",
    )

    assert envelope["schema_version"] == ENVELOPE_SCHEMA
    assert set(envelope) == set(ENVELOPE_FIELDS)
    assert envelope["episode"] is not episode
    assert envelope["episode"] == episode
    assert envelope["placements"] == [{"instruction": 0, "slot": "piggybank.note.body"}]


def test_a_v1_envelope_reads_back_what_it_wrote() -> None:
    episode = _read(DOCUMENTS / "v1_sequential.json")
    written = build_envelope(
        episode,
        package_id="SCN-102-SCN-102",
        scenario_id="SCN-102",
        package_digest="b" * 64,
        claim_level="command_attempt",
        delivery="sequential",
        placements=[],
        tool_revision_range=">=8e8208f",
    )

    envelope = read_envelope(written)

    assert envelope.source_version == ENVELOPE_SCHEMA
    assert (envelope.package_id, envelope.scenario_id) == ("SCN-102-SCN-102", "SCN-102")
    assert (envelope.package_digest, envelope.claim_level) == ("b" * 64, "command_attempt")
    assert envelope.delivery == "sequential"
    assert envelope.placements == ()
    assert envelope.tool_revision_range == ">=8e8208f"
    assert envelope.episode == episode


def test_read_envelope_takes_a_path(tmp_path: Path) -> None:
    path = tmp_path / "injection-spec.json"
    document = _envelope(_read(DOCUMENTS / "v1_single.json"))
    path.write_text(json.dumps(document), encoding="utf-8")

    assert read_envelope(path).episode == document["episode"]


def test_reading_a_v1_envelope_warns_about_nothing() -> None:
    episode = _read(DOCUMENTS / "v1_sequential.json")
    written = build_envelope(
        episode,
        package_id="SCN-102-SCN-102",
        scenario_id="SCN-102",
        package_digest="b" * 64,
        claim_level="command_attempt",
        delivery="sequential",
        placements=[],
        tool_revision_range=">=8e8208f",
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")

        envelope = read_envelope(written)

    assert envelope.source_version == ENVELOPE_SCHEMA


@pytest.mark.parametrize("version", ["asago-midojo-package-v9", ""])
def test_an_unknown_version_is_refused_naming_it(version: str) -> None:
    document = _envelope(_read(DOCUMENTS / "v1_single.json")) | {"schema_version": version}

    with pytest.raises(UnsupportedEnvelopeVersion) as raised:
        read_envelope(document)

    assert raised.value.version == version
    assert version in str(raised.value)
    assert raised.value.supported == (ENVELOPE_SCHEMA,)


def test_an_envelope_without_a_version_is_refused() -> None:
    document = _envelope(_read(DOCUMENTS / "v1_single.json"))
    del document["schema_version"]

    with pytest.raises(EnvelopeError, match="schema_version is required"):
        read_envelope(document)


def test_a_version_that_is_not_text_is_refused() -> None:
    with pytest.raises(EnvelopeError, match="schema_version must be a string"):
        read_envelope(_envelope(_read(DOCUMENTS / "v1_single.json")) | {"schema_version": 1})


@pytest.mark.parametrize("version", ["midojo-injection-spec-v9", "asago-midojo-package-v9"])
def test_an_episode_member_of_another_version_is_refused_naming_it(version: str) -> None:
    """The v1 envelope holds a v1 document: another version is a refusal, not a conversion."""

    episode = _read(DOCUMENTS / "v1_single.json") | {"schema_version": version}
    document = build_envelope(
        episode,
        package_id="SCN-101-SCN-101",
        scenario_id="SCN-101",
        package_digest="a" * 64,
        claim_level="command_attempt",
        delivery="single",
        placements=[],
        tool_revision_range=">=8e8208f",
    )

    with pytest.raises(UnsupportedEnvelopeVersion) as raised:
        read_envelope(document)

    assert raised.value.version == version
    assert raised.value.where == "episode"
    assert raised.value.supported == (INJECTION_SPEC_SCHEMA,)
    assert version in str(raised.value)


def test_an_unknown_key_is_refused() -> None:
    """An unknown envelope key is refused."""

    document = _envelope(_read(DOCUMENTS / "v1_single.json"))

    with pytest.raises(EnvelopeError, match="unknown key 'extra'; the document is closed"):
        read_envelope(document | {"extra": None})


@pytest.mark.parametrize("delivery", ["", "streaming", None])
def test_a_delivery_this_adapter_does_not_write_is_refused(delivery: Any) -> None:
    document = copy.deepcopy(_envelope(_read(DOCUMENTS / "v1_single.json"))) | {
        "delivery": delivery
    }

    with pytest.raises(EnvelopeError, match="delivery"):
        read_envelope(document)


def test_the_deliveries_are_the_two_the_compiler_writes() -> None:
    assert DELIVERIES == ("single", "sequential")


@pytest.mark.parametrize("name", ["package_id", "scenario_id", "package_digest", "claim_level"])
def test_a_field_that_is_not_text_is_refused(name: str) -> None:
    document = _envelope(_read(DOCUMENTS / "v1_single.json")) | {name: ["not", "text"]}

    with pytest.raises(EnvelopeError, match=f"{name} must be a non-empty string"):
        read_envelope(document)


def test_placements_that_are_not_an_array_of_objects_are_refused() -> None:
    document = copy.deepcopy(_envelope(_read(DOCUMENTS / "v1_single.json"))) | {
        "placements": ["piggybank.note.body"]
    }

    with pytest.raises(EnvelopeError, match="placements entry must be a JSON object"):
        read_envelope(document)


def test_turns_that_are_not_text_are_refused() -> None:
    document = copy.deepcopy(_envelope(_read(DOCUMENTS / "v1_single.json")))
    document["episode"]["turns"] = [1, 2]

    with pytest.raises(EnvelopeError, match="turns must be an array of non-empty strings"):
        read_envelope(document)


def test_a_plan_that_is_not_an_object_or_null_is_refused() -> None:
    document = copy.deepcopy(_envelope(_read(DOCUMENTS / "v1_single.json")))
    document["episode"]["plan"] = "instructions"

    with pytest.raises(EnvelopeError, match="plan must be a JSON object or null"):
        read_envelope(document)


def test_an_episode_without_a_plan_still_reads() -> None:
    """MiDojo's document makes the plan optional; a receipt reads such an episode unread."""

    document = copy.deepcopy(_envelope(_read(DOCUMENTS / "v1_single.json")))
    del document["episode"]["plan"]

    assert "plan" not in read_envelope(document).episode


def test_an_episode_without_a_runtime_is_refused() -> None:
    document = copy.deepcopy(_envelope(_read(DOCUMENTS / "v1_single.json")))
    del document["episode"]["runtime"]

    with pytest.raises(EnvelopeError, match="runtime must be a JSON object"):
        read_envelope(document)
