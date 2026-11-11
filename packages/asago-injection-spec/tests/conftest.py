"""Helpers the injection-spec library's tests share; the planted packages are in
``injection_spec_cases``."""

from __future__ import annotations

import json
from pathlib import Path

from midojo_spec.injection_spec_schema import INJECTION_SPEC_DOCUMENT_V1

from asago_injection_spec.document import LEGACY_INJECTION_SPEC_SCHEMA
from asago_injection_spec.injection_spec_compile import (
    INJECTION_SPEC_FILE,
    LEGACY_EPISODE_FILE,
    TOOL_REVISION_RANGE,
)

REPO = Path(__file__).resolve().parents[3]
# The range the recorded fixtures name, compiled at 8e8208f. Compile's minimum rose to
# 2a794f3 when the document gained the ID that revision reads; a recorded fixture keeps
# its bytes (see docs/midojo-changelog.md), so the comparison normalizes the range back.
RECORDED_TOOL_REVISION_RANGE = ">=8e8208f"


def as_written(fixture: Path, package: Path) -> dict[str, bytes]:
    """One recorded fixture directory's compiled document, as compile writes it now.

    The recorded fixtures keep the document's pre-rename filename, ID and revision range
    (``episode.json``, ``midojo-episode-v1``, ``>=8e8208f``) as recorded;
    compile writes ``injection-spec.json`` holding ``midojo-injection-spec-v1`` and names
    the first revision that reads it. A recorded document also names the digest of the
    package it was compiled from, which changes whenever the manifest's ``input_kind``
    does; the comparison swaps in the manifest digest of ``package``, the package compile
    reads. The rest of the bytes are what a new compile writes, so the comparison stays
    byte for byte.
    """

    recorded = (fixture / LEGACY_EPISODE_FILE).read_bytes()
    recorded_digest = json.loads(recorded)["package_digest"]
    package_digest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))[
        "manifest_digest"
    ]
    return {
        INJECTION_SPEC_FILE: recorded.replace(
            LEGACY_INJECTION_SPEC_SCHEMA.encode(), INJECTION_SPEC_DOCUMENT_V1.encode()
        )
        .replace(RECORDED_TOOL_REVISION_RANGE.encode(), TOOL_REVISION_RANGE.encode())
        .replace(recorded_digest.encode(), package_digest.encode())
    }
