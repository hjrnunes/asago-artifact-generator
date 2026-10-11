"""A prerequisite binding of the wrong type is a finding, not a crash."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.authoring.checks import collect_plan_findings_v2

from .policy_support import NO_REVIEW, ONE_PLAN_CORRECTION, run_refund
from .support import ScriptedAuthoringTransport, world_builders

_framed, _inventory, _plan, _runtime_contract = world_builders(
    "refund", "framed", "inventory", "plan", "runtime_contract"
)

_UNHASHABLE_NAMES = [[], {}, ["owned_order"], {"name": "owned_order"}]
_ids = ["empty-list", "empty-object", "list", "object"]


def _plan_with_prerequisite_binding(binding: Any) -> dict[str, Any]:
    plan = _plan()
    plan["prerequisites"][0]["binding"] = binding
    return plan


@pytest.mark.parametrize("binding", _UNHASHABLE_NAMES, ids=_ids)
def test_a_list_or_object_binding_name_gives_the_prerequisite_finding(binding: Any) -> None:
    findings = collect_plan_findings_v2(
        _plan_with_prerequisite_binding(binding), _inventory(), _runtime_contract()
    )

    assert [
        (finding.code, finding.path)
        for finding in findings
        if finding.path == "prerequisites[0].binding"
    ] == [("type_error", "prerequisites[0].binding")]


def test_the_reported_reproduction_returns_findings() -> None:
    findings = collect_plan_findings_v2(
        {"runtime_bindings": [], "prerequisites": [{"binding": []}]}, {}, {}
    )

    assert ("type_error", "prerequisites[0].binding") in [
        (finding.code, finding.path) for finding in findings
    ]


@pytest.mark.parametrize("binding", _UNHASHABLE_NAMES, ids=_ids)
def test_the_orchestrator_reports_the_finding_instead_of_crashing(
    tmp_path: Path, binding: Any
) -> None:
    transport = ScriptedAuthoringTransport([json.dumps(_plan_with_prerequisite_binding(binding))])

    result = run_refund(tmp_path, transport, NO_REVIEW)

    codes = [finding.code for finding in result.findings]
    assert "authoring_crashed" not in codes
    assert "type_error" in codes


@pytest.mark.parametrize("binding", _UNHASHABLE_NAMES, ids=_ids)
def test_the_correction_chance_survives_a_list_or_object_binding_name(
    tmp_path: Path, binding: Any
) -> None:
    transport = ScriptedAuthoringTransport(
        [json.dumps(_plan_with_prerequisite_binding(binding)), json.dumps(_plan()), _framed()]
    )

    result = run_refund(tmp_path, transport, ONE_PLAN_CORRECTION)

    assert [request["stage"] for request in transport.requests[:2]] == ["call1", "correction"]
    assert result.status == "accepted"
