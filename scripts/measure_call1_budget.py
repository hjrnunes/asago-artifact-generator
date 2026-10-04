"""Measure the call1 prompt against the authoring context budget without a model call.

Usage:
    uv run python scripts/measure_call1_budget.py \
        --target-profile PROFILE.json --runtime-contract CONTRACT.json \
        [--target-observations RUNTIME_CONTEXT.json] [--sections] SCENARIO.yaml...

The script renders the call1 packet exactly as ``generate`` does and applies the
same estimator as the pre-dispatch context guard. It prints one tab-separated
row per scenario: id, estimated tokens, remaining input budget, headroom.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from asago_artifact_generator.authoring.context_budget import _context_budget_estimate
from asago_artifact_generator.authoring.core import (
    _CONTEXT_FRAMING_TOKEN_RESERVE,
    AUTHORING_CONTEXT_WINDOW_TOKENS,
    AUTHORING_MAX_COMPLETION_TOKENS,
)
from asago_artifact_generator.authoring.prompt_packets import build_call1_packet_v2
from asago_artifact_generator.input_adapter import load_input
from asago_artifact_generator.target_inputs import load_target_inputs


def _load_mapping(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _section_sizes(user: str) -> list[tuple[str, int]]:
    sizes: dict[str, int] = {}
    title = "(preamble)"
    for line in user.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped and stripped.isupper() and not stripped.startswith((" ", "{", '"')):
            title = stripped
        sizes[title] = sizes.get(title, 0) + len(line.encode("utf-8"))
    return sorted(sizes.items(), key=lambda item: -item[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenarios", nargs="+", type=Path)
    parser.add_argument("--target-profile", type=Path, required=True)
    parser.add_argument("--target-observations", type=Path)
    parser.add_argument("--runtime-contract", type=Path, required=True)
    parser.add_argument("--sections", action="store_true", help="Print per-section bytes.")
    args = parser.parse_args(argv)

    inventory, _ = load_target_inputs(args.target_profile, args.target_observations)
    runtime_contract = _load_mapping(args.runtime_contract)
    remaining = (
        AUTHORING_CONTEXT_WINDOW_TOKENS
        - AUTHORING_MAX_COMPLETION_TOKENS
        - _CONTEXT_FRAMING_TOKEN_RESERVE
    )
    print("scenario\tschema\testimated_prompt_tokens\tremaining_input_budget\theadroom")
    for path in args.scenarios:
        view = load_input(path)
        packet = build_call1_packet_v2(view, inventory, runtime_contract)
        estimate = _context_budget_estimate(packet)
        tokens = int(estimate["estimated_prompt_tokens"])
        flag = "  OVER" if tokens > remaining else ""
        print(
            f"{view.scenario_id}\t{view.handoff_schema_version}\t{tokens}\t"
            f"{remaining}\t{remaining - tokens}{flag}"
        )
        if args.sections:
            print(json.dumps(_section_sizes(packet.user)), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
