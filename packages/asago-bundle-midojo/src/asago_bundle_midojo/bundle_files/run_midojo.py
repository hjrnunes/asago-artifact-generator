"""The bundle's run phase: run ``midojo-run``, then read the control plane into the bundle.

usage: run_midojo.py --bundle DIR --control-url URL -- <midojo-run flags>

The control plane keeps its records in memory, and the serve process stops
after this phase, so the records must reach files here. ``results.json`` is not
the verdict (it holds no security result when no probe payload exists); the
verdict is the evaluation record. The script writes ``control-plane/run.json``
and ``control-plane/evaluation.json`` under the bundle and exits with the
orchestrator's code, or 1 when the orchestrator succeeded but the records could
not be read.

Standard library only: it runs in MiDojo's interpreter.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

CONTROL_PLANE = "control-plane"
LOGS = "midojo-logs"
RESULTS = "results.json"
REQUEST_TIMEOUT_SECONDS = 30.0

Orchestrator = Callable[[str, list[str], Path], int]


def parse_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    """Return the script's options and the flags after ``--`` for ``midojo-run``."""

    arguments = list(argv)
    split = arguments.index("--") if "--" in arguments else len(arguments)
    parser = argparse.ArgumentParser(prog="run_midojo.py")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--control-url", required=True)
    return parser.parse_args(arguments[:split]), arguments[split + 1 :]


def orchestrator_command(control_url: str, flags: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", "midojo.orchestrator", "--control-url", control_url, *flags]


def child_environment(bundle: Path, environ: Mapping[str, str]) -> dict[str, str]:
    """Return ``environ`` with the bundle first on ``PYTHONPATH``, where the suite package is."""

    inherited = environ.get("PYTHONPATH")
    path = os.pathsep.join([str(bundle), inherited] if inherited else [str(bundle)])
    return {**environ, "PYTHONPATH": path}


def run_orchestrator(control_url: str, flags: list[str], bundle: Path) -> int:
    command = orchestrator_command(control_url, flags)
    return subprocess.run(
        command, env=child_environment(bundle, os.environ), check=False
    ).returncode


def fetch_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.load(response)


def results_run_id(bundle: Path) -> str | None:
    """Return the run id ``midojo-run`` recorded in ``results.json``, or None."""

    try:
        results = json.loads((bundle / LOGS / RESULTS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    run_id = results.get("run_id") if isinstance(results, dict) else None
    return run_id if isinstance(run_id, str) and run_id else None


def write_json(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def evaluation_ids(run: Any) -> list[str]:
    evaluations = run.get("evaluations") if isinstance(run, dict) else None
    ids = [item.get("id") for item in evaluations if isinstance(item, dict)] if evaluations else []
    return [item for item in ids if isinstance(item, str)]


def dump_control_plane(control_url: str, run_id: str, bundle: Path) -> bool:
    """Write the run and its one evaluation under the bundle; return whether both were written."""

    directory = bundle / CONTROL_PLANE
    run = fetch_json(f"{control_url}/runs/{run_id}")
    write_json(directory / "run.json", run)
    ids = evaluation_ids(run)
    if len(ids) != 1:
        return False
    write_json(
        directory / "evaluation.json",
        fetch_json(f"{control_url}/runs/{run_id}/evaluations/{ids[0]}"),
    )
    return True


def read_control_plane(control_url: str, bundle: Path) -> bool:
    run_id = results_run_id(bundle)
    if run_id is None:
        return False
    try:
        return dump_control_plane(control_url, run_id, bundle)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"run_midojo: control plane unreadable: {exc}", file=sys.stderr)
        return False


def main(argv: Sequence[str] | None = None, orchestrator: Orchestrator = run_orchestrator) -> int:
    options, flags = parse_args(sys.argv[1:] if argv is None else argv)
    code = orchestrator(options.control_url, flags, options.bundle)
    dumped = read_control_plane(options.control_url, options.bundle)
    return code or (0 if dumped else 1)


if __name__ == "__main__":
    sys.exit(main())
