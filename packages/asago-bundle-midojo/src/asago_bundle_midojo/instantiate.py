"""instantiate: fill a MiDojo bundle template with the orchestrator's values.

The orchestrator knows the values (the gateway and MCP URLs, the model, the
rendered messages, the control plane's port); this module knows MiDojo's format.
The package's one rendered user message becomes the suite's prompt, or, for a
sequential package, each rendered ``user_turn_<n>`` becomes one of the suite's
``turns``; the verdict file's path becomes the absolute path inside the concrete
bundle.

An ``openshell`` bundle also takes the sandbox's values: ``image_id`` (a bare image ID,
``sha256:<64 hex>``: ``name@sha256:`` makes OpenShell's Docker driver pull),
``relay_port`` and ``service_port`` (integers, into the policy's port rules),
``relay_url`` (the model relay as the sandbox reaches it, on ``relay_port``),
``model``, ``instructions`` (the system message; empty when the suite has none, as
MiDojo's unmanaged runs send), ``allowed_tools`` (the package's exposed tools, the list
the Garak path gets, set comma-separated in the sandbox), and ``gateway`` and
``agent_timeout`` for ``midojo-run``. MiDojo expands ``${env.VAR}`` in the runtime
settings, so a value it would expand is refused.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from asago_bundle_core.errors import BundleError
from asago_bundle_core.slots import BUNDLE, fill
from asago_bundle_core.text import canonical_text
from asago_bundle_core.values import check_values, render_entrypoint

from .compiler import (
    BUNDLE_FILE,
    SINGLE,
    SUITE_FILE,
    USER_TURN_PREFIX,
    bundle_code,
    entrypoint_values,
)
from .openshell import OPENSHELL, runtime_type

TEMPLATE_KEYS = frozenset({"requires", "templates", "entrypoint", "serve"})
# MiDojo replaces ``{task:probe}`` in a prompt with the active payload, or with
# nothing when none is active (midojo.probes), so such a prompt would change.
PROBE_PLACEHOLDER = re.compile(r"\{([A-Za-z_]\w*):([A-Za-z_]\w*)\}")
# Characters PyYAML refuses or folds inside a quoted scalar; JSON text escapes them.
YAML_UNSAFE = re.compile("[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]")
MAX_PORT = 65535
# The turns a sequential reply bundle's judge grades, as Garak's ``judge_turns``.
JUDGE_TURNS = frozenset({"final", "all"})
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
# MiDojo's ``${env.VAR}`` reference (midojo ``yaml_task_suite._ENV_VAR_RE``), which it
# expands in the runtime settings when it loads the suite.
ENV_REFERENCE = re.compile(r"\$\{env\.[A-Za-z_]")
DEFAULT_PORTS = {"http": 80, "https": 443}


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
    suite_template = _read_json(template / manifest["templates"]["suite"])
    runtime = runtime_type(suite_template)
    checks = checks_for(runtime)
    if runtime == OPENSHELL:
        _explain_malformed(manifest["requires"], values, checks)
    check_values(manifest["requires"], values, checks, InstantiateError)
    suite_values = values
    if runtime == OPENSHELL:
        _check_relay(values)
        # The sandbox reads its tools from one environment variable, comma-separated.
        suite_values = {**values, "allowed_tools": ",".join(values["allowed_tools"])}
    suite = fill(suite_template, _resolver(out, suite_values))
    serve = manifest["serve"]
    keys = entrypoint_values(runtime)
    concrete = {
        **manifest,
        "templates": {},
        "serve": {
            "argv": render_entrypoint(serve["argv"], out, values, keys),
            "ready_url": render_entrypoint([serve["ready_url"]], out, values, keys)[0],
        },
        "entrypoint": render_entrypoint(manifest["entrypoint"], out, values, keys),
        "values_digest": hashlib.sha256(canonical_text(values).encode()).hexdigest(),
    }
    code = bundle_code(manifest.get("claim_level"), manifest.get("delivery", SINGLE))
    documents = {SUITE_FILE: yaml_text(suite), BUNDLE_FILE: canonical_text(concrete)}
    _write(out, template, documents, code)
    return concrete


def check_for(key: str) -> Callable[[Any], bool] | None:
    """Return the check of a value key; each ``user_turn_<n>`` is checked as one prompt."""

    if key.startswith(USER_TURN_PREFIX):
        return _prompt
    return CHECKS.get(key)


def checks_for(runtime: Any) -> Callable[[str], Callable[[Any], bool] | None]:
    """Return the value checks of a bundle whose suite runs the agent in ``runtime``."""

    if runtime != OPENSHELL:
        return check_for

    def check(key: str) -> Callable[[Any], bool] | None:
        return OPENSHELL_CHECKS.get(key) or check_for(key)

    return check


def _explain_malformed(
    requires: Any, values: Any, checks: Callable[[str], Callable[[Any], bool] | None]
) -> None:
    """Refuse a malformed sandbox value, saying what it must be."""

    if not isinstance(values, dict) or not isinstance(requires, list):
        return
    for key in requires:
        check = checks(key)
        if key in EXPECTED and key in values and check is not None and not check(values[key]):
            raise InstantiateError(f"values carry a malformed {key}: expected {EXPECTED[key]}")


def _check_relay(values: dict[str, Any]) -> None:
    """The relay URL must use the port the policy lets the sandbox reach."""

    parts = urlsplit(values["relay_url"])
    try:
        port = parts.port or DEFAULT_PORTS[parts.scheme]
    except ValueError as exc:
        raise InstantiateError(f"values carry a malformed relay_url: {exc}") from exc
    if port != values["relay_port"]:
        raise InstantiateError(
            f"values carry a relay_url on port {port}, not relay_port {values['relay_port']}: "
            "the sandbox policy lets the agent reach relay_port only"
        )


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


def _url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _port(value: Any) -> bool:
    return type(value) is int and 0 < value <= MAX_PORT


def _prompt(value: Any) -> bool:
    """Accept a text MiDojo sends unchanged: one without a probe placeholder."""

    return _text(value) and PROBE_PLACEHOLDER.search(value) is None


def _messages(value: Any) -> bool:
    """Accept exactly one user message: a single package is one prompt."""

    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return False
    message = value[0]
    return message.get("role") == "user" and _prompt(message.get("content"))


def _setting(value: Any) -> bool:
    """Accept a text MiDojo keeps as it is in the runtime settings."""

    return _text(value) and ENV_REFERENCE.search(value) is None


def _instructions(value: Any) -> bool:
    """Accept the system message, which may be empty: a MiDojo suite without one sends none."""

    return isinstance(value, str) and ENV_REFERENCE.search(value) is None


_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def _tool_list(value: Any) -> bool:
    """Accept the package's exposed tools: a non-empty list of names, as Garak gets them."""

    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(name, str) and _TOOL_NAME.match(name) for name in value)
    )


def _relay_url(value: Any) -> bool:
    return _url(value) and _setting(value) and bool(urlsplit(value).hostname)


def _image_id(value: Any) -> bool:
    return isinstance(value, str) and IMAGE_ID.fullmatch(value) is not None


def _seconds(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _facts(value: Any) -> bool:
    return isinstance(value, dict)


def _judge_turns(value: Any) -> bool:
    return isinstance(value, str) and value in JUDGE_TURNS


CHECKS: dict[str, Callable[[Any], bool]] = {
    "gateway_url": _url,
    "mcp_url": _url,
    "model": _text,
    "messages": _messages,
    "service_port": _port,
    "judge_url": _url,
    "judge_model": _text,
    "judge_runtime_facts": _facts,
    "judge_turns": _judge_turns,
}
# An openshell bundle's values; ``model`` goes into the runtime settings there.
OPENSHELL_CHECKS: dict[str, Callable[[Any], bool]] = {
    "image_id": _image_id,
    "relay_port": _port,
    "relay_url": _relay_url,
    "model": _setting,
    "instructions": _instructions,
    "allowed_tools": _tool_list,
    "gateway": _text,
    "agent_timeout": _seconds,
}
EXPECTED = {
    "image_id": "a bare image ID, sha256:<64 lowercase hex>",
    "relay_port": f"an integer port, 1..{MAX_PORT}",
    "service_port": f"an integer port, 1..{MAX_PORT}",
    "relay_url": "an http(s) URL with a host and no ${env.} reference",
    "model": "non-empty text with no ${env.} reference",
    "instructions": "text, possibly empty, with no ${env.} reference",
    "allowed_tools": "a non-empty list of tool names",
    "gateway": "the OpenShell gateway's name, non-empty text",
    "agent_timeout": "a positive number of seconds",
}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstantiateError(f"template file is unreadable: {path.name}: {exc}") from exc


def _write(out: Path, template: Path, documents: dict[str, str], names: Iterable[str]) -> None:
    if out.exists() and any(out.iterdir()):
        raise InstantiateError(f"output directory is not empty: {out}")
    code = {name: _template_bytes(template, name) for name in names}
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
