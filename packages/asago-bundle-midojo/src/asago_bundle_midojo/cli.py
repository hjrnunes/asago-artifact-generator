"""Command line for the MiDojo adapter.

Exit codes: 0 done, 1 the input cannot be used, 2 usage error, 3 capability
gap (``compile`` only: MiDojo cannot deliver the package, such as a sequential
one or one that claims the reply).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from asago_bundle_core.gap import EXIT_CAPABILITY_GAP, EXIT_INPUT, EXIT_OK, CapabilityGap

from .compiler import CompileError, compile_package
from .instantiate import InstantiateError, instantiate_bundle
from .parse import ParseError, parse_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asago-bundle-midojo",
        description="Compile a package into a MiDojo bundle and parse its control-plane records.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    compile_cmd = commands.add_parser(
        "compile", help="write a bundle template and bundle.json for one package"
    )
    compile_cmd.add_argument("package", type=Path, help="artifact package directory")
    compile_cmd.add_argument("--out", type=Path, required=True, help="new template directory")
    compile_cmd.set_defaults(handler=_compile)
    instantiate_cmd = commands.add_parser(
        "instantiate", help="fill a bundle template with values.json into a concrete bundle"
    )
    instantiate_cmd.add_argument("template", type=Path, help="bundle template directory")
    instantiate_cmd.add_argument("--values", type=Path, required=True, help="values.json")
    instantiate_cmd.add_argument("--out", type=Path, required=True, help="new bundle directory")
    instantiate_cmd.set_defaults(handler=_instantiate)
    parse_cmd = commands.add_parser(
        "parse",
        help="write execution-receipt-v1 from the control-plane records; a failed run exits 0",
    )
    parse_cmd.add_argument("bundle", type=Path, help="concrete bundle directory")
    parse_cmd.add_argument("--out", type=Path, required=True, help="receipt path")
    parse_cmd.add_argument(
        "--records", type=Path, help="package output directory holding boundary records"
    )
    parse_cmd.add_argument("--tool-revision", help="MiDojo commit that ran the bundle")
    parse_cmd.add_argument("--adapter-revision", help="consumer commit of this adapter")
    parse_cmd.set_defaults(handler=_parse)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


def _compile(args: argparse.Namespace) -> int:
    try:
        compile_package(args.package, args.out)
    except CapabilityGap as gap:
        print(json.dumps(gap.record, indent=2, sort_keys=True))
        return EXIT_CAPABILITY_GAP
    except CompileError as exc:
        print(f"compile: {exc}", file=sys.stderr)
        return EXIT_INPUT
    return EXIT_OK


def _instantiate(args: argparse.Namespace) -> int:
    try:
        values = json.loads(args.values.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"instantiate: values are unreadable: {exc}", file=sys.stderr)
        return EXIT_INPUT
    try:
        instantiate_bundle(args.template, values, args.out)
    except InstantiateError as exc:
        print(f"instantiate: {exc}", file=sys.stderr)
        return EXIT_INPUT
    return EXIT_OK


def _parse(args: argparse.Namespace) -> int:
    try:
        parse_bundle(
            args.bundle,
            args.out,
            records_dir=args.records,
            tool_revision=args.tool_revision,
            adapter_revision=args.adapter_revision,
        )
    except ParseError as exc:
        print(f"parse: {exc}", file=sys.stderr)
        return EXIT_INPUT
    return EXIT_OK
