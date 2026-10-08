"""Command line for the Garak adapter.

Exit codes: 0 done, 1 the input cannot be used, 2 usage error, 3 capability
gap (``compile`` only: Garak cannot deliver the package yet).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .compiler import CapabilityGap, CompileError, compile_package

EXIT_OK = 0
EXIT_INPUT = 1
EXIT_CAPABILITY_GAP = 3


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
