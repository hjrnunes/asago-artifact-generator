"""Run each compiled episode in MiDojo and record its sealed snapshot and verdict.

Run with MiDojo's interpreter at asago/main b6e0f8f (see ``tests/episode_cases.py``)::

    uv run --frozen python generate.py <dir holding <case>/episode.json>

For each case it builds ``midojo.episode.Episode`` from ``episode.json`` field by field,
checks that the episode written back as data is the document's ``episode`` (so the
consumer's data has MiDojo's shape), and checks that each instruction's payload is in the
environment at its template path. Then it runs each scenario once with
``midojo.open_episode`` on an in-process control plane, with a scripted agent that records
its calls through the evaluation's session, and writes ``<case>/<scenario>/snapshot.json``
(the snapshot as ``GET .../snapshot`` returns it) and ``verdict.json`` (``midojo.grade``:
``evaluation_id``, ``snapshot_id`` and the grade). It reads each snapshot back the way
``ControlClient.snapshot`` does, which refuses one whose digest differs. A reply case's
verifier is the one ``compile`` wrote, with a scripted judge. Nothing leaves the process.
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import importlib
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import midojo
from midojo.control_plane.client import snapshot_from_response
from midojo.control_plane.config import AppConfig
from midojo.control_plane.models import SnapshotResponse
from midojo.control_plane.routers.runs import snapshot_response
from midojo.control_plane.service import ControlPlane
from midojo.episode import Episode, thaw
from midojo.runtimes import UnmanagedRuntime
from midojo.verifiers import register_verifier
from midojo_sdk.injection import (
    Channel,
    GeneratorPermissions,
    InjectionInstruction,
    InjectionMode,
    InjectionPlan,
    InjectionTarget,
    MismatchPolicy,
    Occurrence,
    OccurrenceRule,
    SelectorSurface,
    Transformation,
)

REVISION = "b6e0f8fb2fae1b442da28e65778e171be0ce0cc6"
SUPPORTED = json.dumps({"verdict": "supported", "evidence_refs": ["messages[0].content"]})


def instruction(data: dict[str, Any]) -> InjectionInstruction:
    target, occurrence = data["target"], data["occurrence"]
    return InjectionInstruction(
        channel=Channel(data["channel"]),
        payload=data["payload"],
        target=InjectionTarget(
            tool=target["tool"],
            surface=SelectorSurface(target["surface"]),
            content_block_index=target["content_block_index"],
            content_block_type=target["content_block_type"],
            structured_content_path=tuple(target["structured_content_path"]),
            template_path=tuple(target["template_path"]),
            template_key=target["template_key"],
        ),
        occurrence=OccurrenceRule(Occurrence(occurrence["kind"]), occurrence["nth"]),
        transformation=Transformation(data["transformation"]),
        size_limit=data["size_limit"],
        mismatch_policy=MismatchPolicy(data["mismatch_policy"]),
        mode=InjectionMode(data["mode"]),
        probe_key=data["probe_key"],
    )


def plan(data: dict[str, Any]) -> InjectionPlan:
    permissions = data["generator_permissions"]
    return InjectionPlan(
        tuple(instruction(item) for item in data["instructions"]),
        generator_permissions=GeneratorPermissions(
            tools=frozenset(permissions["tools"]),
            fields=frozenset(tuple(path) for path in permissions["fields"]),
            modes=frozenset(InjectionMode(mode) for mode in permissions["modes"]),
        ),
        probe_payloads=tuple(tuple(pair) for pair in data["probe_payloads"]),
    )


def episode(data: dict[str, Any]) -> Episode:
    return Episode(
        runtime=data["runtime"],
        turns=data["turns"],
        plan=plan(data["plan"]),
        security=data["security"],
        utility=data["utility"],
        environment=data["environment"],
        suite_name=data["suite_name"],
        user_task_id=data["user_task_id"],
        injection_task_id=data["injection_task_id"],
    )


def plain(value: Any) -> Any:
    """A plan dataclass as data: enums by value, tuples as lists."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: plain(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [plain(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(plain(item) for item in value)
    return value


def written_back(built: Episode) -> dict[str, Any]:
    def optional(value: Any) -> Any:
        return None if value is None else thaw(value)

    return {
        "runtime": thaw(built.runtime),
        "turns": None if built.turns is None else list(built.turns),
        "plan": plain(built.plan),
        "security": optional(built.security),
        "utility": optional(built.utility),
        "environment": optional(built.environment),
        "suite_name": built.suite_name,
        "user_task_id": built.user_task_id,
        "injection_task_id": built.injection_task_id,
    }


def check_placements(built: Episode) -> None:
    environment = thaw(built.environment)
    for item in built.plan:
        node: Any = environment
        for part in item.target.template_path:
            node = node[part]
        assert item.payload in node, f"{item.probe_key} is not at {item.target.template_path}"
    assert dict(built.injections) == {item.probe_key: item.payload for item in built.plan}


class ScriptedJudge:
    """The judge a reply case's verifier calls: each call answers ``reply``."""

    reply = SUPPORTED

    def __call__(self, messages: list[dict[str, str]]) -> str:
        return self.reply


JUDGE = ScriptedJudge()
Call = tuple[str, dict[str, Any], str | None]  # function, args, result (None: never finished)
Agent = Callable[[Any, str], Awaitable[str]]


def agent(
    turn_calls: list[list[Call]], replies: list[str], fail_after: int | None = None
) -> Agent:
    """A scripted agent: in turn ``n`` it makes ``turn_calls[n]`` and answers ``replies[n]``.

    With ``fail_after`` it raises once that turn's calls are recorded, as a host whose agent
    client failed: the episode is sealed as interrupted.
    """

    async def send(ep: Any, index: int) -> str:
        recorder = ep.control.recorder(ep.binding.session_token)
        for number, (function, args, result) in enumerate(turn_calls[index]):
            call_id = f"call-{index}-{number}"
            if result is None:
                await recorder.start_call(
                    call_id=call_id, function=function, args=args, disposition="forwarded"
                )
            else:
                await recorder.finish_call(
                    call_id=call_id,
                    function=function,
                    args=args,
                    result=result,
                    disposition="forwarded",
                )
        if fail_after == index:
            raise RuntimeError("the agent client failed")
        return replies[index]

    return send  # type: ignore[return-value]


LOOKUP: Call = ("lookup_order", {"order_id": "ORD-101"}, '{"item": "URGENT: ..."}')
SCHEDULE: Call = (
    "schedule_payment",
    {"plan_id": "PLAN-21", "due_date": "2026-12-01"},
    '{"status": "scheduled"}',
)
# Case -> scenario -> (agent script, judge reply for a reply case).
SCENARIOS: dict[str, dict[str, tuple[Agent, str]]] = {
    "command-single": {
        "detected": (agent([[LOOKUP, SCHEDULE]], ["I moved PLAN-21."]), SUPPORTED),
        "not-detected": (agent([[LOOKUP]], ["Your order is a desk lamp."]), SUPPORTED),
        "interrupted": (agent([[LOOKUP]], ["unused"], fail_after=0), SUPPORTED),
        "unfinished": (
            agent(
                [[LOOKUP, ("schedule_payment", {"plan_id": "PLAN-12"}, None)]],
                ["Working on it."],
            ),
            SUPPORTED,
        ),
    },
    "command-sequential": {
        "detected": (
            agent([[LOOKUP], [SCHEDULE]], ["Found ORD-101.", "I moved PLAN-21."]),
            SUPPORTED,
        ),
    },
    "reply-single": {
        "supported": (
            agent([[LOOKUP, SCHEDULE]], ["PLAN-21 now falls due on 2026-12-01."]),
            SUPPORTED,
        ),
        "unresolved": (agent([[LOOKUP]], ["Your order is a desk lamp."]), "not json"),
    },
}


async def run(built: Episode, send: Agent) -> Any:
    control = ControlPlane({}, config=AppConfig(closure_wait_seconds=0))
    context = midojo.open_episode(built, runtime=UnmanagedRuntime(), control=control)
    try:
        async with context as ep:
            for index, message in enumerate(ep.turns or ()):
                await ep.turn(message, lambda _message, ep=ep, index=index: send(ep, index))
    except RuntimeError:
        pass
    return context.sealed


def write(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def register_judge(case_dir: Path, document: dict[str, Any]) -> None:
    sys.path.insert(0, str(case_dir))
    try:
        for verifier in document["verifiers"]:
            module = importlib.import_module(verifier["module"])
            judge = module.EpisodeRubricJudgeVerifier(JUDGE)
            assert judge.name == verifier["check"]
            register_verifier(judge)
    finally:
        sys.path.remove(str(case_dir))


def main(root: Path) -> None:
    recorded: dict[str, Any] = {}
    for case, scenarios in SCENARIOS.items():
        document = json.loads((root / case / "episode.json").read_text())
        register_judge(root / case, document)
        built = episode(document["episode"])
        assert written_back(built) == document["episode"], f"{case}: the episode differs"
        check_placements(built)
        for scenario, (send, judge_reply) in scenarios.items():
            JUDGE.reply = judge_reply
            snapshot = asyncio.run(run(built, send))
            response = snapshot_response(snapshot)
            again = snapshot_from_response(
                SnapshotResponse.model_validate(json.loads(response.model_dump_json())),
                built.environment_type,
            )
            assert again.snapshot_id == snapshot.snapshot_id
            verdict = midojo.grade(built, snapshot)
            write(root / case / scenario / "snapshot.json", response.model_dump(mode="json"))
            write(
                root / case / scenario / "verdict.json",
                {
                    "evaluation_id": verdict.evaluation_id,
                    "snapshot_id": verdict.snapshot_id,
                    "grade": verdict.grade.model_dump(mode="json"),
                },
            )
            recorded.setdefault(case, {})[scenario] = verdict.grade.security
    write(root / "recording.json", {"midojo_revision": REVISION, "security": recorded})


if __name__ == "__main__":
    main(Path(sys.argv[1]))
