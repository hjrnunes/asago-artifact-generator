"""report_kit: the consumer reads the producer's ``asago-report-kit`` member.

The kit is no longer a byte-for-byte mirror under ``src``. The consumer declares
the member by name with a version range and keeps only its path in
``[tool.uv.sources]``, so the report writers import ``asago_report_kit``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import asago_report_kit

from asago_artifact_generator.report import author_report

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "src" / "asago_artifact_generator" / "report"
MIRROR = ROOT / "src" / "asago_artifact_generator" / "report_kit"
MEMBER = ROOT.parent / "asago-scenario-generator" / "packages" / "asago-report-kit"
OLD_IMPORT = "from ..report_kit import"
MEMBER_IMPORT = "from asago_report_kit import"
KIT_FILES = (
    "__init__.py",
    "CONTRACT.lock",
    "catalog.html",
    "kit.css",
    "kit.js",
    "stage-summary-v1.schema.json",
)


def test_the_consumer_depends_on_the_member_by_name_with_a_range() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    source = project["tool"]["uv"]["sources"]["asago-report-kit"]

    assert "asago-report-kit>=0.1,<0.2" in project["project"]["dependencies"]
    assert source["editable"] is True
    assert (ROOT / source["path"]).resolve() == MEMBER.resolve()


def test_the_report_package_imports_the_member() -> None:
    offenders: list[str] = []
    users: list[str] = []
    for path in sorted(REPORT.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if OLD_IMPORT in text:
            offenders.append(path.name)
        if MEMBER_IMPORT in text:
            users.append(path.name)

    assert offenders == []
    assert users


def test_the_schema_path_comes_from_the_member() -> None:
    assert author_report.SCHEMA == asago_report_kit.stage_summary_schema()


def test_the_mirror_and_its_lock_are_gone() -> None:
    assert [name for name in KIT_FILES if (MIRROR / name).exists()] == []
