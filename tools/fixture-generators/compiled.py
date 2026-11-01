"""Find the document a compiled case directory holds, under either name.

Compile writes ``injection-spec.json``. The recorded fixtures under ``tests/fixtures/``
keep ``episode.json``, the name compile wrote before the rename, until M9 regenerates
them, and a regeneration starts from a directory compile just wrote. The generators take
either so that both layouts work.
"""

from __future__ import annotations

from pathlib import Path

# The name compile writes, then the name a recorded fixture keeps until M9.
NAMES = ("injection-spec.json", "episode.json")


def find_document_path(case_dir: Path) -> Path | None:
    """Return ``case_dir``'s compiled document under the first name it holds, or None."""

    for name in NAMES:
        if (case_dir / name).is_file():
            return case_dir / name
    return None


def document_path(case_dir: Path) -> Path:
    """Return ``case_dir``'s compiled document, under the first name it holds."""

    path = find_document_path(case_dir)
    if path is None:
        raise SystemExit(f"{case_dir} holds neither {NAMES[0]} nor {NAMES[1]}")
    return path
