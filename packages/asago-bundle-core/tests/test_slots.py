"""Slot markers: one-key objects that mark a hole in a bundle template."""

from __future__ import annotations

from typing import Any

import pytest

from asago_bundle_core.slots import (
    BUNDLE,
    VALUE,
    bundle_slot,
    contains_marker,
    fill,
    marker,
    template_slots,
    value_slot,
)


def test_the_builders_make_one_key_objects() -> None:
    assert value_slot("model") == {"$value": "model"}
    assert bundle_slot("reports") == {"$bundle": "reports"}
    assert (VALUE, BUNDLE) == ("$value", "$bundle")


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"$value": "model"}, ("$value", "model")),
        ({"$bundle": "run.yaml"}, ("$bundle", "run.yaml")),
        ({"$value": "model", "extra": 1}, None),
        ({"$other": "model"}, None),
        ("$value", None),
        (["$value"], None),
    ],
)
def test_marker_recognises_only_a_lone_known_key(node: Any, expected: Any) -> None:
    assert marker(node) == expected


def test_contains_marker_looks_inside_objects_and_lists() -> None:
    assert contains_marker({"a": [{"b": {"$value": "x"}}]})
    assert contains_marker([1, {"$bundle": "x"}])
    assert not contains_marker({"a": [1, "$value", {"b": None}]})


def test_template_slots_collects_value_keys_only() -> None:
    first = {"a": {"$value": "model"}, "b": [{"$value": "mcp_url"}, {"$bundle": "reports"}]}
    second = {"c": {"$value": "model"}}

    assert template_slots([first, second]) == {"model", "mcp_url"}


def test_fill_replaces_every_slot_with_the_resolver_result() -> None:
    seen: list[tuple[str, Any]] = []

    def resolve(kind: str, argument: Any) -> Any:
        seen.append((kind, argument))
        return f"<{kind}:{argument}>"

    template = {"x": [{"$value": "a"}, 1], "y": {"z": {"$bundle": "b"}}, "w": "text"}

    assert fill(template, resolve) == {
        "x": ["<$value:a>", 1],
        "y": {"z": "<$bundle:b>"},
        "w": "text",
    }
    assert seen == [("$value", "a"), ("$bundle", "b")]


def test_fill_does_not_mutate_the_template() -> None:
    template = {"x": {"$value": "a"}}

    fill(template, lambda kind, argument: "v")

    assert template == {"x": {"$value": "a"}}
