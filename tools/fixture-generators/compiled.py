"""Find the injection-spec document in a compiled case directory."""

from __future__ import annotations

from pathlib import Path

INJECTION_SPEC_FILE = "injection-spec.json"


def find_document_path(case_dir: Path) -> Path | None:
    """Return the compiled document path if the file exists."""

    path = case_dir / INJECTION_SPEC_FILE
    return path if path.is_file() else None


def document_path(case_dir: Path) -> Path:
    """Return the compiled document path or report that the file is missing."""

    path = find_document_path(case_dir)
    if path is None:
        raise SystemExit(f"{case_dir} holds no {INJECTION_SPEC_FILE}")
    return path
