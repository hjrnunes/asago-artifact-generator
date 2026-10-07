"""Builders for example captures and the artifact checks that read them."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from asago_artifact_generator.authoring.checks import collect_artifact_findings_v2
from asago_artifact_generator.authoring.contracts import (
    neutral_artifact_plan_v2,
    neutral_artifact_response_without_source,
)
from asago_artifact_generator.authoring.core import Finding

from .support import NEUTRAL_INVENTORY

RUNTIME = {"delivery": ["direct_user_message"], "setup_permissions": []}


def call(operation: str = "inspect_record", **arguments: Any) -> dict[str, Any]:
    return {"operation": operation, "arguments": arguments}


def capture(*calls: dict[str, Any], complete: bool = True) -> dict[str, Any]:
    return {"tool_calls": list(calls), "complete": complete}


def neutral_metadata(**captures: dict[str, Any] | None) -> dict[str, Any]:
    """Return the neutral Call 2 metadata with the named examples' captures replaced.

    A ``None`` capture removes the example's capture.
    """

    document = deepcopy(neutral_artifact_response_without_source())
    for label, value in captures.items():
        if value is None:
            document["examples"][label].pop("capture", None)
        else:
            document["examples"][label]["capture"] = value
    return document


def artifact_findings(
    document: dict[str, Any], plan: dict[str, Any] | None = None, **kwargs: Any
) -> list[Finding]:
    """Run the artifact checks on the neutral plan and inventory."""

    return collect_artifact_findings_v2(
        document,
        plan or neutral_artifact_plan_v2(),
        deepcopy(NEUTRAL_INVENTORY),
        RUNTIME,
        **kwargs,
    )
