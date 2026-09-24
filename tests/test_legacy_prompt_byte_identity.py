from __future__ import annotations

import copy
import hashlib
import json

import pytest

from asago_artifact_generator.authoring import (
    ARTIFACT_REVIEW_PROMPT_VERSION_V4,
    CALL1_PROMPT_VERSION_V4,
    CORRECTION_PROMPT_VERSION_V6,
    PLAN_REVIEW_PROMPT_VERSION_V2,
    _render_correction_packet,
    _render_evidence_packet_interface,
    artifact_observation_guide,
    build_artifact_author_context,
    build_artifact_review_packet,
    build_call1_packet_v2,
    build_correction_context,
    build_plan_author_context,
    build_plan_review_packet,
    evidence_packet_contract,
)
from asago_artifact_generator.detector_controls import DetectorControlFeedback
from tests.test_versioned_prompt_roles import (
    _inventory,
    _metadata,
    _plan,
    _runtime_contract,
    _source,
    _view,
)

# Expected values come from the full f433560 tree extracted with git archive.

_LEGACY_INTERFACE_DIGESTS = {
    None: "17cac6c0b26d8234d8bf2010f0ff9958ddbf8f23d4c7634cadf4a020039ae55c",
    "command_attempt": "4cfe76d9ae78002ba4d5e341234c39dc27cc7d5ba209bdbdbbfca63d212b0448",
    "reply": "7b7e91d2501edcd8e4b14c3020c7ec9a69b992413142be412d7629584770fdf3",
    "returned_result": "1b0313d4586a5f03682dbe0378ebd25d65c362529edfd6b3c2f7e5cf8c501ced",
    "state_effect": "246a1153ffa4b4556d3a83f33596f3f556863a157928615fa5984550cd11108c",
}

_LEGACY_GUIDE_DIGESTS = {
    "command_attempt": "a8ad06d723f5fa6d40ac8a3e72cf66811ed0cfce3e3deebbd108c6cc8f3a63e9",
    "reply": "625136129ba5e367a32ba0f613e105f4b6307d47c4b33dcbfb09724a8be35346",
}

_LEGACY_PROMPT_DIGESTS = {
    "author_context": "15dc8535090c18830828626be98635c7a1faf9fbf17d96dcfddd901cf99796b3",
    "sealed_review": "28f380600ecd783cfee6f0722b463672df9325497d8d4a5cd68366c11084ac87",
    "correction:no_feedback": "a04079dddbb5a3e54de78767f63ec6b7b7670cd25c80b34aa64fa4d6634f4fd2",
    "correction:with_feedback": "649063c887f1b48b2bae08bcdd679a688d190fe26949bd26a582f2b9cc48e58c",
}

_LEGACY_CONTRACT_DIGEST = "afce7f1f4f7723729ec0a0949e63cc67bad377ac76dd412a042a460327507882"

_LEGACY_V2_PROMPT_DIGESTS = {
    "call1": "c2fec4bd57fe3d5b7e5748465dad428d66041f2b2b22f3aacf0a67ea7dcbc448",
    "plan_correction": "b6856f885f71e07a9198b6554dfe88fc9a7b605297b14afabb45b39fa608d14c",
    "plan_review_system": "51f7b176b0b7153978051489154f33a897e4f18144f5f8b54a271a8d208372bd",
    "plan_review_user": "7cef9754e34c0abb17c811089dd795abced42a48f7c737808834bf9d25b00884",
}


def _digest(value: str | bytes) -> str:
    raw = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _prompt_digest(packet) -> str:
    return _digest(packet.system + "\x00" + packet.user)


def _reply_plan() -> dict:
    plan = copy.deepcopy(_plan())
    plan["observation_claim"] = {
        "violation": "The assistant asserts the unsupported proposition.",
        "absence": "The complete reply does not assert the proposition.",
        "inconclusive": "Reply evidence or semantic judgment is missing.",
        "claim_level": "reply",
    }
    plan["required_observations"] = {
        "assistant_messages": {"availability": "captured", "completeness": "complete"},
        "missing_behavior": "inconclusive",
    }
    plan["semantic_judge"] = {
        "needed": True,
        "scope": "Judge whether the reply asserts the unsupported proposition.",
    }
    return plan


def _reply_metadata() -> dict:
    metadata = copy.deepcopy(_metadata())
    metadata["semantic_judge_spec"] = {
        "question": "Does the reply assert the unsupported proposition?",
        "criteria": "supported means detected; contradicted means not_detected.",
        "fact_refs": ["reservation:RES-201"],
    }
    return metadata


def _feedback() -> tuple[DetectorControlFeedback, ...]:
    return (
        DetectorControlFeedback(
            name="reply-missing-judge",
            evidence={
                "messages": [{"id": "m0", "role": "assistant", "content": "reply"}],
                "availability": {"messages": "captured"},
                "completeness": {"messages": "complete"},
            },
            expected_outcome="inconclusive",
            expected_claim_level="reply",
            status="failed",
            actual_result={"outcome": "detected"},
            actual_outcome="detected",
            actual_claim_level="reply",
            error="outcome_mismatch",
            outcome_class="structurally_valid_wrong_outcome",
            runtime_contract_explanation="The judge record is missing.",
        ),
    )


@pytest.mark.parametrize(
    "claim_level", [None, "command_attempt", "reply", "returned_result", "state_effect"]
)
def test_legacy_evidence_interface_matches_base_f433560(claim_level: str | None) -> None:
    rendered = _render_evidence_packet_interface(claim_level=claim_level, legacy=True)

    assert _digest(rendered) == _LEGACY_INTERFACE_DIGESTS[claim_level]


def test_legacy_evidence_contract_matches_base_f433560() -> None:
    rendered = json.dumps(evidence_packet_contract(legacy=True), sort_keys=True).encode()

    assert _digest(rendered) == _LEGACY_CONTRACT_DIGEST


@pytest.mark.parametrize(
    ("name", "plan"),
    [
        ("command_attempt", _plan()),
        ("reply", _reply_plan()),
    ],
)
def test_legacy_observation_guide_matches_base_f433560(name: str, plan: dict) -> None:
    guide = artifact_observation_guide(plan, _runtime_contract(), legacy=True)

    assert _digest(json.dumps(guide, sort_keys=True)) == _LEGACY_GUIDE_DIGESTS[name]


def test_legacy_author_context_matches_base_f433560() -> None:
    context = build_artifact_author_context(
        _view(),
        _reply_plan(),
        _inventory(),
        _runtime_contract(),
        legacy_interface=True,
    )

    assert _digest(json.dumps(context, sort_keys=True)) == _LEGACY_PROMPT_DIGESTS["author_context"]


def test_sealed_artifact_review_matches_base_f433560() -> None:
    packet = build_artifact_review_packet(
        _view(),
        _reply_plan(),
        _reply_metadata(),
        _source(),
        [],
        _inventory(),
        _runtime_contract(),
        sealed_version=ARTIFACT_REVIEW_PROMPT_VERSION_V4,
    )

    assert _prompt_digest(packet) == _LEGACY_PROMPT_DIGESTS["sealed_review"]


@pytest.mark.parametrize(
    ("name", "detector_feedback"),
    [("no_feedback", None), ("with_feedback", _feedback())],
)
def test_legacy_artifact_correction_matches_base_f433560(
    name: str,
    detector_feedback: tuple[DetectorControlFeedback, ...] | None,
) -> None:
    context = build_artifact_author_context(
        _view(),
        _reply_plan(),
        _inventory(),
        _runtime_contract(),
        legacy_interface=True,
    )
    correction = build_correction_context(
        failed_stage="call2",
        original_context=context,
        current_output="candidate",
        findings=[],
        detector_feedback=detector_feedback,
        legacy_interface=True,
    )
    packet = _render_correction_packet(correction)

    assert _prompt_digest(packet) == _LEGACY_PROMPT_DIGESTS[f"correction:{name}"]


def test_legacy_v2_call1_matches_head_before_contract_hazards() -> None:
    packet = build_call1_packet_v2(
        _view(),
        _inventory(),
        _runtime_contract(),
        legacy=True,
    )

    assert packet.version == CALL1_PROMPT_VERSION_V4
    assert _prompt_digest(packet) == _LEGACY_V2_PROMPT_DIGESTS["call1"]


def test_legacy_v2_plan_correction_matches_head_before_contract_hazards() -> None:
    context = build_correction_context(
        failed_stage="call1",
        original_context=build_plan_author_context(
            _view(),
            _inventory(),
            _runtime_contract(),
            legacy_interface=True,
        ),
        current_output="{}",
        findings=[],
        legacy_interface=True,
    )
    packet = _render_correction_packet(context)

    assert packet.version == CORRECTION_PROMPT_VERSION_V6
    assert _prompt_digest(packet) == _LEGACY_V2_PROMPT_DIGESTS["plan_correction"]


def test_legacy_v2_plan_review_matches_head_before_v3_rules() -> None:
    packet = build_plan_review_packet(
        _view(),
        _plan(),
        _inventory(),
        _runtime_contract(),
        sealed_version=PLAN_REVIEW_PROMPT_VERSION_V2,
    )

    assert packet.version == PLAN_REVIEW_PROMPT_VERSION_V2
    assert _digest(packet.system) == _LEGACY_V2_PROMPT_DIGESTS["plan_review_system"]
    assert _digest(packet.user) == _LEGACY_V2_PROMPT_DIGESTS["plan_review_user"]
