"""instantiate: fill a bundle template with resolved values.

``instantiate_bundle`` is the lower step of the adapter's ``instantiate``
(``entry_points``), which prepares the package and builds the values. The values
are the URLs, model, allowed tools, rendered messages or, for a sequential
package, each rendered user turn, resolved judge facts, and the turns the judge
grades; this module knows Garak's format. It substitutes no package bindings:
the messages and facts arrive already resolved. Like ``instantiate``, it refuses
a target config not authorized for a run before it reads anything else.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from asago_bundle_core.errors import BundleError
from asago_bundle_core.schema import manifest_errors
from asago_bundle_core.slots import BUNDLE, VALUE, fill, template_slots
from asago_bundle_core.target import TargetConfig, authorized_target
from asago_bundle_core.text import canonical_text
from asago_bundle_core.values import check_values, render_entrypoint

from .compiler import (
    BUNDLE_FILE,
    CONVERSATIONS,
    ENTRYPOINT_VALUES,
    REPORTS,
    RUN_CONFIG,
    USER_TURN_PREFIX,
)

MESSAGE_ROLES = frozenset({"system", "user", "assistant"})
JUDGE_TURNS = frozenset({"final", "all"})
TEMPLATE_KEYS = frozenset({"requires", "templates", "entrypoint"})
TEMPLATE_ROLES = frozenset({"conversation", "run"})
USER_TURN = re.compile(rf"{USER_TURN_PREFIX}[1-9][0-9]*")
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
# What may stand in braces in a template's entrypoint: what render_entrypoint renders
# ({bundle} and the value keys), and {tool_python}, which it leaves for the runner.
ENTRYPOINT_PLACEHOLDERS = frozenset({"bundle", "tool_python", *ENTRYPOINT_VALUES})


class InstantiateError(BundleError):
    """The template or the values cannot make a concrete bundle."""


def instantiate_bundle(
    template_dir: str | Path,
    values: dict[str, Any],
    out_dir: str | Path,
    *,
    target: TargetConfig | Mapping[str, Any] | str | Path,
) -> dict[str, Any]:
    """Write the concrete bundle for ``values`` and return its manifest.

    ``target`` is the target config (see ``asago_bundle_core.target``); one that does not
    mark the target as a mock or a test instance is refused first.
    """

    authorized_target(target, InstantiateError)
    return fill_template(read_template(template_dir), values, out_dir)


@dataclass(frozen=True)
class Template:
    """A bundle template ``read_template`` checked: its manifest and its two documents."""

    manifest: dict[str, Any]
    documents: dict[str, Any]


def read_template(template_dir: str | Path) -> Template:
    """Read a bundle template and refuse every defect of its own that filling could meet.

    The manifest must satisfy ``tool-bundle-v1`` and name exactly a conversation and a run
    template, each a file inside the template directory holding a JSON object. The slot
    markers are the ones core ``fill`` replaces (``$value`` and ``$bundle``); any other
    object, ``$``-prefixed keys included, is data. Every ``$value`` must name a text key
    and every ``$bundle`` a relative path inside the bundle. ``requires`` must list exactly
    the ``$value`` keys plus the entrypoint's value keys (``ENTRYPOINT_VALUES``), each one
    this adapter checks (``check_for``), and the entrypoint may hold no placeholder
    ``render_entrypoint`` does not render besides ``{tool_python}``. What is left to
    ``fill_template`` is the values themselves.
    """

    template = Path(template_dir)
    manifest = _read_json(template / BUNDLE_FILE)
    if not isinstance(manifest, dict) or not TEMPLATE_KEYS <= manifest.keys():
        raise InstantiateError(f"{BUNDLE_FILE} is not a bundle template manifest")
    errors = manifest_errors(manifest)
    if errors:
        raise InstantiateError(f"{BUNDLE_FILE} is not a tool-bundle-v1 template: {errors[0]}")
    names = manifest["templates"]
    if set(names) != TEMPLATE_ROLES:
        raise InstantiateError(f"{BUNDLE_FILE} templates must name {sorted(TEMPLATE_ROLES)}")
    for name in names.values():
        if not _inside(name):
            raise InstantiateError(f"template file {name!r} is outside the template directory")
    requires = manifest["requires"]
    unknown = sorted(key for key in requires if check_for(key) is None)
    if unknown:
        raise InstantiateError(
            f"{BUNDLE_FILE} requires keys this adapter has no check for: {unknown}"
        )
    unrendered = sorted(
        {
            name
            for arg in manifest["entrypoint"]
            for name in PLACEHOLDER.findall(arg)
            if name not in ENTRYPOINT_PLACEHOLDERS
        }
    )
    if unrendered:
        raise InstantiateError(f"{BUNDLE_FILE} entrypoint has unknown placeholders: {unrendered}")
    documents = {role: _read_json(template / name) for role, name in names.items()}
    for role, document in documents.items():
        if not isinstance(document, dict):
            raise InstantiateError(f"the {role} template is not a JSON object")
        problem = _slot_problem(document)
        if problem:
            raise InstantiateError(f"the {role} template {problem}")
    used = template_slots(documents.values()) | set(ENTRYPOINT_VALUES)
    if set(requires) != used:
        raise InstantiateError(
            f"{BUNDLE_FILE} requires does not match what the templates and the entrypoint "
            f"use: unused {sorted(set(requires) - used)}, unlisted {sorted(used - set(requires))}"
        )
    return Template(manifest, documents)


def fill_template(
    template: Template, values: dict[str, Any], out_dir: str | Path
) -> dict[str, Any]:
    """Write the concrete bundle of a template ``read_template`` returned; return its manifest.

    The caller has authorized the target. Raises ``InstantiateError`` for values that lack
    a ``requires`` key or carry a malformed one, and for an output directory in use.
    """

    manifest = template.manifest
    out = Path(out_dir).resolve()
    check_values(manifest["requires"], values, check_for, InstantiateError)

    def resolve(kind: str, argument: Any) -> Any:
        return str(out / argument) if kind == BUNDLE else values[argument]

    conversation = fill(template.documents["conversation"], resolve)
    run = fill(template.documents["run"], resolve)
    concrete = {
        **manifest,
        "templates": {},
        "entrypoint": render_entrypoint(manifest["entrypoint"], out, values, ENTRYPOINT_VALUES),
        "values_digest": hashlib.sha256(canonical_text(values).encode()).hexdigest(),
    }
    _write(
        out,
        {
            CONVERSATIONS: json.dumps(conversation, ensure_ascii=False) + "\n",
            RUN_CONFIG: canonical_text(run),
            BUNDLE_FILE: canonical_text(concrete),
        },
    )
    (out / REPORTS).mkdir()
    return concrete


def _inside(name: Any) -> bool:
    """Say whether ``name`` is a relative path that stays inside its directory."""

    if not isinstance(name, str) or not name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and not name.startswith("\\") and ".." not in path.parts


def _slot_problem(document: Any) -> str | None:
    """Return what is wrong with the first slot marker of ``document`` that core
    ``fill`` would replace, or None. Other objects, ``$``-prefixed keys included, are not
    treated as markers; ``fill`` still walks into them for nested markers."""

    problems: list[str] = []

    def visit(kind: str, argument: Any) -> None:
        if kind == VALUE and not isinstance(argument, str):
            problems.append(f"has a $value slot naming {argument!r}, which is not a key")
        elif kind == BUNDLE and not _inside(argument):
            problems.append(
                f"has a $bundle slot {argument!r} that is not a path inside the bundle"
            )

    fill(document, visit)
    return problems[0] if problems else None


def check_for(key: str) -> Callable[[Any], bool] | None:
    """Return the check of a value key; each ``user_turn_<n>`` (n from 1) is a text."""

    if USER_TURN.fullmatch(key):
        return _text
    return CHECKS.get(key)


def _url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _names(value: Any) -> bool:
    return isinstance(value, list) and all(_text(item) for item in value)


def _message(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and item.get("role") in MESSAGE_ROLES
        and isinstance(item.get("content"), str)
    )


def _messages(value: Any) -> bool:
    """Accept chat messages whose last one is the user turn Garak sends."""

    return (
        isinstance(value, list)
        and bool(value)
        and all(_message(item) for item in value)
        and value[-1]["role"] == "user"
    )


def _object(value: Any) -> bool:
    return isinstance(value, dict)


def _judge_turns(value: Any) -> bool:
    return isinstance(value, str) and value in JUDGE_TURNS


CHECKS: dict[str, Callable[[Any], bool]] = {
    "gateway_url": _url,
    "mcp_url": _url,
    "judge_url": _url,
    "model": _text,
    "judge_model": _text,
    "allowed_tools": _names,
    "messages": _messages,
    "judge_runtime_facts": _object,
    "judge_turns": _judge_turns,
}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstantiateError(f"template file is unreadable: {path.name}: {exc}") from exc


def _write(out: Path, files: dict[str, str]) -> None:
    if out.exists() and any(out.iterdir()):
        raise InstantiateError(f"output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
