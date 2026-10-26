"""What the caller's target config declares about the target's tools.

A target config is ``target.yaml`` with ``tools-list.json`` beside it, the target's MCP
``tools/list`` result (each tool's name, input and output schemas, and annotations), and
``runtime-spec.yaml``, which states what that list cannot, such as ``operations.mutating``.
``compile`` reads both from the directory of the ``target.yaml`` the caller names.

A tool is mutating when its annotations set ``readOnlyHint`` false or ``destructiveHint``
true; a tool with neither hint is mutating when ``operations.mutating`` lists it, and
read-only otherwise. A tool whose ``readOnlyHint`` is true is read-only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from asago_bundle_core.errors import BundleError
from asago_bundle_core.target import TargetConfig

TOOLS_FILE = "tools-list.json"
RUNTIME_SPEC_FILE = "runtime-spec.yaml"


class DeclarationFault(BundleError):
    """The target config's declaration files cannot be read."""


@dataclass(frozen=True)
class DeclaredTools:
    """The target's tools by name, as ``tools-list.json`` lists them, and which ones write."""

    tools: Mapping[str, Mapping[str, Any]]
    mutating: frozenset[str]


def declared_tools(config: TargetConfig) -> DeclaredTools:
    """Return the tools the files beside ``config``'s ``target.yaml`` declare, or raise."""

    if config.source is None:
        raise DeclarationFault(
            f"the target config was given as a mapping; its {TOOLS_FILE} and "
            f"{RUNTIME_SPEC_FILE} are read beside its target.yaml, so give that file's path"
        )
    directory = config.source.parent
    listed = _read(directory / TOOLS_FILE, json.loads)
    tools = listed.get("tools") if isinstance(listed, dict) else None
    if not isinstance(tools, list) or not all(
        isinstance(tool, dict) and isinstance(tool.get("name"), str) for tool in tools
    ):
        raise DeclarationFault(f"{directory / TOOLS_FILE} is not a tools/list result")
    spec = _read(directory / RUNTIME_SPEC_FILE, yaml.safe_load)
    operations = spec.get("operations") if isinstance(spec, dict) else None
    listed_mutating = operations.get("mutating", []) if isinstance(operations, dict) else []
    if not isinstance(listed_mutating, list):
        raise DeclarationFault(
            f"{directory / RUNTIME_SPEC_FILE}'s operations.mutating is not a list"
        )
    by_name = {tool["name"]: tool for tool in tools}
    mutating = frozenset(
        name for name, tool in by_name.items() if _mutating(tool, name in listed_mutating)
    )
    return DeclaredTools(by_name, mutating)


def _mutating(tool: Mapping[str, Any], listed: bool) -> bool:
    annotations = tool.get("annotations")
    hints = annotations if isinstance(annotations, dict) else {}
    if hints.get("readOnlyHint") is True:
        return False
    if hints.get("readOnlyHint") is False or hints.get("destructiveHint") is True:
        return True
    return listed


def _read(path: Any, load: Any) -> Any:
    try:
        return load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        raise DeclarationFault(f"the target config's {path.name} cannot be read: {exc}") from exc
