"""Strict STPA execution-bundle ingestion."""

from .loader import (
    BUNDLE_SCHEMA_VERSION,
    PROJECTION_SCHEMA_VERSION,
    BundleValidationError,
    LoaderLimits,
    ValidationViolation,
    VerifiedExecutionBundle,
    VerifiedExecutionBundleEntry,
    load_execution_bundle,
    load_execution_bundle_with_limits,
)

__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "BundleValidationError",
    "LoaderLimits",
    "PROJECTION_SCHEMA_VERSION",
    "ValidationViolation",
    "VerifiedExecutionBundle",
    "VerifiedExecutionBundleEntry",
    "load_execution_bundle",
    "load_execution_bundle_with_limits",
]
