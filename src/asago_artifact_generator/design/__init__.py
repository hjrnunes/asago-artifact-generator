"""Artifact-design package: genuine consumer design from the scenario handoff."""

from .authoring import (
    ArtifactAuthor,
    DesignBrief,
    LLMArtifactAuthor,
    PreboundAuthor,
    design_artifact,
)
from .compile import (
    compile_design,
    validate_design_case,
    verify_frozen_artifact,
    write_design_outputs,
)
from .records import ArtifactDesignPlan, DesignExclusion, DesignOutcome

__all__ = [
    "ArtifactAuthor",
    "ArtifactDesignPlan",
    "DesignBrief",
    "DesignExclusion",
    "DesignOutcome",
    "LLMArtifactAuthor",
    "PreboundAuthor",
    "compile_design",
    "design_artifact",
    "validate_design_case",
    "verify_frozen_artifact",
    "write_design_outputs",
]
