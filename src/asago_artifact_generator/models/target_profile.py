"""Stable import surface for the producer-owned execution target profile."""

from .execution_classification import (
    EXECUTION_TARGET_PROFILE_DIGEST_FRAME,
    EXECUTION_TARGET_PROFILE_SCHEMA_VERSION,
    ExecutionResourceKind,
    ExecutionSurface,
    ExecutionTargetProfile,
    InventoryCompleteness,
    ProfileAuthority,
    ProfileBasis,
    SimulationBehavior,
    TargetProfileOperation,
    TargetProfileResource,
)

__all__ = [
    "EXECUTION_TARGET_PROFILE_DIGEST_FRAME",
    "EXECUTION_TARGET_PROFILE_SCHEMA_VERSION",
    "ExecutionResourceKind",
    "ExecutionSurface",
    "ExecutionTargetProfile",
    "InventoryCompleteness",
    "ProfileAuthority",
    "ProfileBasis",
    "TargetProfileOperation",
    "TargetProfileResource",
    "SimulationBehavior",
]
