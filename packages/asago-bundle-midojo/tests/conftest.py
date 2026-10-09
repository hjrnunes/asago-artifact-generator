"""Fixtures over the shared package builders in ``asago_bundle_core.testing``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from asago_bundle_core.testing import sequential_stimulus, write_test_package

REPO = Path(__file__).resolve().parents[3]
RECEIPT_SCHEMA = REPO / "contracts/execution-receipt/execution-receipt-v1.schema.json"
PROMPT = "Show the referral for PAT-201."
VALUES: dict[str, Any] = {
    "gateway_url": "http://127.0.0.1:18997/v1",
    "mcp_url": "http://127.0.0.1:18996/sse",
    "model": "fixture-model",
    "messages": [{"role": "user", "content": PROMPT}],
    "service_port": 18123,
}


@pytest.fixture
def command_package(tmp_path: Path) -> Path:
    return write_test_package(tmp_path / "packages")


@pytest.fixture
def reply_package(tmp_path: Path) -> Path:
    return write_test_package(tmp_path / "packages", claim_level="reply")


@pytest.fixture
def sequential_package(tmp_path: Path) -> Path:
    return write_test_package(tmp_path / "packages", stimulus=sequential_stimulus())


@pytest.fixture
def template(command_package: Path, tmp_path: Path) -> Path:
    from asago_bundle_midojo.compiler import compile_package

    out = tmp_path / "template"
    compile_package(command_package, out)
    return out


@pytest.fixture
def bundle(template: Path, tmp_path: Path) -> Path:
    from asago_bundle_midojo.instantiate import instantiate_bundle

    out = tmp_path / "bundle"
    instantiate_bundle(template, VALUES, out)
    return out
