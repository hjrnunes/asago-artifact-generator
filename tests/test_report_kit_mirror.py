"""report_kit: the byte-for-byte mirror of orch's report kit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from asago_artifact_generator import report_kit as kit

KIT_DIR = Path(kit.__file__).parent
REGENERATE = (
    'uv run python -c "from asago_artifact_generator.report_kit import '
    'write_catalog; write_catalog()"'
)


def test_the_catalog_matches_its_golden_file() -> None:
    golden = (KIT_DIR / "catalog.html").read_text(encoding="utf-8")
    assert golden == kit.render_catalog(), (
        f"catalog.html differs from the kit; copy the kit from orch (or run {REGENERATE})"
    )


def test_every_file_matches_the_digest_in_the_lock() -> None:
    lock = json.loads((KIT_DIR / "CONTRACT.lock").read_text(encoding="utf-8"))
    actual = {
        name: hashlib.sha256((KIT_DIR / name).read_bytes()).hexdigest() for name in lock["files"]
    }
    assert actual == lock["files"]
    assert lock["authority"] == "asago-orch"
    assert lock["contract"] == "report-kit"
