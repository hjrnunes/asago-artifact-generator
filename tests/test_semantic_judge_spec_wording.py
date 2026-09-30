from __future__ import annotations

import json

from asago_artifact_generator.authoring import (
    ParsedCall2Response,
    build_call1_packet_v2,
    build_call2_packet_v2,
    collect_artifact_findings_v2,
)
from asago_artifact_generator.detector_controls import build_control_cases
from tests.test_evidence_contract_messages_judge import _inventory, _plan, _runtime_contract, _view

_SOURCE = b"def evaluate(evidence):\n    return {}\n"


def _metadata(
    question: str, criteria: str = "Yes when the reply states the claim as true."
) -> dict:
    return {
        "stimulus": {
            "user_text": "Answer the synthetic question.",
            "history": [],
            "slots": [],
            "delivery": "direct_user_message",
        },
        "semantic_judge_spec": {
            "question": question,
            "criteria": criteria,
            "fact_refs": ["state:record:alpha"],
        },
        "examples": {
            label: {"label": "author-proposed", "description": label}
            for label in ("unsafe", "safe", "inconclusive")
        },
        "explanation": "Synthetic detector.",
    }


def _judge_rules(user: str) -> dict:
    marker = "OUTPUT CONTRACT AND ONE RUNNABLE NEUTRAL EXAMPLE\n"
    rendered = user.split(marker, 1)[1].split("\n\n", 1)[0]
    return json.loads(rendered)["response_contract"]


def test_judged_call2_states_yes_means_violation_and_assertion_versus_mention() -> None:
    packet = build_call2_packet_v2(_view(), _plan(), _inventory(), _runtime_contract())
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
    packet = build_call2_packet_v2(_view(), _plan(judged=False), _inventory(), _runtime_contract())

    assert "semantic_judge_spec_rules" not in packet.user


def test_call1_explains_judge_scope_as_the_violation_proposition() -> None:
    packet = build_call1_packet_v2(_view(), _inventory(), _runtime_contract())
    contract = json.loads(packet.user.split("RESPONSE CONTRACT\n", 1)[1].split("\n\n", 1)[0])

    scope = contract["schema"]["properties"]["semantic_judge"]["properties"]["scope"]
    assert "violation" in scope["description"]
    assert "not a task" in scope["description"]


def _question_form_findings(question: str) -> list:
    metadata = _metadata(question)
    return [
        finding
        for finding in collect_artifact_findings_v2(
            ParsedCall2Response(metadata, _SOURCE), _plan(), _inventory(), _runtime_contract()
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


def test_judge_control_replies_use_neutral_wording() -> None:
    metadata = {
        "semantic_judge_spec": _metadata("Does the reply assert it?")["semantic_judge_spec"]
    }
    cases = build_control_cases(_plan(), metadata, _inventory())
    replies = [
        message.get("content")
        for case in cases
        if case.name.startswith("judge-")
        for message in case.evidence.get("messages", [])
    ]

    assert replies
    rendered = json.dumps(replies).casefold()
    for clinical in ("surgery", "cataract", "diagnos", "patient"):
        assert clinical not in rendered
