"""instantiate: fill a MiDojo bundle template with orch's values.

orch knows the values (the gateway and MCP URLs, the model, the rendered
messages, the control plane's port); this module knows MiDojo's format. The
package's one rendered user message becomes the suite's prompt, and the verdict
file's path becomes the absolute path inside the concrete bundle.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from asago_bundle_core.errors import BundleError
from asago_bundle_core.slots import BUNDLE, fill
from asago_bundle_core.text import canonical_text
from asago_bundle_core.values import check_values, render_entrypoint

from .compiler import BUNDLE_CODE, BUNDLE_FILE, ENTRYPOINT_VALUES, SUITE_FILE

TEMPLATE_KEYS = frozenset({"requires", "templates", "entrypoint", "serve"})
# MiDojo replaces ``{task:probe}`` in a prompt with the active payload, or with
# nothing when none is active (midojo.probes), so such a prompt would change.
PROBE_PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*):([A-Za-z_]\w*)\}")
# Characters PyYAML refuses or folds inside a quoted scalar; JSON text escapes them.
YAML_UNSAFE = re.compile("[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]")
MAX_PORT = 65535


class InstantiateError(BundleError):
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
    check_values(manifest["requires"], values, check_for, InstantiateError)
    suite = fill(_read_json(template / manifest["templates"]["suite"]), _resolver(out, values))
    serve = manifest["serve"]
    concrete = {
        **manifest,
        "templates": {},
        "serve": {
            "argv": _render(serve["argv"], out, values),
            "ready_url": _render([serve["ready_url"]], out, values)[0],
        },
        "entrypoint": _render(manifest["entrypoint"], out, values),
        "values_digest": hashlib.sha256(canonical_text(values).encode()).hexdigest(),
    }
    _write(out, template, {SUITE_FILE: yaml_text(suite), BUNDLE_FILE: canonical_text(concrete)})
    return concrete


def check_for(key: str) -> Callable[[Any], bool] | None:
    """Return the check of a value key."""

    return CHECKS.get(key)


def yaml_text(document: Any) -> str:
    """Return ``document`` as JSON text that PyYAML reads back unchanged.

    JSON is YAML, but PyYAML rejects some raw characters (the C1 controls) and
    folds U+0085, U+2028 and U+2029 as line breaks, so those stay escaped.
    """

    text = canonical_text(document)
    return YAML_UNSAFE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def _resolver(out: Path, values: dict[str, Any]) -> Callable[[str, Any], Any]:
    def resolve(kind: str, argument: Any) -> Any:
        if kind == BUNDLE:
            return str(out / argument)
        return values[argument][0]["content"] if argument == "messages" else values[argument]

    return resolve


def _render(argv: list[str], out: Path, values: dict[str, Any]) -> list[str]:
    return render_entrypoint(argv, out, values, ENTRYPOINT_VALUES)


def _url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _port(value: Any) -> bool:
    return type(value) is int and 0 < value <= MAX_PORT


def _messages(value: Any) -> bool:
    """Accept exactly one user message: MiDojo sends one prompt per evaluation."""

    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return False
    message = value[0]
    content = message.get("content")
    return (
        message.get("role") == "user"
        and _text(content)
        and PROBE_PLACEHOLDER.search(content) is None
    )


CHECKS: dict[str, Callable[[Any], bool]] = {
    "gateway_url": _url,
    "mcp_url": _url,
    "model": _text,
    "messages": _messages,
    "service_port": _port,
}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstantiateError(f"template file is unreadable: {path.name}: {exc}") from exc


def _write(out: Path, template: Path, documents: dict[str, str]) -> None:
    if out.exists() and any(out.iterdir()):
        raise InstantiateError(f"output directory is not empty: {out}")
    code = {name: _template_bytes(template, name) for name in BUNDLE_CODE}
    out.mkdir(parents=True, exist_ok=True)
    (out / Path(SUITE_FILE).parent).mkdir()
    for name, text in documents.items():
        (out / name).write_text(text, encoding="utf-8")
    for name, content in code.items():
        (out / name).write_bytes(content)


def _template_bytes(template: Path, name: str) -> bytes:
    try:
        return (template / name).read_bytes()
    except OSError as exc:
        raise InstantiateError(f"template file is unreadable: {name}: {exc}") from exc
