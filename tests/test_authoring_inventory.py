from __future__ import annotations

from typing import Any

import pytest

from asago_artifact_generator.authoring.core import ArtifactValidationError
from asago_artifact_generator.authoring.inventory import _first_fact_named, _resolved_judge_spec

_INVENTORY = {
    "facts": [
        {"ref": "fact:a", "value": 1, "provenance": {"from": "input"}},
        {"ref": "fact:b", "value": "two"},
        {"ref": "fact:novalue"},
    ]
}


@pytest.mark.parametrize(
    ("spec", "message", "path"),
    [
        ("text", "judge specification must be an object", "judge.json"),
        (
            {"fact_refs": "fact:a"},
            "judge specification fact_refs must be a list",
            "judge.json.fact_refs",
        ),
        ({"fact_refs": [" "]}, "unknown static fact reference:  ", "judge.json.fact_refs[0]"),
        (
            {"fact_refs": ["fact:a", 7]},
            "unknown static fact reference: 7",
            "judge.json.fact_refs[1]",
        ),
        (
            {"fact_refs": ["fact:missing"]},
            "unknown static fact reference: fact:missing",
            "judge.json.fact_refs[0]",
        ),
        (
            {"fact_refs": ["fact:novalue"]},
            "static fact has no supplied value: fact:novalue",
            "judge.json.fact_refs[0]",
        ),
    ],
)
def test_resolved_judge_spec_rejects_bad_specs(spec: Any, message: str, path: str) -> None:
    with pytest.raises(ArtifactValidationError) as error:
        _resolved_judge_spec(spec, _INVENTORY)

    assert (error.value.message, error.value.path) == (message, path)


def test_resolved_judge_spec_resolves_supplied_facts() -> None:
    spec = {"question": "q", "criteria": ["c"], "fact_refs": ["fact:a", "fact:b"]}

    assert _resolved_judge_spec(None, _INVENTORY) is None
    assert _resolved_judge_spec(spec, _INVENTORY) == {
        "question": "q",
        "criteria": ["c"],
        "facts": [
            {"ref": "fact:a", "value": 1, "source": "fact:a", "provenance": {"from": "input"}},
            {"ref": "fact:b", "value": "two", "source": "fact:b"},
        ],
    }


def test_first_fact_named_returns_the_first_matching_fact_or_none() -> None:
    inventory = {"facts": ["skip", {"ref": "a", "n": 1}, {"ref": "a", "n": 2}]}

    assert _first_fact_named(inventory, "a") == {"ref": "a", "n": 1}
    assert _first_fact_named(inventory, "b") is None
    assert _first_fact_named({}, "a") is None
