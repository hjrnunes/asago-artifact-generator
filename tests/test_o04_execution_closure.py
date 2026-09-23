from __future__ import annotations

import pytest
from scripts.continuations import o04_execution_closure as closure


def _packet(stage: str = "plan_review") -> closure.PromptPacket:
    return closure.PromptPacket(
        stage=stage,
        version="test-packet-v1",
        system="system instructions",
        user="exact candidate input",
        payload={"candidate": {"immutable": True}},
    )


def test_plan_review_dispatch_reservation_is_single_use() -> None:
    packet = _packet()
    ledger = {
        "allowance": {"total": 4, "plan_review": 1},
        "dispatches": [],
        "state": "prepared_for_plan_review",
    }

    dispatch = closure._reserve_plan_review_dispatch(ledger, packet)

    assert ledger["state"] == "plan_review_dispatched"
    assert dispatch["prompt_sha256"] == packet.sha256
    assert dispatch["controls"] == {
        "profile": "gemma4-oc",
        "temperature": 0,
        "thinking": False,
        "max_completion_tokens": closure.AUTHORING_MAX_COMPLETION_TOKENS,
        "context_window_tokens": closure.AUTHORING_CONTEXT_WINDOW_TOKENS,
        "max_retries": 0,
    }
    with pytest.raises(ValueError, match="slot is already consumed"):
        closure._reserve_plan_review_dispatch(ledger, packet)
    assert len(ledger["dispatches"]) == 1


def test_artifact_review_dispatch_reservation_is_single_use() -> None:
    packet = _packet("artifact_review")
    ledger = {
        "allowance": {"total": 4, "artifact_review": 1},
        "dispatches": [{"stage": "plan_review", "status": "review_recorded"}],
        "state": "prepared_for_artifact_review",
    }

    dispatch = closure._reserve_artifact_review_dispatch(ledger, packet)

    assert ledger["state"] == "artifact_review_dispatched"
    assert dispatch["prompt_sha256"] == packet.sha256
    assert dispatch["controls"]["max_completion_tokens"] == 2048
    with pytest.raises(ValueError, match="slot is already consumed"):
        closure._reserve_artifact_review_dispatch(ledger, packet)
    assert len(ledger["dispatches"]) == 2
