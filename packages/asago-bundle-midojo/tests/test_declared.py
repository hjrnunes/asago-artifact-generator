"""The tools a target config declares: ``tools-list.json`` and ``runtime-spec.yaml`` beside it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from episode_cases import SPEC, TARGET, TOOLS, target_variant

from asago_bundle_core.target import load_target_config
from asago_bundle_midojo.declared import DeclarationFault, declared_tools


def declared(target: Path) -> Any:
    return declared_tools(load_target_config(target, DeclarationFault))


def test_the_fixture_target_declares_its_tools_and_its_one_write() -> None:
    tools = declared(TARGET)

    assert sorted(tools.tools) == ["get_balance", "list_notes", "withdraw"]
    assert tools.tools["list_notes"] == next(t for t in TOOLS if t["name"] == "list_notes")
    assert tools.mutating == frozenset({"withdraw"})


@pytest.mark.parametrize(
    ("annotations", "listed", "mutating"),
    [
        ({"readOnlyHint": False}, False, True),
        ({"destructiveHint": True}, False, True),
        ({}, True, True),
        ({}, False, False),
        ({"readOnlyHint": True}, True, False),
    ],
)
def test_a_tool_is_mutating_by_its_annotations_else_by_the_runtime_spec(
    tmp_path: Path, annotations: dict[str, Any], listed: bool, mutating: bool
) -> None:
    tools = [
        {**tool, "annotations": annotations} if tool["name"] == "withdraw" else tool
        for tool in TOOLS
    ]
    spec = {**SPEC, "operations": {"mutating": ["withdraw"] if listed else []}}

    found = declared(target_variant(tmp_path / "variant", tools=tools, spec=spec))

    assert ("withdraw" in found.mutating) is mutating


def test_a_runtime_spec_without_operations_declares_no_write(tmp_path: Path) -> None:
    spec = {key: value for key, value in SPEC.items() if key != "operations"}

    assert declared(target_variant(tmp_path / "variant", spec=spec)).mutating == frozenset()


def test_a_config_given_as_a_mapping_has_no_files_to_read() -> None:
    config = load_target_config(TARGET, DeclarationFault).config

    with pytest.raises(DeclarationFault, match="give that file's path"):
        declared_tools(load_target_config(config, DeclarationFault))


def test_a_tools_list_that_is_not_one_cannot_be_read(tmp_path: Path) -> None:
    target = target_variant(tmp_path / "variant")
    (target.parent / "tools-list.json").write_text('{"tools": [{"title": "x"}]}', encoding="utf-8")

    with pytest.raises(DeclarationFault, match="is not a tools/list result"):
        declared(target)
