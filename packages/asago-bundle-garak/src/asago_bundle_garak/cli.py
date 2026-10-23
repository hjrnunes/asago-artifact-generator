"""Command line for the Garak adapter.

Exit codes: 0 done, 1 the input cannot be used (including a target config
not authorized for a run), 2 usage error, 3 capability gap (``compile`` only:
Garak cannot deliver the package, such as a sequential history holding a
message other than a user turn), 4 the package could not be prepared
(``instantiate`` only). ``instantiate`` prints its preparation as JSON on
stdout: the setup ledger, bindings and prerequisites, or the incomplete reason.

``instantiate TEMPLATE --values VALUES --out OUT`` is the legacy form for callers that
resolve the values themselves: it reads no target config, runs no preparation and prints
nothing. ``--values`` combined with ``--package``, ``--target``, ``--port`` or ``--run`` is
a usage error, and so is a call that gives neither ``--values`` nor all of ``--package``,
``--target`` and ``--run``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from asago_bundle_core.gap import (
    EXIT_CAPABILITY_GAP,
    EXIT_INCOMPLETE,
    EXIT_INPUT,
    EXIT_OK,
    CapabilityGap,
)
from asago_bundle_core.preparation import PreparationIncomplete
from asago_bundle_core.target import AUTHORIZATION_FIELD, AUTHORIZED_KINDS, authorized_target

from .compiler import CompileError
from .entry_points import compile, instantiate, parse
from .instantiate import InstantiateError, instantiate_resolved
from .report_parse import ParseError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asago-bundle-garak",
        description="Compile a package into a Garak bundle and parse Garak's report.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    compile_cmd = commands.add_parser(
        "compile", help="write a bundle template and bundle.json for one package"
    )
    compile_cmd.add_argument("package", type=Path, help="artifact package directory")
    compile_cmd.add_argument("--out", type=Path, required=True, help="new template directory")
    compile_cmd.set_defaults(handler=_compile)
    instantiate_cmd = commands.add_parser(
        "instantiate",
        help="prepare the package against the target (its setup makes real writes) and fill "
        "the bundle template into a concrete bundle; or, in the legacy form, fill it with "
        "values already resolved",
        description="Give --package, --target and --run (and --port when the endpoint URLs "
        "hold {port}) to prepare the package against the target and fill the template. The "
        "legacy form, TEMPLATE --values VALUES --out OUT, is for callers that resolve the "
        "values themselves: it reads no target config and runs no preparation. --values "
        "cannot be combined with --package, --target, --port or --run.",
    )
    instantiate_cmd.add_argument("template", type=Path, help="bundle template directory")
    instantiate_cmd.add_argument(
        "--values",
        type=Path,
        help="legacy form: values.json holding values already resolved; no preparation runs",
    )
    instantiate_cmd.add_argument(
        "--package", type=Path, help="the artifact package the template is for"
    )
    instantiate_cmd.add_argument(
        "--target",
        type=Path,
        help=f"the target's target.yaml; its {AUTHORIZATION_FIELD} must be one of "
        f"{', '.join(AUTHORIZED_KINDS)}",
    )
    instantiate_cmd.add_argument(
        "--port", type=int, help="fills {port} in the target's endpoint URLs"
    )
    instantiate_cmd.add_argument(
        "--run",
        type=Path,
        help="run settings JSON: gateway_url, model, and optionally judge_url, judge_model, "
        "judge_turns, mcp_url, allowed_tools",
    )
    instantiate_cmd.add_argument("--out", type=Path, required=True, help="new bundle directory")
    instantiate_cmd.set_defaults(handler=_instantiate, usage_error=instantiate_cmd.error)
    parse_cmd = commands.add_parser(
        "parse",
        help="write execution-receipt-v1 from Garak's report; a failed run still exits 0",
    )
    parse_cmd.add_argument("bundle", type=Path, help="concrete bundle directory")
    parse_cmd.add_argument("--out", type=Path, required=True, help="receipt path")
    parse_cmd.add_argument(
        "--report", type=Path, help="Garak report (default: the bundle's native output)"
    )
    parse_cmd.add_argument(
        "--records", type=Path, help="package output directory holding boundary records"
    )
    parse_cmd.add_argument("--tool-revision", help="Garak commit that ran the bundle")
    parse_cmd.add_argument("--adapter-revision", help="consumer commit of this adapter")
    parse_cmd.set_defaults(handler=_parse)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


def _compile(args: argparse.Namespace) -> int:
    try:
        compile(args.package, args.out)
    except CapabilityGap as gap:
        print(json.dumps(gap.record, indent=2, sort_keys=True))
        return EXIT_CAPABILITY_GAP
    except CompileError as exc:
        print(f"compile: {exc}", file=sys.stderr)
        return EXIT_INPUT
    return EXIT_OK


# The options of the form that prepares the package; none may go with --values.
PREPARED_OPTIONS = ("package", "target", "port", "run")
PREPARED_REQUIRED = ("package", "target", "run")


def _instantiate(args: argparse.Namespace) -> int:
    given = [f"--{name}" for name in PREPARED_OPTIONS if getattr(args, name) is not None]
    if args.values is not None:
        if given:
            args.usage_error(
                f"--values (the legacy form, values already resolved) cannot be combined "
                f"with {', '.join(given)}"
            )
        return _instantiate_resolved(args)
    missing = [f"--{name}" for name in PREPARED_REQUIRED if getattr(args, name) is None]
    if missing:
        args.usage_error(
            "give either --package, --target and --run, or, in the legacy form, --values; "
            f"missing {', '.join(missing)}"
        )
    return _instantiate_prepared(args)


def _instantiate_resolved(args: argparse.Namespace) -> int:
    try:
        values = json.loads(args.values.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"instantiate: values are unreadable: {exc}", file=sys.stderr)
        return EXIT_INPUT
    try:
        instantiate_resolved(args.template, values, args.out)
    except InstantiateError as exc:
        print(f"instantiate: {exc}", file=sys.stderr)
        return EXIT_INPUT
    return EXIT_OK


def _instantiate_prepared(args: argparse.Namespace) -> int:
    # The target config is checked before anything else is read.
    try:
        target = authorized_target(args.target, InstantiateError, port=args.port)
    except InstantiateError as exc:
        print(f"instantiate: {exc}", file=sys.stderr)
        return EXIT_INPUT
    try:
        run = json.loads(args.run.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"instantiate: run settings are unreadable: {exc}", file=sys.stderr)
        return EXIT_INPUT
    try:
        result = instantiate(args.package, args.template, args.out, target=target, run=run)
    except InstantiateError as exc:
        print(f"instantiate: {exc}", file=sys.stderr)
        return EXIT_INPUT
    if isinstance(result, PreparationIncomplete):
        print(json.dumps({"status": "incomplete", **asdict(result)}, indent=2, sort_keys=True))
        return EXIT_INCOMPLETE
    record = {"status": "prepared", **asdict(result.preparation)}
    print(json.dumps(record, indent=2, sort_keys=True))
    return EXIT_OK


def _parse(args: argparse.Namespace) -> int:
    try:
        parse(
            args.bundle,
            args.out,
            report=args.report,
            records_dir=args.records,
            tool_revision=args.tool_revision,
            adapter_revision=args.adapter_revision,
        )
    except ParseError as exc:
        print(f"parse: {exc}", file=sys.stderr)
        return EXIT_INPUT
    return EXIT_OK
