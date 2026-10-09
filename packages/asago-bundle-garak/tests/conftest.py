"""Fixtures over the shared package builders in ``asago_bundle_core.testing``."""

from __future__ import annotations

from pathlib import Path

import pytest

from asago_bundle_core.testing import sequential_stimulus, write_test_package


@pytest.fixture
def command_package(tmp_path: Path) -> Path:
    return write_test_package(tmp_path / "packages")


@pytest.fixture
def reply_package(tmp_path: Path) -> Path:
    return write_test_package(tmp_path / "packages", claim_level="reply")


@pytest.fixture
def sequential_package(tmp_path: Path) -> Path:
    return write_test_package(tmp_path / "packages", stimulus=sequential_stimulus())
