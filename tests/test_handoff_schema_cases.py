"""The consumer rejects every producer schema case with the producer's codes.

The producer's v4 kit carries ``invalid/schema-*.json``, one broken field of
a bound handoff each, with the producer's codes in ``expected-violations.json``.
This reader reports the same codes in the same order on the raised error's
``codes``; only the unknown-version case stops earlier, at the version gate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asago_artifact_generator.input_adapter import InputSourceError, load_input

_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "scenario-handoff"
_KIT = _CONTRACT / "handoff-v4"
_EXPECTED = json.loads((_KIT / "expected-violations.json").read_text("utf-8"))
_VERSION_GATE = "schema-unknown-version"


def _cases() -> list[str]:
    return [
        path.stem
        for path in sorted((_KIT / "invalid").glob("schema-*.json"))
        if path.stem != _VERSION_GATE
    ]


@pytest.mark.parametrize("name", _cases())
def test_the_reader_rejects_each_schema_case_with_the_kits_codes(name: str) -> None:
    codes = _EXPECTED[f"invalid/{name}.json"]

    with pytest.raises(InputSourceError) as raised:
        load_input(_KIT / "invalid" / f"{name}.json")

    assert codes
    assert raised.value.codes == tuple(codes)
    assert str(raised.value).startswith(f"handoff schema invalid: {', '.join(codes)}")


def test_the_unknown_version_case_stops_at_the_version_gate() -> None:
    with pytest.raises(InputSourceError) as raised:
        load_input(_KIT / "invalid" / f"{_VERSION_GATE}.json")

    assert str(raised.value) == (
        "authoring source must be a producer scenario-handoff-v4 document; "
        "found scenario-handoff-v9"
    )
