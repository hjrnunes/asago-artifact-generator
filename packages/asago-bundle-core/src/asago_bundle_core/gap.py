"""Capability gaps: a tool cannot deliver a package, and the orchestrator reads the record.

Exit codes: 0 done, 1 the input cannot be used, 2 usage error, 3 capability gap
(``compile`` only), 4 the package could not be prepared for the run (``instantiate``
only, for an adapter whose ``instantiate`` prepares the package).
"""

from __future__ import annotations

from typing import Any

from asago_artifact_generator.package_io import ArtifactPackage

EXIT_OK = 0
EXIT_INPUT = 1
EXIT_CAPABILITY_GAP = 3
EXIT_INCOMPLETE = 4


class CapabilityGap(Exception):
    """The tool cannot run the package yet; ``record`` says why."""

    def __init__(self, record: dict[str, Any]) -> None:
        super().__init__(record["reason"])
        self.record = record


def capability_gap_record(
    tool: str, package: ArtifactPackage, delivery: str, reason: str
) -> dict[str, Any]:
    return {
        "kind": "capability_gap",
        "tool": tool,
        "package_id": package.manifest.package_id,
        "scenario_id": package.manifest.scenario_id,
        "package_digest": package.manifest.manifest_digest,
        "delivery": delivery,
        "reason": reason,
    }
