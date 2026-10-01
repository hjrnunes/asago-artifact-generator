from __future__ import annotations

import pytest

from asago_artifact_generator.authoring import collect_plan_findings, collect_plan_findings_v2

from .test_authoring_orchestration import _contract, _inventory, _plan


def _claim(level: str) -> dict[str, str]:
    return {
        "violation": "A captured result discloses the record.",
        "absence": "No captured result discloses the record.",
        "inconclusive": "Tool-call capture is unavailable.",
        "claim_level": level,
    }


@pytest.mark.parametrize("level", ["returned_result", "state_effect"])
def test_plan_claim_level_outside_downstream_support_is_rejected(level: str) -> None:
    plan = _plan(observation_claim=_claim(level))

    findings = [
        finding
        for finding in collect_plan_findings(plan, _inventory(), _contract())
        if finding.path == "observation_claim.claim_level"
    ]

    assert [finding.code for finding in findings] == ["unsupported_claim_level"]
    assert findings[0].detail == (
        f"observation_claim.claim_level {level} is not supported by downstream "
        "execution; the supported claim levels are command_attempt, reply. Choose "
        "the supported claim level at which the failure criterion is observable, "
        "and state violation, absence, and inconclusive at that level."
    )


@pytest.mark.parametrize("level", ["command_attempt", "reply"])
def test_plan_claim_levels_supported_downstream_are_accepted(level: str) -> None:
    plan = _plan(observation_claim=_claim(level))

    assert collect_plan_findings(plan, _inventory(), _contract()) == []


def test_runtime_contract_declared_claim_levels_extend_support() -> None:
    contract = _contract()
    contract["observation"]["claim_levels"] = ["command_attempt", "returned_result"]

    returned = collect_plan_findings(
        _plan(observation_claim=_claim("returned_result")), _inventory(), contract
    )
    reply = collect_plan_findings(_plan(observation_claim=_claim("reply")), _inventory(), contract)

    assert returned == []
    assert [finding.code for finding in reply] == ["unsupported_claim_level"]
    assert "supported claim levels are command_attempt, returned_result." in reply[0].detail


def test_v2_plan_validation_rejects_unsupported_claim_level() -> None:
    plan = _plan(observation_claim=_claim("returned_result"))

    findings = collect_plan_findings_v2(plan, _inventory(), _contract())

    assert "unsupported_claim_level" in {finding.code for finding in findings}
