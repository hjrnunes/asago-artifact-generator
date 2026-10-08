"""Command line for the Garak adapter."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="asago-bundle-garak",
        description="Compile a package into a Garak bundle and parse Garak's report.",
    )


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0
