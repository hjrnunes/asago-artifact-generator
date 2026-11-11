"""Command line for the Garak adapter.

Exit codes: 0 done, 1 the input cannot be used (including a target config
not authorized for a run), 2 usage error, 3 capability gap (``compile`` only:
Garak cannot deliver the package, such as a sequential history holding a
message other than a user turn, or a planted item the episode compiler cannot
place), 4 the package could not be prepared
(``instantiate`` only). ``instantiate`` prints its preparation as JSON on
stdout: the setup ledger, bindings and prerequisites, or the incomplete reason.
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
from asago_injection_spec import FINAL_TURN, JUDGE_TURNS

from .compiler import CompileError
from .entry_points import compile, instantiate, parse
from .instantiate import InstantiateError
from .judge_service import (
    DEFAULT_PORT,
    MODEL_ENV,
    MODEL_URL_ENV,
    SERVICE_TOKEN_ENV,
    ServiceConfigError,
    serve,
)
from .report_parse import ParseError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asago-bundle-garak",
        description="Compile a Garak bundle, parse its report, or serve the reply judge.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    compile_cmd = commands.add_parser(
        "compile", help="write a bundle template and bundle.json for one package"
    )
    compile_cmd.add_argument("package", type=Path, help="artifact package directory")
    compile_cmd.add_argument("--out", type=Path, required=True, help="new template directory")
    compile_cmd.add_argument(
        "--target",
        type=Path,
        help=f"the target's target.yaml, required for a planted_item package; its "
        f"{AUTHORIZATION_FIELD} must be one of {', '.join(AUTHORIZED_KINDS)}",
    )
    compile_cmd.add_argument(
        "--judge-turns",
        choices=sorted(JUDGE_TURNS),
        default=FINAL_TURN,
        help="the turns a reply episode's judge grades (default: %(default)s)",
    )
    compile_cmd.set_defaults(handler=_compile)
    instantiate_cmd = commands.add_parser(
        "instantiate",
        help="prepare the package against the target (its setup makes real writes) and fill "
        "the bundle template into a concrete bundle",
        description="Prepare the package against the target and fill the template; give "
        "--port when the endpoint URLs hold {port}.",
    )
    instantiate_cmd.add_argument("template", type=Path, help="bundle template directory")
    instantiate_cmd.add_argument(
        "--package", type=Path, required=True, help="the artifact package the template is for"
    )
    instantiate_cmd.add_argument(
        "--target",
        type=Path,
        required=True,
        help=f"the target's target.yaml; its {AUTHORIZATION_FIELD} must be one of "
        f"{', '.join(AUTHORIZED_KINDS)}",
    )
    instantiate_cmd.add_argument(
        "--port", type=int, help="fills {port} in the target's endpoint URLs"
    )
    instantiate_cmd.add_argument(
        "--run",
        type=Path,
        required=True,
        help="run settings JSON: gateway_url, model, and optionally judge_url, judge_model, "
        "judge_turns, mcp_url, allowed_tools",
    )
    instantiate_cmd.add_argument("--out", type=Path, required=True, help="new bundle directory")
    instantiate_cmd.set_defaults(handler=_instantiate)
    parse_cmd = commands.add_parser(
        "parse",
        help="write execution-receipt-v2 from Garak's report; a failed run still exits 0",
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
    serve_cmd = commands.add_parser(
        "judge-serve",
        help="serve midojo.judge.v1 on 127.0.0.1 for a reply episode's reply_judge check",
        description=f"Reads the model's base URL from {MODEL_URL_ENV}, the model from "
        f"{MODEL_ENV} and its key from OPENAICOMPATIBLE_API_KEY, and requires the bearer "
        f"token in {SERVICE_TOKEN_ENV} on every request.",
    )
    serve_cmd.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="port on 127.0.0.1; 0 takes a free one"
    )
    serve_cmd.set_defaults(handler=_judge_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


def _compile(args: argparse.Namespace) -> int:
    try:
        compile(
            args.package,
            args.out,
            target=args.target,
            judge_turns=args.judge_turns,
        )
    except CapabilityGap as gap:
        print(json.dumps(gap.record, indent=2, sort_keys=True))
        return EXIT_CAPABILITY_GAP
    except CompileError as exc:
        print(f"compile: {exc}", file=sys.stderr)
        return EXIT_INPUT
    return EXIT_OK


def _instantiate(args: argparse.Namespace) -> int:
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


def _judge_serve(args: argparse.Namespace) -> int:
    try:
        serve(args.port)
    except ServiceConfigError as exc:
        print(f"judge-serve: {exc}", file=sys.stderr)
        return EXIT_INPUT
    return EXIT_OK
