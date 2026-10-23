"""Write ``package/`` with the package writer of c18e46d, which copied each request into it.

usage: C=<checkout of c18e46d>
       uv run python generate.py "$C"

Everything runs with that checkout's code, which the script checks: its scripted
orchestrator authors the refund world's command-attempt scenario with both reviews
(call1, plan_review, call2, artifact_review), so the package holds the copies the
current writer no longer writes: ``authoring/NN-<stage>.json``, ``.prompt`` and
``.raw``, ``authoring/ledger.json``, ``reviews.json`` and ``transformations.json``,
and ``source-hashes.json``. ``package/`` must not exist yet. To regenerate, make the
checkout with ``git worktree add --detach "$(mktemp -d)/c18e46d" c18e46d``.

pytest does not collect this file (its name has no ``test_`` prefix).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REVISION = "c18e46d"
HERE = Path(__file__).resolve().parent


def main(checkout: Path) -> None:
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not head.startswith(REVISION):
        raise SystemExit(f"{checkout} is at {head}, not {REVISION}")
    sys.path[:0] = [str(checkout / "src"), str(checkout)]

    from tests.support import review_response, scripted_orchestrator, world_builders

    import asago_artifact_generator
    from asago_artifact_generator.authoring.policy import AuthoringPolicy

    if not Path(asago_artifact_generator.__file__).is_relative_to(checkout):
        raise SystemExit(f"imported {asago_artifact_generator.__file__}, not the checkout's")
    framed, inventory, plan, runtime_contract, view = world_builders(
        "refund", "framed", "inventory", "plan", "runtime_contract", "view"
    )
    work = Path(tempfile.mkdtemp())
    orchestrator, _ = scripted_orchestrator(
        work,
        [json.dumps(plan()), review_response(), framed(), review_response()],
        task_id="SCN-901",
        policy=AuthoringPolicy(),
    )
    result = orchestrator.run(view(), inventory(), runtime_contract())
    if result.status != "accepted":
        raise SystemExit(f"the scripted run ended {result.status}")
    shutil.copytree(work / "package", HERE / "package")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
