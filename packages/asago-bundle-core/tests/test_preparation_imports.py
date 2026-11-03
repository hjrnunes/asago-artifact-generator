"""Package preparation reads the package library, not the authoring application.

``prepare_package`` runs inside the adapters, downstream of authoring. Its admission
step therefore imports the binding rules and the package values from ``asago_package``,
and it must not name the authoring application at all.
"""

from __future__ import annotations

from pathlib import Path

import asago_bundle_core.preparation as preparation
import asago_package.binding_rules as package_binding_rules
import asago_package.package_io as package_package_io


def test_preparation_imports_no_authoring_application_module() -> None:
    source = Path(preparation.__file__).read_text(encoding="utf-8")

    assert "asago_artifact_generator" not in source


def test_preparation_uses_the_package_librarys_binding_rules_and_package() -> None:
    assert (
        preparation.validate_binding_declarations
        is package_binding_rules.validate_binding_declarations
    )
    assert preparation.ArtifactPackage is package_package_io.ArtifactPackage
