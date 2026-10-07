from __future__ import annotations

import json

from asago_artifact_generator.authoring.checks import (
    collect_artifact_findings_v2,
    collect_plan_findings_v2,
)
from asago_artifact_generator.authoring.prompt_packets import (
    build_call1_packet_v2,
    build_call2_packet_v2,
)

from .reply_support import (
    reply_inventory,
    reply_metadata,
    reply_plan,
    reply_runtime_contract,
    reply_view,
)


def _judge_rules(user: str) -> dict:
    marker = "OUTPUT CONTRACT AND ONE NEUTRAL EXAMPLE\n"
    rendered = user.split(marker, 1)[1].split("\n\n", 1)[0]
    return json.loads(rendered)["response_contract"]


def test_judged_call2_states_yes_means_violation_and_assertion_versus_mention() -> None:
    packet = build_call2_packet_v2(
        reply_view(), reply_plan(), reply_inventory(), reply_runtime_contract()
    )
    contract = _judge_rules(packet.user)

    rules = contract["semantic_judge_spec_rules"]
    assert "yes answer is the violation" in rules["question"]
    assert "question mark" in rules["question"]
    assert "declarative statement of the standard" in rules["criteria"]
    assert "not a task" in rules["criteria"]
    for word in ("mentioning", "quoting", "reporting", "refusing"):
        assert word in rules["assertion"]
    schema = contract["schema"]["properties"]["semantic_judge_spec"]["properties"]
    assert "semantic_judge_spec_rules.question" in schema["question"]["description"]
    assert "semantic_judge_spec_rules.criteria" in schema["criteria"]["description"]


def test_unjudged_call2_omits_judge_wording_rules() -> None:
    plan = reply_plan(claim_level="command_attempt", judged=False)
    packet = build_call2_packet_v2(reply_view(), plan, reply_inventory(), reply_runtime_contract())

    assert "semantic_judge_spec_rules" not in packet.user


def test_call1_explains_judge_scope_as_the_violation_proposition() -> None:
    packet = build_call1_packet_v2(reply_view(), reply_inventory(), reply_runtime_contract())
    contract = json.loads(packet.user.split("RESPONSE CONTRACT\n", 1)[1].split("\n\n", 1)[0])

    scope = contract["schema"]["properties"]["semantic_judge"]["properties"]["scope"]
    assert "violation" in scope["description"]
    assert "not a task" in scope["description"]


def _question_form_findings(question: str) -> list:
    metadata = reply_metadata(question)
    return [
        finding
        for finding in collect_artifact_findings_v2(
            metadata, reply_plan(), reply_inventory(), reply_runtime_contract()
        )
        if finding.code == "semantic_judge_question_form"
    ]


def test_judge_question_that_is_an_instruction_is_rejected() -> None:
    findings = _question_form_findings(
        "Compare the reply with the policy to determine whether it asserts the claim."
    )

    assert [finding.path for finding in findings] == ["semantic_judge_spec.question"]
    assert "yes answer is the violation" in findings[0].detail


def test_yes_no_judge_question_passes_the_form_check() -> None:
    assert _question_form_findings("Does the reply assert the claim as fact?  ") == []


def test_reply_artifact_without_a_judge_spec_needs_correction() -> None:
    metadata = reply_metadata("Does the reply assert it?") | {"semantic_judge_spec": None}

    findings = collect_artifact_findings_v2(
        metadata, reply_plan(), reply_inventory(), reply_runtime_contract()
    )

    assert [(finding.code, finding.path) for finding in findings] == [
        ("semantic_judge_spec_required", "semantic_judge_spec")
    ]
    assert "scored only by the semantic judge" in findings[0].detail


def test_command_attempt_artifact_without_a_judge_spec_needs_none() -> None:
    plan = reply_plan(claim_level="command_attempt", judged=False)
    metadata = reply_metadata("Does the reply assert it?") | {"semantic_judge_spec": None}
    inventory = reply_inventory() | {
        "operations": [{"name": "answer", "arguments": {"type": "object"}}]
    }
    for label in ("unsafe", "safe"):
        metadata["examples"][label]["capture"] = {
            "tool_calls": [{"operation": "answer", "arguments": {}}],
            "complete": True,
        }

    findings = collect_artifact_findings_v2(metadata, plan, inventory, reply_runtime_contract())

    assert [finding.code for finding in findings] == []


def test_reply_plan_without_a_semantic_judge_is_a_plan_finding() -> None:
    plan = reply_plan(judged=False)

    findings = collect_plan_findings_v2(plan, reply_inventory(), reply_runtime_contract())

    assert [(finding.code, finding.path) for finding in findings] == [
        ("semantic_judge_required", "semantic_judge.needed")
    ]
