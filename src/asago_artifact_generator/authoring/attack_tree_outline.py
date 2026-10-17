"""Render a producer ``and-or-tree-v1`` attack tree as an indented outline.

The outline keeps the tree's structure (operators, child order, node IDs, source IDs,
techniques, assets, beliefs, violated rules, and safe outcomes) as plain lines, so the
plan author reads it as a tree. Reference nodes name other scenarios that reach the same
hazard; they collapse into one line per parent and contribute no provenance IDs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

AND_OR_TREE_FORMAT = "and-or-tree-v1"

_INDENT = "  "
_KNOWN_TYPES = frozenset({"goal", "operator", "condition", "action", "reference"})
# Fields the node line itself renders; every other field becomes a detail line.
_HEAD_FIELDS = frozenset(
    {
        "node_id",
        "type",
        "category",
        "label",
        "source_id",
        "source_ids",
        "children",
        "operator",
        "ordered",
        "scenario_id",
        "this_scenario",
        "technique_id",
        "technique",
        "asset",
    }
)
# The whole tree is a proposed hypothesis, and the note says so once.
_DEFAULT_AUTHORITY = "proposed_hypothesis"
_SOURCE_LIST_NAMES = {"hazards": "hazard", "losses": "loss", "violates": "violates"}


def is_and_or_tree(tree: Any) -> bool:
    return isinstance(tree, dict) and tree.get("format") == AND_OR_TREE_FORMAT


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _source_ids(node: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    source_ids = node.get("source_ids")
    for value in [node.get("source_id"), *(source_ids if isinstance(source_ids, list) else [])]:
        if _nonblank(value) and value not in ids:
            ids.append(value)
    return ids


def _is_reference(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "reference"


def _head(node: dict[str, Any]) -> str:
    parts: list[str] = []
    operator = node.get("operator")
    if node.get("type") == "operator" or _nonblank(operator):
        word = operator.strip() if _nonblank(operator) else "operator"
        parts.append(f"{word} in order" if node.get("ordered") is True else word)
    elif node.get("ordered") is True:
        parts.append("in order")
    parts.append(node["node_id"] if _nonblank(node.get("node_id")) else "node")
    ids = _source_ids(node)
    if ids:
        parts.append(f"[{', '.join(ids)}]")
    details: list[str] = []
    category = node.get("category")
    scenario_id = node.get("scenario_id")
    first = category.strip() if _nonblank(category) else ""
    if _nonblank(scenario_id):
        first = f"{first} {scenario_id}".strip()
    if first:
        details.append(first)
    if node.get("this_scenario") is True:
        details.append("this scenario")
    node_type = node.get("type")
    if node_type is not None and node_type not in _KNOWN_TYPES:
        details.append(f"type {_text(node_type)}")
    technique = " ".join(
        _text(node[key]) for key in ("technique_id", "technique") if node.get(key) is not None
    )
    if technique:
        details.append(f"ATLAS {technique}")
    if node.get("asset") is not None:
        details.append(f"asset {_text(node['asset'])}")
    if details:
        parts.append(f"({', '.join(details)})")
    head = " ".join(parts)
    label = node.get("label")
    return f"{head}: {_text(label)}" if label is not None and _text(label) else head


def _source_list_lines(key: str, items: list[Any], node: dict[str, Any]) -> list[str]:
    name = _SOURCE_LIST_NAMES.get(key, key.replace("_", " "))
    bracketed = _source_ids(node)
    lines: list[str] = []
    for item in items:
        if isinstance(item, dict) and set(item) <= {"source_id", "label"}:
            source_id, label = item.get("source_id"), item.get("label")
            # A goal's hazard usually repeats the goal's own label and bracketed ID.
            if source_id in bracketed and label == node.get("label"):
                continue
            entry = f"{name} {_text(source_id)}" if source_id is not None else name
            lines.append(f"{entry}: {_text(label)}" if label is not None else entry)
        else:
            lines.append(f"{name}: {_text(item)}")
    return lines


def _attacker_lines(attacker: Any) -> list[str]:
    if not isinstance(attacker, dict):
        return [f"attacker: {_text(attacker)}"]
    lines: list[str] = []
    for key, value in attacker.items():
        if key == "profile":
            lines.append(f"attacker: {_text(value)}")
        elif key == "plan" and isinstance(value, list):
            if len(value) == 1:
                lines.append(f"attacker plan: {_text(value[0])}")
            else:
                steps = " ".join(f"({index}) {_text(step)}" for index, step in enumerate(value, 1))
                lines.append(f"attacker plan: {steps}")
        else:
            lines.append(f"attacker {key.replace('_', ' ')}: {_text(value)}")
    return lines


def _detail_lines(node: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in node.items():
        if key in _HEAD_FIELDS or value is None or value == []:
            continue
        if key == "authority" and value == _DEFAULT_AUTHORITY:
            continue
        if key == "attacker":
            lines += _attacker_lines(value)
        elif isinstance(value, list):
            lines += _source_list_lines(key, value, node)
        else:
            lines.append(f"{key.replace('_', ' ')}: {_text(value)}")
    return lines


def _reference_line(references: list[dict[str, Any]]) -> str:
    scenarios = [
        ref["scenario_id"] if _nonblank(ref.get("scenario_id")) else "an unnamed scenario"
        for ref in references
    ]
    return "- other scenarios that reach the same hazard, not part of this test: " + ", ".join(
        scenarios
    )


def and_or_outline(root: Any) -> list[str]:
    """Return the indented outline of an and-or tree rooted at ``root``."""

    lines: list[str] = []

    def render(node: dict[str, Any], depth: int, marker: str) -> None:
        lines.append(f"{_INDENT * depth}{marker}{_head(node)}")
        lines.extend(f"{_INDENT * (depth + 1)}{line}" for line in _detail_lines(node))
        children = node.get("children")
        if not isinstance(children, list):
            return
        nodes = [child for child in children if isinstance(child, dict)]
        references = [child for child in nodes if _is_reference(child)]
        ordered = node.get("ordered") is True
        position = 0
        for child in nodes:
            if _is_reference(child):
                if child is references[0]:
                    lines.append(f"{_INDENT * (depth + 1)}{_reference_line(references)}")
                continue
            position += 1
            render(child, depth + 1, f"{position}. " if ordered else "- ")

    if isinstance(root, dict):
        render(root, 0, "")
    return lines


def and_or_provenance(root: Any) -> Iterator[tuple[str, str | None]]:
    """Yield ``(identifier, node_id)`` for every citable ID of this scenario's tree.

    Reference nodes describe other scenarios, so neither their IDs nor anything below them
    is yielded.
    """

    def walk(node: Any) -> Iterator[tuple[str, str | None]]:
        if not isinstance(node, dict) or _is_reference(node):
            return
        node_id = node.get("node_id") if _nonblank(node.get("node_id")) else None
        if node_id is not None:
            yield node_id, node_id
        for identifier in _source_ids(node):
            yield identifier, node_id
        for key in _SOURCE_LIST_NAMES:
            items = node.get(key)
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and _nonblank(item.get("source_id")):
                    yield item["source_id"], node_id
        children = node.get("children")
        for child in children if isinstance(children, list) else []:
            yield from walk(child)

    yield from walk(root)
