"""Command line for the MiDojo adapter.

Exit codes: 0 done, 1 the input cannot be used, 2 usage error, 3 capability gap
(``compile`` only: MiDojo cannot run the package, such as a direct or user-turn package,
a single one with history, or one that claims a state effect; the record is printed).

``compile`` writes one package's episode; the runner opens it with MiDojo, seals it and
grades the snapshot; ``parse`` reads the sealed snapshot and the verdict into a receipt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from asago_bundle_core.gap import EXIT_CAPABILITY_GAP, EXIT_INPUT, EXIT_OK, CapabilityGap

from .bundle_files.asago_episode_judge import FINAL_TURN, JUDGE_TURNS
from .episode_compile import CompileError, compile_episode
from .route import DEFAULT_ROUTE, ROUTES
from .sealed import ParseError, parse_sealed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asago-bundle-midojo",
        description="Compile a planted_item package into a MiDojo episode and parse its "
        "sealed result.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    compile_cmd = commands.add_parser(
        "compile", help="write episode.json (and a reply's verifiers) for one package"
    )
    compile_cmd.add_argument("package", type=Path, help="artifact package directory")
    compile_cmd.add_argument("--out", type=Path, required=True, help="new episode directory")
    compile_cmd.add_argument(
        "--midojo-base", type=Path, required=True, help="mini-agents-midojo-base-v1 file"
    )
    compile_cmd.add_argument("--domain", required=True, help="domain of the base to compile for")
    compile_cmd.add_argument(
        "--judge-turns",
        choices=sorted(JUDGE_TURNS),
        default=FINAL_TURN,
        help="the turns a reply's judge grades",
    )
    compile_cmd.set_defaults(handler=_compile)
    parse_cmd = commands.add_parser(
        "parse",
        help="write execution-receipt-v1 from a sealed snapshot and its verdict; a failed "
        "run exits 0",
    )
    parse_cmd.add_argument("episode", type=Path, help="the directory compile wrote")
    parse_cmd.add_argument(
        "--snapshot", type=Path, required=True, help="the sealed snapshot (GET .../snapshot)"
    )
    parse_cmd.add_argument(
        "--verdict", type=Path, required=True, help="midojo.grade's verdict of that snapshot"
    )
    parse_cmd.add_argument("--out", type=Path, required=True, help="receipt path")
    parse_cmd.add_argument(
        "--route",
        choices=ROUTES,
        default=DEFAULT_ROUTE,
        help="the client that drove this run: midojo (MiDojo's own attacker loop)",
    )
    parse_cmd.add_argument("--tool-revision", help="MiDojo commit that ran the episode")
    parse_cmd.add_argument("--adapter-revision", help="consumer commit of this adapter")
    parse_cmd.set_defaults(handler=_parse)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


def _compile(args: argparse.Namespace) -> int:
    try:
        compile_episode(
            args.package,
            args.out,
            midojo_base=args.midojo_base,
            domain=args.domain,
            judge_turns=args.judge_turns,
        )
    except CapabilityGap as gap:
        print(json.dumps(gap.record, indent=2, sort_keys=True))
        return EXIT_CAPABILITY_GAP
    except CompileError as exc:
        print(f"compile: {exc}", file=sys.stderr)
        return EXIT_INPUT
    return EXIT_OK


def _parse(args: argparse.Namespace) -> int:
    try:
        parse_sealed(
            args.episode,
            args.snapshot,
            args.verdict,
            args.out,
            tool_revision=args.tool_revision,
            adapter_revision=args.adapter_revision,
            route=args.route,
        )
    except ParseError as exc:
        print(f"parse: {exc}", file=sys.stderr)
        return EXIT_INPUT
    return EXIT_OK
