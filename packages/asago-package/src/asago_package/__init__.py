"""The artifact-package-v5 values, the contained writer and the strict reader.

This library declares only pydantic and jsonschema, and it imports nothing from the
authoring application, so a downstream tool can read or write a package without the
authoring dependencies. It also carries the three authority contract kits the values
verify: ``artifact-package``, ``runtime-contract`` and ``capability-gap``.

:mod:`asago_package.binding_rules` owns the binding and slot rules, and
:mod:`asago_package.bindings` adds authoring's canonical paths and wording, so package
preparation and the adapters validate declarations from the same engine.
The tool-call-condition-v2 validator and evaluator come from ``asago_handoff``, the
producer's package, so this library stays free of the condition rules.
"""

from __future__ import annotations

from .package_io import (
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
    validate_artifact_package_contract,
    write_package,
)

__all__ = [
    "JUDGE_MEMBER",
    "PACKAGE_SCHEMA_VERSION",
    "SEED_MEMBER",
    "TOOL_CALL_CONDITION_MEMBER",
    "ArtifactPackage",
    "PackageIntegrityError",
    "PackageManifest",
    "PackagePathError",
    "build_package",
    "load_package",
    "validate_artifact_package_contract",
    "write_package",
]
