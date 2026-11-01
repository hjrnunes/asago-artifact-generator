"""The MiDojo episode as a JSON document: episode document v1, whose schema MiDojo owns.

MiDojo describes one run as a frozen ``Episode`` and writes it as an ``midojo-episode-v1``
document (``src/midojo/episode_document.py``; the schema is
``midojo_sdk.episode_schema.load_schema()``). The document holds the user ``turns``, the
injection ``plan`` (``midojo_sdk.injection.InjectionPlan``,
``interception/python-sdk/src/midojo_sdk/injection.py``), the ``security`` and ``utility``
checks as authored specs, the initial ``environment`` of the simulated tools, the
``runtime`` definition, the interception settings, the agent's tool policy, and the
provenance IDs ``suite_name``, ``user_task_id`` and ``injection_task_id`` (null when no
suite compiled it). This module writes that document for a planted item, in MiDojo's field
names, with every member written and every tuple as a list. MiDojo's ``Episode.from_document``
reads it.

A planted item becomes a ``tool_output`` instruction: MiDojo's host interceptor
(``midojo.interception_host``) forwards each call to the target's MCP server and places
the payload in the result of the instruction's tool, at its ``structured_content`` path
(``midojo_sdk.mcp``). A path step is a key, a list index, or a record selector
(``RecordSelector``), written as MiDojo's plan data writes it: ``{"field": <key>,
"equals": <value>}``. A target may also name ``call_arguments``, an object of the call's
top-level arguments and their values: only a call whose arguments match gets the
instruction, and MiDojo leaves any other call of the tool unchanged and records it
``unmatched``, at occurrence 0, without counting it. MiDojo's plan data leaves the key out
when there are none, and so does this module. From c0bee12 a target may instead (or also)
name ``result_record``, a record selector on the result's structured content object
itself, ``{"field": <key>, "equals": <value>}``: only a call whose result has that field
equal to the value, type for type, gets the instruction, and any other call of the tool,
one with no result included, is left unchanged and recorded ``unmatched`` at occurrence 0,
as for an argument miss. MiDojo's plan data leaves it out when there is none, and so does
this module. The interceptor changes the result's
``structuredContent``, and copies the change into its text when that text is a JSON
mirror of the structured content (the fixture target's is); other text stays as the target
sent it, and the outcome is ``partial``. The call record MiDojo seals holds the text the
agent got, the structured content and the injection outcomes.
The target keeps its own data, so the episode has no environment; and the plan has no
``probe_payloads``, which only template instructions name.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from midojo_sdk.episode_schema import EPISODE_DOCUMENT_V1

# ``midojo_sdk.injection`` enum values.
TOOL_OUTPUT = "tool_output"
STRUCTURED_CONTENT = "structured_content"
FIRST = "first"
EVERY = "every"
IDENTITY = "identity"
STRICT = "strict"
UNMANAGED_RUNTIME = {"type": "unmanaged"}
# The interceptor runs on the host: the runner supplies the target's MCP endpoint at run
# time, so the document names no upstream.
HOST_INTERCEPTION = {"mode": "host", "upstream": None}
# The provenance of an instruction, ``<task>:<probe>`` as MiDojo names probes.
PROBE_TASK = "planted_item"
# Every member of an episode document v1, in MiDojo's ``to_document`` order, null where the
# episode has none: two dumps of the same episode are the same bytes.
EPISODE_FIELDS = (
    "schema_version",
    "suite_name",
    "user_task_id",
    "injection_task_id",
    "environment_type",
    "environment",
    "runtime",
    "turns",
    "plan",
    "security",
    "utility",
    "verifiers",
    "interception",
    "tools",
)


def probe_key(index: int) -> str:
    """Return the provenance key of the instruction that places seed item ``index``."""

    return f"{PROBE_TASK}:seed_{index}"


def tool_output_instruction(
    *,
    tool: str,
    path: Sequence[str | int | dict[str, str]],
    call_arguments: Mapping[str, str] | None = None,
    result_record: Mapping[str, str] | None = None,
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
    The target's ``call_arguments`` are written only when there are some, and its
    ``result_record`` only when there is one, as MiDojo's codec writes them.
    """

    target: dict[str, Any] = {
        "tool": tool,
        "surface": STRUCTURED_CONTENT,
        "content_block_index": None,
        "content_block_type": None,
        "structured_content_path": list(path),
        "template_path": [],
        "template_key": None,
    }
    if call_arguments:
        target["call_arguments"] = dict(call_arguments)
    if result_record is not None:
        target["result_record"] = dict(result_record)
    return {
        "channel": TOOL_OUTPUT,
        "payload": payload,
        "target": target,
        "occurrence": {"kind": occurrence, "nth": None},
        "transformation": IDENTITY,
        "size_limit": size_limit,
        "mismatch_policy": STRICT,
        "mode": mode,
        "probe_key": key,
    }


def injection_plan(instructions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Return an ``InjectionPlan``: the instructions, no generator permissions, no payload map."""

    return {
        "instructions": list(instructions),
        "generator_permissions": {"tools": [], "fields": [], "modes": []},
        "probe_payloads": [],
    }


def episode(
    *,
    turns: Sequence[str],
    plan: dict[str, Any],
    security: dict[str, Any],
    tools: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an episode document v1: no utility check, no environment, the ``unmanaged``
    runtime, the host interceptor, ``tools`` as the agent may use them, and no verifier.

    The document is closed and every member is written, as MiDojo's ``to_document`` writes
    them, so a reader sees the null fields as well as the set ones.
    """

    return {
        "schema_version": EPISODE_DOCUMENT_V1,
        "suite_name": None,
        "user_task_id": None,
        "injection_task_id": None,
        "environment_type": None,
        "environment": None,
        "runtime": dict(UNMANAGED_RUNTIME),
        "turns": list(turns),
        "plan": plan,
        "security": security,
        "utility": None,
        "verifiers": [],
        "interception": dict(HOST_INTERCEPTION),
        "tools": dict(tools),
    }
