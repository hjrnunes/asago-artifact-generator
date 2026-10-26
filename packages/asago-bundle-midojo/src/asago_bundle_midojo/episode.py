"""The MiDojo episode as plain data: the fields of ``midojo.episode.Episode``, JSON-able.

MiDojo (asago/main at b6e0f8f) describes one run as a frozen ``Episode``
(``src/midojo/episode.py``): the user ``turns``, the injection ``plan``
(``midojo_sdk.injection.InjectionPlan``, ``interception/python-sdk/src/midojo_sdk/injection.py``),
the ``security`` and ``utility`` checks as authored specs, the initial ``environment``
of the simulated tools, the ``runtime`` definition (``suite_definition.AgentRuntimeDefinition``
as its fields), and the provenance IDs ``suite_name``, ``user_task_id`` and
``injection_task_id`` (None when no suite compiled it). MiDojo publishes no JSON form of
an episode, so this module writes each dataclass as a mapping of its fields, in MiDojo's
field names, with every enum as its string value and every tuple as a list. A runner
builds the MiDojo types from it field by field (``tests/fixtures/episode/generate.py``
does, in MiDojo's interpreter). The environment type is code, not data: MiDojo infers it
from the environment.

A planted item becomes what ``YAMLTaskSuite.episode`` makes of a probe in a suite's
environment: an ``environment_state`` instruction with a ``template`` selector whose
``template_path`` is the field's path in the environment and whose ``template_key`` names
the probe, mode ``replace`` (the probe's placeholder is replaced), and an entry of the
plan's ``probe_payloads``. The episode is resolved: its environment holds the payload, as
the suite's episode holds the substituted environment.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# ``midojo_sdk.injection`` enum values.
TOOL_OUTPUT = "tool_output"
STRUCTURED_CONTENT = "structured_content"
EVERY = "every"
ENVIRONMENT_STATE = "environment_state"
TEMPLATE = "template"
REPLACE = "replace"
FIRST = "first"
IDENTITY = "identity"
STRICT = "strict"
UNMANAGED_RUNTIME = {"type": "unmanaged"}
# The provenance of a probe key, ``<task>:<probe>`` as MiDojo names probes.
PROBE_TASK = "planted_item"
EPISODE_FIELDS = (
    "runtime",
    "turns",
    "plan",
    "security",
    "utility",
    "environment",
    "suite_name",
    "user_task_id",
    "injection_task_id",
)


def probe_key(index: int) -> str:
    """Return the key of the probe that places seed item ``index``."""

    return f"{PROBE_TASK}:seed_{index}"


def template_instruction(path: Sequence[str | int], key: str, payload: str) -> dict[str, Any]:
    """Return an ``InjectionInstruction`` that places ``payload`` at ``path`` in the environment.

    Every field is written, defaults included, so the data names the whole placement:
    the first occurrence, no transformation, no size limit, strict on a mismatch.
    """

    return {
        "channel": ENVIRONMENT_STATE,
        "payload": payload,
        "target": {
            "tool": None,
            "surface": TEMPLATE,
            "content_block_index": None,
            "content_block_type": None,
            "structured_content_path": [],
            "template_path": list(path),
            "template_key": key,
        },
        "occurrence": {"kind": FIRST, "nth": None},
        "transformation": IDENTITY,
        "size_limit": None,
        "mismatch_policy": STRICT,
        "mode": REPLACE,
        "probe_key": key,
    }


def tool_output_instruction(
    *,
    tool: str,
    path: Sequence[str | int],
    payload: str,
    mode: str,
    size_limit: int,
    occurrence: str,
    key: str,
) -> dict[str, Any]:
    """Return an ``InjectionInstruction`` that places ``payload`` at ``path`` in ``tool``'s result.

    Every field is written, defaults included, so the data names the whole placement: the
    calls ``occurrence`` selects, no transformation, the size limit in UTF-8 bytes, and
    strict on a mismatch (MiDojo reports a path it cannot match and leaves the result).
    """

    return {
        "channel": TOOL_OUTPUT,
        "payload": payload,
        "target": {
            "tool": tool,
            "surface": STRUCTURED_CONTENT,
            "content_block_index": None,
            "content_block_type": None,
            "structured_content_path": list(path),
            "template_path": [],
            "template_key": None,
        },
        "occurrence": {"kind": occurrence, "nth": None},
        "transformation": IDENTITY,
        "size_limit": size_limit,
        "mismatch_policy": STRICT,
        "mode": mode,
        "probe_key": key,
    }


def injection_plan(instructions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Return an ``InjectionPlan``: the instructions, no generator permissions, the payloads.

    ``probe_payloads`` are sorted by key, as ``InjectionPlan`` keeps them.
    """

    payloads = sorted((item["target"]["template_key"], item["payload"]) for item in instructions)
    return {
        "instructions": list(instructions),
        "generator_permissions": {"tools": [], "fields": [], "modes": []},
        "probe_payloads": [list(pair) for pair in payloads],
    }


def episode(
    *,
    turns: Sequence[str],
    plan: dict[str, Any],
    security: dict[str, Any],
    environment: dict[str, Any],
) -> dict[str, Any]:
    """Return an ``Episode``: no utility check, the ``unmanaged`` runtime, no suite provenance."""

    return {
        "runtime": dict(UNMANAGED_RUNTIME),
        "turns": list(turns),
        "plan": plan,
        "security": security,
        "utility": None,
        "environment": environment,
        "suite_name": None,
        "user_task_id": None,
        "injection_task_id": None,
    }
