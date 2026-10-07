"""Each stage's rendered prompt matches its golden file, and a changed text carries a new version.

A failure here means a prompt changed. Bump the stage's prompt version in
``authoring/core.py``, then run ``uv run python -m tests.golden_prompts`` to rewrite the files.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from . import golden_prompts
from .golden_prompts import (
    CASE_STAGES,
    GOLDEN_DIR,
    changed_stages,
    golden_path,
    golden_text,
    read_manifest,
    rendered_packets,
    stage_versions,
    text_diff,
)

_REFRESH = "run `uv run python -m tests.golden_prompts` after a deliberate prompt change"

_CASE_STAGE_PAIRS = [(case, stage) for case, stages in CASE_STAGES.items() for stage in stages]


@pytest.mark.parametrize(("case", "stage"), _CASE_STAGE_PAIRS)
def test_the_rendered_prompt_matches_its_golden_file(case: str, stage: str) -> None:
    diff = text_diff(case, stage)

    assert diff == "", f"{stage} prompt differs from its golden file; {_REFRESH}\n{diff}"


def test_a_stage_whose_text_changed_carries_a_new_prompt_version() -> None:
    manifest = read_manifest()
    versions = stage_versions()

    unversioned = {
        stage: versions[stage]
        for stage in changed_stages()
        if manifest.get(stage) == versions[stage]
    }

    assert unversioned == {}, (
        f"the text of these stages changed but their prompt version did not: {unversioned}"
    )


def test_the_manifest_lists_the_version_each_stage_renders() -> None:
    assert read_manifest() == stage_versions(), _REFRESH


def test_the_golden_directory_holds_only_the_files_the_cases_name() -> None:
    expected = {golden_path(case, stage) for case, stage in _CASE_STAGE_PAIRS}

    assert set(GOLDEN_DIR.rglob("*.txt")) == expected


@pytest.fixture
def golden_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the golden helpers at a copy that a test may edit."""

    root = tmp_path / "prompts"
    shutil.copytree(GOLDEN_DIR, root)
    monkeypatch.setattr(golden_prompts, "GOLDEN_DIR", root)
    monkeypatch.setattr(golden_prompts, "MANIFEST_PATH", root / "manifest.json")
    return root


def _edit_golden(root: Path, case: str, stage: str) -> Path:
    path = root / case / f"{stage}.txt"
    path.write_text(path.read_text(encoding="utf-8").replace("Feature:", "Feat:", 1), "utf-8")
    return path


def test_a_changed_golden_line_is_reported_at_the_words_that_differ(golden_copy: Path) -> None:
    _edit_golden(golden_copy, "ehr", "call1")

    diff = text_diff("ehr", "call1")

    assert "- " in diff
    assert "Feat:" in diff.split("\n+ ")[0]
    assert "Feature:" in diff.split("\n+ ")[1]
    assert len(diff) < 2_000


def test_refreshing_refuses_a_text_change_under_an_unchanged_version(golden_copy: Path) -> None:
    path = _edit_golden(golden_copy, "ehr", "call1")
    edited = path.read_text(encoding="utf-8")

    assert golden_prompts.refresh() == 1
    assert path.read_text(encoding="utf-8") == edited


def test_refreshing_rewrites_a_stage_whose_version_changed(golden_copy: Path) -> None:
    path = _edit_golden(golden_copy, "ehr", "call1")
    manifest_path = golden_copy / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["call1"] = "authoring-call1-v0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert golden_prompts.refresh() == 0
    assert path.read_text(encoding="utf-8") == golden_text(rendered_packets("ehr")["call1"])
    assert read_manifest() == stage_versions()


def test_refreshing_accepts_a_test_world_change_on_request(golden_copy: Path) -> None:
    path = _edit_golden(golden_copy, "ehr", "call1")

    assert golden_prompts.refresh(allow_unversioned=True) == 0
    assert path.read_text(encoding="utf-8") == golden_text(rendered_packets("ehr")["call1"])
