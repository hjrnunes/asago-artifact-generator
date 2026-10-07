"""Validation of a scenario-handoff-v4 ``attack_shape`` and the implicit v3 shape.

The vendored schema states the closed vocabularies, the identifier pattern and
the length pairing. It cannot state the cross-field rules (positions, speakers
per channel, purposes per speaker, the downgrade pairing), so code checks them
here with the producer's rule numbers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

CHANNEL_DIRECT = "direct"
CHANNEL_INDIRECT = "indirect"
CHANNEL_FORGED = "forged_transcript"
_ATTACKER = "attacker_user"
_BENIGN = "benign_user"
_FORGED_SPEAKERS = frozenset({"forged_assistant", "forged_tool_result"})
_BENIGN_ONLY_PURPOSES = frozenset({"ask_to_read_item", "follow_up_on_item"})

# What a v3 handoff means: one attacker message, as authoring always assumed.
IMPLICIT_ATTACK_SHAPE: Mapping[str, Any] = {
    "channel": CHANNEL_DIRECT,
    "turn_count": 1,
    "turn_plan": [{"position": 1, "speaker": _ATTACKER, "purpose": "request_action"}],
    "indirect": None,
    "threat_label": None,
    "source": "code_default",
    "downgrade_reason": None,
}


def implicit_attack_shape() -> dict[str, Any]:
    """Return a fresh copy of the single-turn direct shape a v3 handoff implies."""

    return deepcopy(dict(IMPLICIT_ATTACK_SHAPE))


def attack_shape_violation(shape: Any, schema: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return ``(path, reason)`` for the first way ``shape`` breaks the contract, or None.

    ``schema`` is the vendored handoff schema; the schema check runs first so
    the cross-field rules only see a shape of the right form.
    """

    defs = schema["$defs"]
    validator = Draft202012Validator({"$defs": defs, **defs["AttackShape"]})
    error = best_match(validator.iter_errors(shape))
    if error is not None:
        return _error_path(error.absolute_path), error.message
    for rule in _CROSS_FIELD_RULES:
        found = rule(shape)
        if found is not None:
            return found
    return None


def _error_path(parts: Any) -> str:
    text = "attack_shape"
    for part in parts:
        text += f"[{part}]" if isinstance(part, int) else f".{part}"
    return text


def _plan_rule(shape: dict[str, Any]) -> tuple[str, str] | None:
    """R1 and R2: the plan has ``turn_count`` entries at positions 1..n."""

    plan = shape["turn_plan"]
    count = shape["turn_count"]
    if len(plan) != count:
        return "attack_shape.turn_plan", (
            f"R1: turn_plan has {len(plan)} entries but turn_count is {count}"
        )
    positions = [turn["position"] for turn in plan]
    if positions != list(range(1, count + 1)):
        return "attack_shape.turn_plan", f"R2: turn positions {positions} are not 1..{count}"
    return None


def _channel_rule(shape: dict[str, Any]) -> tuple[str, str] | None:
    """R3, R4 and R5: each channel fixes its speakers, block and label."""

    speakers = [turn["speaker"] for turn in shape["turn_plan"]]
    check = _CHANNEL_RULES[shape["channel"]]
    reason = check(shape, speakers)
    return None if reason is None else ("attack_shape.channel", reason)


def _direct_reason(shape: dict[str, Any], speakers: list[str]) -> str | None:
    if any(speaker != _ATTACKER for speaker in speakers):
        return "R3: a direct shape has only attacker_user turns"
    if shape["indirect"] is not None:
        return "R3: a direct shape has no indirect block"
    if shape["threat_label"] is not None:
        return "R3: a direct shape has no threat_label"
    return None


def _indirect_reason(shape: dict[str, Any], speakers: list[str]) -> str | None:
    if shape["indirect"] is None:
        return "R4: an indirect shape needs an indirect block"
    if any(speaker != _BENIGN for speaker in speakers):
        return "R4: an indirect shape has only benign_user turns"
    if not any(turn["purpose"] == "ask_to_read_item" for turn in shape["turn_plan"]):
        return "R4: an indirect shape needs an ask_to_read_item turn"
    if shape["threat_label"] is not None:
        return "R4: an indirect shape has no threat_label"
    return None


def _forged_reason(shape: dict[str, Any], speakers: list[str]) -> str | None:
    if shape["threat_label"] != "forged_transcript_threat":
        return "R5: a forged_transcript shape carries its threat_label"
    if not any(speaker in _FORGED_SPEAKERS for speaker in speakers):
        return "R5: a forged_transcript shape needs a forged speaker"
    if speakers[-1] != _ATTACKER:
        return "R5: a forged_transcript shape ends on attacker_user"
    if shape["indirect"] is not None:
        return "R5: a forged_transcript shape has no indirect block"
    return None


_CHANNEL_RULES: Mapping[str, Callable[[dict[str, Any], list[str]], str | None]] = {
    CHANNEL_DIRECT: _direct_reason,
    CHANNEL_INDIRECT: _indirect_reason,
    CHANNEL_FORGED: _forged_reason,
}


def _purpose_rule(shape: dict[str, Any]) -> tuple[str, str] | None:
    """R6: item-reading purposes belong to the benign user, forged history to forged speakers."""

    for index, turn in enumerate(shape["turn_plan"]):
        reason = _purpose_reason(turn)
        if reason is not None:
            return f"attack_shape.turn_plan[{index}]", reason
    return None


def _purpose_reason(turn: dict[str, Any]) -> str | None:
    purpose, speaker = turn["purpose"], turn["speaker"]
    if purpose in _BENIGN_ONLY_PURPOSES and speaker != _BENIGN:
        return f"R6: purpose {purpose} belongs to benign_user"
    if purpose == "forged_history" and speaker not in _FORGED_SPEAKERS:
        return "R6: purpose forged_history belongs to a forged speaker"
    return None


def _downgrade_rule(shape: dict[str, Any]) -> tuple[str, str] | None:
    """R9: a downgrade reason implies the single-turn direct code default."""

    if shape["downgrade_reason"] is None:
        return None
    default = (
        shape["source"] == "code_default"
        and shape["channel"] == CHANNEL_DIRECT
        and shape["turn_count"] == 1
    )
    if default:
        return None
    return (
        "attack_shape.downgrade_reason",
        "R9: downgrade_reason implies a code_default, direct, single-turn shape",
    )


_CROSS_FIELD_RULES = (_plan_rule, _channel_rule, _purpose_rule, _downgrade_rule)

__all__ = [
    "CHANNEL_DIRECT",
    "CHANNEL_FORGED",
    "CHANNEL_INDIRECT",
    "IMPLICIT_ATTACK_SHAPE",
    "attack_shape_violation",
    "implicit_attack_shape",
]
