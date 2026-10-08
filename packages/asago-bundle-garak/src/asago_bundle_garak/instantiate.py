"""instantiate: fill a bundle template with orch's values.

orch knows the values (URLs, model, allowed tools, rendered messages or, for
a sequential package, each rendered user turn, resolved judge facts, the
turns the judge grades); this module knows Garak's format. It substitutes no package
bindings: the messages and facts arrive already resolved.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .compiler import (
    BUNDLE_FILE,
    CONVERSATIONS,
    REPORTS,
    RUN_CONFIG,
    USER_TURN_PREFIX,
    canonical_text,
)
from .slots import BUNDLE, fill

MESSAGE_ROLES = frozenset({"system", "user", "assistant"})
JUDGE_TURNS = frozenset({"final", "all"})
TEMPLATE_KEYS = frozenset({"requires", "templates", "entrypoint"})


class InstantiateError(ValueError):
    """The template or the values cannot make a concrete bundle."""


def instantiate_bundle(
    template_dir: str | Path, values: dict[str, Any], out_dir: str | Path
) -> dict[str, Any]:
    """Write the concrete bundle for ``values`` and return its manifest."""

    template = Path(template_dir)
    out = Path(out_dir).resolve()
    manifest = _read_json(template / BUNDLE_FILE)
    if not isinstance(manifest, dict) or not TEMPLATE_KEYS <= manifest.keys():
        raise InstantiateError(f"{BUNDLE_FILE} is not a bundle template manifest")
    check_values(manifest["requires"], values)
    templates = {role: _read_json(template / name) for role, name in manifest["templates"].items()}

    def resolve(kind: str, argument: Any) -> Any:
        return str(out / argument) if kind == BUNDLE else values[argument]

    conversation = fill(templates["conversation"], resolve)
    run = fill(templates["run"], resolve)
    concrete = {
        **manifest,
        "templates": {},
        "entrypoint": render_entrypoint(manifest["entrypoint"], out, values),
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


def render_entrypoint(entrypoint: list[str], out: Path, values: dict[str, Any]) -> list[str]:
    """Render ``{bundle}`` and ``{model}``; ``{tool_python}`` stays for orch."""

    return [
        arg.replace("{bundle}", str(out)).replace("{model}", str(values["model"]))
        for arg in entrypoint
    ]


def check_values(requires: list[str], values: Any) -> None:
    """Refuse values that lack a required key or carry a malformed one."""

    if not isinstance(values, dict):
        raise InstantiateError("values must be a JSON object")
    for key in requires:
        if key not in values:
            raise InstantiateError(f"values lack {key}")
        check = check_for(key)
        if check is not None and not check(values[key]):
            raise InstantiateError(f"values carry a malformed {key}")


def check_for(key: str) -> Callable[[Any], bool] | None:
    """Return the check of a value key; each ``user_turn_<n>`` is a text."""

    if key.startswith(USER_TURN_PREFIX):
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
