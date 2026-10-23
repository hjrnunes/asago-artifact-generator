"""The adapter's library entry points: ``compile``, ``instantiate`` and ``parse``.

Each validates its own inputs and raises the step's ``BundleError`` subclass
(``CompileError``, ``InstantiateError``, ``ParseError``) on input it cannot use; the
command line calls these functions and nothing else.

``instantiate`` takes the target config as an argument and refuses one that is not
authorized for a run (``asago_bundle_core.target``) before it does anything else. It then
prepares the package for the run with ``asago_bundle_core.preparation.prepare_package``,
whose setup steps make real writes through ``call_target`` (by default an MCP call to the
target config's ``endpoint.mcp_url``), and turns the rendered stimulus and the judge's
facts into the values the lower-level template filler ``instantiate_bundle`` takes. The
bundle's agent uses the same authorized ``endpoint.mcp_url``. Every input, including each
template member, is checked before the first setup call. A package that cannot be
prepared returns the ``PreparationIncomplete`` result and writes no bundle.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from asago_artifact_generator.package_io import ArtifactPackage, load_package
from asago_bundle_core.preparation import (
    PreparationError,
    PreparationIncomplete,
    PreparedPackage,
    TargetCall,
    prepare_package,
)
from asago_bundle_core.target import TargetConfig, authorized_target, mcp_call_target

from .compiler import CompileError, compile_package
from .instantiate import CHECKS, InstantiateError, instantiate_bundle, read_template
from .report_parse import ParseError, parse_report

DEFAULT_JUDGE_TURNS = "final"


@dataclass(frozen=True)
class RunSettings:
    """What the caller chooses for one run, beyond the package and the target config.

    ``gateway_url`` is the Responses endpoint Garak's generator calls and ``model`` the model
    it names. ``judge_url`` and ``judge_model`` serve a reply package's judge, and
    ``judge_turns`` (``final`` or ``all``) a sequential reply package's. The MCP server the
    agent is told to use is always the authorized target config's ``endpoint.mcp_url``;
    ``mcp_url``, when set, must name that same URL. ``allowed_tools`` are the tools the
    agent may call, every operation of the package's inventory when None.
    """

    gateway_url: str
    model: str
    judge_url: str | None = None
    judge_model: str | None = None
    judge_turns: str = DEFAULT_JUDGE_TURNS
    mcp_url: str | None = None
    allowed_tools: tuple[str, ...] | None = None

    @classmethod
    def from_mapping(cls, settings: Any) -> RunSettings:
        """Return the settings a JSON object holds; raise ``InstantiateError`` otherwise."""

        if not isinstance(settings, Mapping):
            raise InstantiateError("the run settings must be a JSON object")
        if not all(isinstance(key, str) for key in settings):
            raise InstantiateError("the run settings' keys must be texts")
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(settings) - known)
        if unknown:
            raise InstantiateError(f"the run settings hold unknown keys: {', '.join(unknown)}")
        missing = sorted(key for key in ("gateway_url", "model") if key not in settings)
        if missing:
            raise InstantiateError(f"the run settings lack {', '.join(missing)}")
        values = dict(settings)
        if isinstance(values.get("allowed_tools"), list):
            values["allowed_tools"] = tuple(values["allowed_tools"])
        return cls(**values)


@dataclass(frozen=True)
class Instantiated:
    """A concrete bundle and how it was made.

    ``manifest`` is the bundle's ``bundle.json``, ``values`` the values that filled it, and
    ``preparation`` the prepared package, whose setup ledger and resolved bindings the
    caller records.
    """

    manifest: dict[str, Any]
    values: dict[str, Any]
    preparation: PreparedPackage


def compile(package_dir: str | Path, out_dir: str | Path) -> dict[str, Any]:
    """Write the bundle template for one package directory and return its manifest.

    Raises ``CapabilityGap`` before writing anything when Garak cannot deliver the package,
    and ``CompileError`` for a package it cannot read or an output directory in use.
    """

    _directory(package_dir, "package", CompileError)
    _new_directory(out_dir, CompileError)
    return compile_package(package_dir, out_dir)


def instantiate(
    package_dir: str | Path,
    template_dir: str | Path,
    out_dir: str | Path,
    *,
    target: TargetConfig | Mapping[str, Any] | str | Path,
    run: RunSettings | Mapping[str, Any],
    call_target: TargetCall | None = None,
) -> Instantiated | PreparationIncomplete:
    """Prepare the package for one run against ``target`` and write its concrete bundle.

    ``template_dir`` is what ``compile`` wrote for ``package_dir``. ``target`` is the target
    config: a ``TargetConfig``, a loaded mapping, or the path of a ``target.yaml``; its
    ``endpoint.mcp_url``, with any placeholder filled, is the MCP server both the setup steps
    and the bundle's agent use. ``call_target`` runs the setup steps; when None they go to
    that URL over MCP. Raises ``InstantiateError`` for an unauthorized target config or any
    other input it cannot use, including every template member, before setup runs.
    """

    loaded = authorized_target(target, InstantiateError)
    settings = run if isinstance(run, RunSettings) else RunSettings.from_mapping(run)
    package = _package(package_dir)
    template = _directory(template_dir, "template", InstantiateError)
    requires = _template_requires(template, package)
    _new_directory(out_dir, InstantiateError)
    mcp_url = loaded.mcp_url
    if not mcp_url or "{" in mcp_url:
        raise InstantiateError(f"the target config names no usable endpoint.mcp_url: {mcp_url!r}")
    if settings.mcp_url is not None and settings.mcp_url != mcp_url:
        raise InstantiateError(
            f"the run settings' mcp_url {settings.mcp_url!r} is not the authorized target "
            f"config's endpoint.mcp_url {mcp_url!r}"
        )
    allowed_tools = _allowed_tools(settings, package)
    _check_settings(settings, mcp_url, allowed_tools, requires)
    if call_target is None:
        call_target = mcp_call_target(mcp_url)
    try:
        prepared = prepare_package(package, call_target=call_target)
    except PreparationError as exc:
        raise InstantiateError(f"the package cannot be prepared: {exc}") from exc
    if isinstance(prepared, PreparationIncomplete):
        return prepared
    values = bundle_values(prepared, settings, mcp_url=mcp_url, allowed_tools=allowed_tools)
    manifest = instantiate_bundle(template, values, out_dir, target=loaded)
    return Instantiated(manifest=manifest, values=values, preparation=prepared)


def parse(
    bundle_dir: str | Path,
    out_path: str | Path,
    *,
    report: str | Path | None = None,
    records_dir: str | Path | None = None,
    tool_revision: str | None = None,
    adapter_revision: str | None = None,
) -> dict[str, Any]:
    """Write the receipt of one concrete bundle's Garak report to ``out_path`` and return it.

    ``report`` defaults to the bundle's native output; ``records_dir``, when given, is a
    directory of boundary records that replace the report's evidence. A run that failed
    still yields a receipt; ``ParseError`` means the inputs name no usable bundle.
    """

    _directory(bundle_dir, "bundle", ParseError)
    if not isinstance(out_path, str | Path) or not str(out_path):
        raise ParseError("the receipt path must be a path")
    if Path(out_path).is_dir():
        raise ParseError(f"the receipt path is a directory: {out_path}")
    if report is not None and (not isinstance(report, str | Path) or not str(report)):
        raise ParseError("the report must be a path")
    if records_dir is not None:
        _directory(records_dir, "records", ParseError)
    for name, value in (("tool_revision", tool_revision), ("adapter_revision", adapter_revision)):
        if value is not None and not isinstance(value, str):
            raise ParseError(f"{name} must be a string")
    return parse_report(
        bundle_dir,
        out_path,
        report=report,
        records_dir=records_dir,
        tool_revision=tool_revision,
        adapter_revision=adapter_revision,
    )


def bundle_values(
    prepared: PreparedPackage,
    settings: RunSettings,
    *,
    mcp_url: str,
    allowed_tools: list[str],
) -> dict[str, Any]:
    """Return the bundle values of a prepared package; settings left None are left out.

    ``messages`` is the rendered history followed by the rendered user text, and each
    ``user_turn_<n>`` (counted from 1) the text of message n, for a sequential template.
    ``judge_runtime_facts`` are the prepared judge facts.
    """

    stimulus = prepared.stimulus
    messages = [
        {"role": item["role"], "content": item["content"]}
        for item in stimulus.get("history") or []
        if isinstance(item, Mapping)
    ]
    messages.append({"role": "user", "content": stimulus["user_text"]})
    values = {
        "gateway_url": settings.gateway_url,
        "mcp_url": mcp_url,
        "allowed_tools": allowed_tools,
        "model": settings.model,
        "messages": messages,
        **{
            f"user_turn_{number}": message["content"]
            for number, message in enumerate(messages, start=1)
        },
        "judge_url": settings.judge_url,
        "judge_model": settings.judge_model,
        "judge_runtime_facts": dict(prepared.judge_facts),
        "judge_turns": settings.judge_turns,
    }
    return {key: value for key, value in values.items() if value is not None}


def _allowed_tools(settings: RunSettings, package: ArtifactPackage) -> list[str]:
    """Return the settings' tools, else every operation of the package's inventory."""

    if settings.allowed_tools is not None:
        if not isinstance(settings.allowed_tools, list | tuple):
            raise InstantiateError("the run settings carry a malformed allowed_tools")
        return list(settings.allowed_tools)
    inputs = _json_member(package, "inputs.json")
    inventory = inputs.get("inventory") if isinstance(inputs, dict) else None
    operations = inventory.get("operations") if isinstance(inventory, dict) else None
    names = sorted(
        {
            item["name"]
            for item in operations or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
    )
    if not names:
        raise InstantiateError(
            "no allowed tools: the run settings name none and the package inventory lists "
            "no operation"
        )
    return names


def _check_settings(
    settings: RunSettings, mcp_url: str, allowed_tools: list[str], requires: list[str]
) -> None:
    """Refuse a malformed setting, or one the template requires and the settings lack."""

    given = {
        "gateway_url": settings.gateway_url,
        "model": settings.model,
        "mcp_url": mcp_url,
        "allowed_tools": allowed_tools,
        "judge_url": settings.judge_url,
        "judge_model": settings.judge_model,
        "judge_turns": settings.judge_turns,
    }
    for key, value in given.items():
        if value is None:
            if key in requires:
                raise InstantiateError(f"the run settings lack {key}, which the template needs")
        elif not CHECKS[key](value):
            raise InstantiateError(f"the run settings carry a malformed {key}")


def _template_requires(template: Path, package: ArtifactPackage) -> list[str]:
    """Return the template's required values, after checking every template member.

    Refuses a template whose manifest or conversation and run templates
    ``instantiate_bundle`` could not use, and one not compiled for this package.
    """

    manifest, _ = read_template(template)
    if manifest.get("package_digest") != package.manifest.manifest_digest:
        raise InstantiateError("the template was not compiled from this package")
    return manifest["requires"]


def _package(package_dir: str | Path) -> ArtifactPackage:
    path = _directory(package_dir, "package", InstantiateError)
    try:
        return load_package(path)
    except (OSError, ValueError) as exc:
        raise InstantiateError(f"package cannot be loaded: {exc}") from exc


def _json_member(package: ArtifactPackage, name: str) -> Any:
    content = package.members.get(name)
    if content is None:
        return None
    try:
        return json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise InstantiateError(f"package {name} is not JSON: {exc}") from exc


def _directory(path: Any, role: str, error: type[Exception]) -> Path:
    if not isinstance(path, str | Path):
        raise error(f"the {role} directory must be a path")
    directory = Path(path)
    if not directory.is_dir():
        raise error(f"the {role} directory does not exist: {directory}")
    return directory


def _new_directory(path: Any, error: type[Exception]) -> None:
    if not isinstance(path, str | Path):
        raise error("the output directory must be a path")
    out = Path(path)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise error(f"output directory is not empty: {out}")
