"""The artifact-package-v5 writer and reader, re-exported from the ``asago-package`` member.

The implementation, its values and the three authority contract kits it verifies live in
``packages/asago-package``. This module keeps the consumer's import path working and binds
the same objects, so ``asago_artifact_generator.package_io.write_package`` and
``asago_package.package_io.write_package`` are one function.
"""

from __future__ import annotations

from asago_package.package_io import (
    JUDGE_MEMBER,
    PACKAGE_SCHEMA_VERSION,
    SEED_MEMBER,
    TOOL_CALL_CONDITION_MEMBER,
    ArtifactPackage,
    PackageIntegrityError,
    PackageManifest,
    PackagePathError,
    build_package,
    load_package,
    tool_call_condition_bytes,
    validate_artifact_package_contract,
    write_package,
)

__all__ = [
    "ArtifactPackage",
    "JUDGE_MEMBER",
    "PACKAGE_SCHEMA_VERSION",
    "PackageIntegrityError",
    "PackageManifest",
    "PackagePathError",
    "SEED_MEMBER",
    "TOOL_CALL_CONDITION_MEMBER",
    "build_package",
    "load_package",
    "tool_call_condition_bytes",
    "write_package",
    "validate_artifact_package_contract",
]
