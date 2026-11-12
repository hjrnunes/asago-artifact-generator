"""``scripts/midojo_lock.py`` writes and checks the lock of the MiDojo vector copies."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from midojo_lock import LOCK, VECTORS, main

REVISION = "a" * 40
NEXT_REVISION = "b" * 40


def _checkout(tmp_path: Path, revision: str = REVISION) -> Path:
    """A minimal checkout: the pin, two vector copies and the lock the script wrote."""

    (tmp_path / "pyproject.toml").write_text(
        "[tool.uv.sources]\n"
        'midojo-spec = { git = "file:///midojo", subdirectory = "packages/midojo-spec", '
        f'rev = "{revision}" }}\n',
        encoding="utf-8",
    )
    vectors = tmp_path / VECTORS / "injection_spec_document"
    vectors.mkdir(parents=True)
    (vectors / "v1_single.json").write_text('{"a": 1}\n', encoding="utf-8")
    (vectors / "README.md").write_text("vectors\n", encoding="utf-8")
    assert main([], root=tmp_path) == 0
    return tmp_path


def test_the_committed_lock_is_the_one_the_pin_and_the_copies_call_for(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--check"]) == 0
    assert "verified" in capsys.readouterr().out


def test_write_records_the_pinned_revision_and_every_copy(tmp_path: Path) -> None:
    root = _checkout(tmp_path)

    lock = json.loads((root / LOCK).read_text(encoding="utf-8"))

    assert lock["source"] == f"midojo local {REVISION}"
    assert sorted(lock["files"]) == [
        "injection_spec_document/README.md",
        "injection_spec_document/v1_single.json",
    ]


def test_check_names_a_repin_the_lock_does_not_follow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _checkout(tmp_path)
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(REVISION, NEXT_REVISION), encoding="utf-8"
    )

    assert main(["--check"], root=root) == 1
    assert f"the pin calls for 'midojo local {NEXT_REVISION}'" in capsys.readouterr().out


def test_check_names_a_copy_that_changed_and_a_copy_the_lock_lacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _checkout(tmp_path)
    vectors = root / VECTORS / "injection_spec_document"
    (vectors / "v1_single.json").write_text('{"a": 2}\n', encoding="utf-8")
    (vectors / "v1_extra.json").write_text("{}\n", encoding="utf-8")

    assert main(["--check"], root=root) == 1

    out = capsys.readouterr().out
    assert "injection_spec_document/v1_single.json differs" in out
    assert "injection_spec_document/v1_extra.json is a copy the lock does not list" in out


def test_check_changes_nothing_and_write_repairs_the_lock(tmp_path: Path) -> None:
    root = _checkout(tmp_path)
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(REVISION, NEXT_REVISION), encoding="utf-8"
    )
    before = (root / LOCK).read_bytes()

    assert main(["--check"], root=root) == 1
    assert (root / LOCK).read_bytes() == before
    assert main([], root=root) == 0
    assert main(["--check"], root=root) == 0
    assert json.loads((root / LOCK).read_text(encoding="utf-8"))["source"] == (
        f"midojo local {NEXT_REVISION}"
    )
