"""Every in-memory package builder in this suite publishes through the package writer.

``build_package`` checks the package values and the secret policy; the manifest schema runs in
``write_package``. A helper that stops at ``build_package`` can therefore carry a field the
writer refuses while every test still passes: a review found an authoring
``{attempts, max_retries}`` block that way. Each builder whose package stands for a package
the pipeline publishes goes through the writer here. A builder that deliberately assembles a
partial package for a reader that accepts partials is not in this list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from asago_package.package_io import load_package, write_package
from tests.test_artifact_package_cases import _POLICY, _package_with


@pytest.mark.parametrize("entry", _POLICY["allowed"], ids=lambda entry: entry["name"])
def test_every_allowed_metadata_case_publishes_and_reloads(
    tmp_path: Path, entry: dict[str, Any]
) -> None:
    package = _package_with(entry["metadata"])

    written = write_package(tmp_path / entry["name"], package)

    assert load_package(written).manifest.to_dict() == package.manifest.to_dict()
