"""The episodes behind ``fixtures/placed-a19562e/``: what MiDojo seals of a placement.

From a19562e MiDojo seals, with each placement (``applied``, ``partial``, ``fallback``), what
it placed and where: ``payload_digest``, ``placed_digest`` and ``slot``. The cases cover a
list slot reached through a record selector (``note``, the fixture target's
``piggybank.note.body``, compiled as ``episode_cases.py``'s ``command-single``) and a
``result_record`` slot (``referral``, the clinic target's ``clinic.referral.reason``,
compiled as ``result_record_cases.py``'s ``referral``), each with a static plan and with a
plan that may generate payloads (``note-generated``, ``referral-generated``): the compiled
episode with ``generator_permissions`` naming the carrier, the slot's field and its mode,
which compile never writes, so those two documents are derived here. Each scenario
directory holds a snapshot and its verdict that MiDojo sealed and graded at asago/main
a19562e (``fixtures/placed-a19562e/generate.py``), its host interceptor in front of a
scripted upstream of the targets' shape in MiDojo's process, not a target, and the
``delivery.json`` a runner writes from them. For a generated case the script stands in for
the generator: the interceptor is given the plan with each tool-output payload replaced, its
targets as compiled. To regenerate, with a MiDojo export or checkout at a19562e
(``uv sync --frozen --extra dev`` there first)::

    uv run python packages/asago-bundle-midojo/tests/placed_cases.py <empty dir>
    cd <that MiDojo tree> && uv run --frozen --extra dev python \\
        <consumer>/packages/asago-bundle-midojo/tests/fixtures/placed-a19562e/generate.py \\
        <the full a19562e SHA> <that dir>

then copy ``<that dir>`` over ``fixtures/placed-a19562e/`` (``generate.py`` stays).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from episode_cases import FIXTURES, compile_case
from result_record_cases import compile_clinic_case

from asago_bundle_core.text import canonical_text

PLACED = FIXTURES / "placed-a19562e"
# Each generated case: the static case it derives from, and the generator permissions it adds.
GENERATED: dict[str, tuple[str, dict[str, Any]]] = {
    "note-generated": (
        "note",
        {"tools": ["list_notes"], "fields": [["notes", "body"]], "modes": ["append"]},
    ),
    "referral-generated": (
        "referral",
        {"tools": ["get_referral"], "fields": [["reason"]], "modes": ["append"]},
    ),
}


def write_cases(out: Path) -> None:
    """Compile the static cases into ``out`` and derive the generated ones from them."""

    with tempfile.TemporaryDirectory() as packages:
        compile_case(Path(packages), "command-single", out / "note")
        compile_clinic_case(Path(packages), "referral", out / "referral")
    for case, (base, permissions) in GENERATED.items():
        document = json.loads((out / base / "episode.json").read_text(encoding="utf-8"))
        document["episode"]["plan"]["generator_permissions"] = permissions
        (out / case).mkdir(parents=True)
        (out / case / "episode.json").write_text(canonical_text(document), encoding="utf-8")


if __name__ == "__main__":
    write_cases(Path(sys.argv[1]))
