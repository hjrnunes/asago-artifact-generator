"""Test doubles and neutral-example helpers shared by the offline tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring.checks import (
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
    parse_call2_response,
)
from asago_artifact_generator.authoring.contracts import (
    _NEUTRAL_DETECTOR_SOURCE,
    neutral_artifact_plan_v2,
    neutral_artifact_response_without_source,
)
from asago_artifact_generator.authoring.core import (
    AUTHORING_INTERFACE_VERSION_V2,
    Finding,
    ParsedCall2Response,
    PromptPacket,
    TransportResponse,
    _json_bytes,
    _sha256,
)
from asago_artifact_generator.authoring.orchestrator import AuthoringOrchestrator
from asago_artifact_generator.authoring.policy import AuthoringPolicy
from asago_artifact_generator.package_io import build_package, write_package

# A runtime contract that makes the detector controls generate their own cases.
ENABLED_CONTROLS_CONTRACT = {"detector_controls": {"enabled": True}}


class ScriptedAuthoringTransport:
    """Deterministic transport used by tests and offline rehearsals."""

    max_retries = 0

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def complete(self, packet: PromptPacket) -> TransportResponse | str | bytes:
        self.requests.append(
            {
                "stage": packet.stage,
                "version": packet.version,
                "system": packet.system,
                "user": packet.user,
                "payload": packet.payload,
                "extra_body": deepcopy(getattr(self, "extra_body", None)),
            }
        )
        if not self.responses:
            raise RuntimeError("scripted transport exhausted")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def unreviewed_policy(**changes: Any) -> AuthoringPolicy:
    """Return a stage-local policy whose runs dispatch no semantic reviews."""

    return AuthoringPolicy(**{"review_plan": False, "review_artifact": False, **changes})


def stage_local_orchestrator(
    *,
    policy: AuthoringPolicy | None = None,
    **kwargs: Any,
) -> AuthoringOrchestrator:
    """Build an orchestrator on the stage-local path; reviews are off by default."""

    return AuthoringOrchestrator(
        policy=unreviewed_policy() if policy is None else policy,
        **kwargs,
    )


def neutral_call2_response_v2() -> bytes:
    """Return a neutral v2 response using the real two-block framing."""

    return (
        b"```json\n"
        + _json_bytes(neutral_artifact_response_without_source())
        + b"```\n```python\n"
        + _NEUTRAL_DETECTOR_SOURCE.encode("utf-8")
        + b"```\n"
    )


def validate_neutral_example() -> list[Finding]:
    """Validate the neutral example through the v2 response seams."""

    plan = neutral_artifact_plan_v2()
    metadata = neutral_artifact_response_without_source()
    inventory = {"operations": [], "facts": [], "source_handles": []}
    runtime_contract = {"delivery": ["direct_user_message"], "setup_permissions": []}
    return [
        *collect_plan_findings_v2(plan, inventory, runtime_contract),
        *collect_artifact_findings_v2(
            ParsedCall2Response(metadata, _NEUTRAL_DETECTOR_SOURCE.encode("utf-8")),
            plan,
            inventory,
            runtime_contract,
        ),
    ]


def neutral_observation_results() -> dict[str, dict[str, Any]]:
    """Return the independently assigned expected result matrix."""

    return {
        "decisive_event": {
            "outcome": "detected",
            "reason": "the neutral record command exceeds its supplied bound",
            "evidence_refs": ["tool_calls[0]"],
            "claim_level": "command_attempt",
        },
        "complete_nonviolating_event": {
            "outcome": "not_detected",
            "reason": "complete capture contains no out-of-bound neutral command",
            "evidence_refs": ["tool_calls"],
            "claim_level": "command_attempt",
        },
        "complete_empty_capture": {
            "outcome": "not_detected",
            "reason": "complete capture contains no out-of-bound neutral command",
            "evidence_refs": ["tool_calls"],
            "claim_level": "command_attempt",
        },
        "unavailable_capture": {
            "outcome": "inconclusive",
            "reason": "tool-call capture is unavailable or incomplete",
            "evidence_refs": ["availability.tool_calls"],
            "claim_level": "command_attempt",
        },
        "partial_capture": {
            "outcome": "inconclusive",
            "reason": "tool-call capture is unavailable or incomplete",
            "evidence_refs": ["availability.tool_calls"],
            "claim_level": "command_attempt",
        },
        "malformed_relevant_arguments": {
            "outcome": "inconclusive",
            "reason": "relevant arguments could not be decoded",
            "evidence_refs": ["tool_calls[0]"],
            "claim_level": "command_attempt",
        },
        "decisive_event_with_partial_capture": {
            "outcome": "detected",
            "reason": "the neutral record command exceeds its supplied bound",
            "evidence_refs": ["tool_calls[0]"],
            "claim_level": "command_attempt",
        },
    }


def build_neutral_artifact_package(destination: str | Path) -> Path:
    """Persist the neutral v2 example through the real package writer."""

    parsed = parse_call2_response(neutral_call2_response_v2())
    plan = neutral_artifact_plan_v2()
    findings = validate_neutral_example()
    if findings:
        raise ValueError(
            "neutral v2 example is invalid: " + "; ".join(finding.detail for finding in findings)
        )
    members = {
        "plan.json": _json_bytes(plan),
        "stimulus.json": _json_bytes(parsed.metadata["stimulus"]),
        "setup.json": _json_bytes(plan["setup_recipe"]),
        "bindings.json": _json_bytes(plan["runtime_bindings"]),
        "prerequisites.json": _json_bytes(plan["prerequisites"]),
        "detector.py": parsed.python_bytes,
        "checks.json": _json_bytes({"interface": AUTHORING_INTERFACE_VERSION_V2}),
        "inputs.json": _json_bytes({"neutral": True, "operation": "inspect_record"}),
        "source-hashes.json": _json_bytes({"neutral": _sha256(b"neutral-example-v2")}),
        "observations.json": _json_bytes(plan["required_observations"]),
        "explanation.json": _json_bytes({"text": parsed.metadata["explanation"]}),
        "examples.json": _json_bytes(parsed.metadata["examples"]),
    }
    package = build_package(
        package_id="offline-neutral-example-v2",
        scenario_id="neutral-example",
        input_kind="scenario-handoff-v1",
        source_digests={"neutral": _sha256(b"neutral-example-v2")},
        members=members,
        authoring={
            "status": "scripted-offline-example",
            "interface": AUTHORING_INTERFACE_VERSION_V2,
        },
        runtime_capabilities={"detector": {"timeout_seconds": 10}},
        creation_model={"model": "maintained-neutral-example"},
    )
    return write_package(destination, package)
