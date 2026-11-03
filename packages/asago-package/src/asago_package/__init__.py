"""The artifact-package-v5 values, the contained writer and the strict reader.

This library declares only pydantic and jsonschema, and it imports nothing from the
authoring application, so a downstream tool can read or write a package without the
authoring dependencies. It also carries the three authority contract kits the values
verify: ``artifact-package``, ``runtime-contract`` and ``capability-gap``.
"""

from __future__ import annotations

from .package_io import (
    PACKAGE_SCHEMA_VERSION,
    ArtifactPackage,
    PackageIntegrityError,
    PackageManifest,
    PackagePathError,
    build_package,
    load_package,
    validate_artifact_package_contract,
    write_package,
)

__all__ = [
    "PACKAGE_SCHEMA_VERSION",
    "ArtifactPackage",
    "PackageIntegrityError",
    "PackageManifest",
    "PackagePathError",
    "build_package",
    "load_package",
    "validate_artifact_package_contract",
    "write_package",
]
