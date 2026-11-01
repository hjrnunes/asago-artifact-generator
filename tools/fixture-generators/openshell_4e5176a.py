"""Seal the referral result-record episode in MiDojo 4e5176a with OpenShell observations.

Run with MiDojo's interpreter at 4e5176a, naming that revision, since the interpreter may
run from an export with no Git history. With an export of 4e5176a in an empty directory
(``git -C <midojo> archive 4e5176a | tar -x -C <dir>``, then ``uv sync --frozen --extra
dev`` there)::

    cd <that dir> && uv run --frozen --extra dev python \\
        <consumer>/tools/fixture-generators/openshell_4e5176a.py \\
        <the full 4e5176a SHA> \\
        <consumer>/packages/asago-bundle-midojo/tests/fixtures/result-record/referral \\
        <empty dir>

then copy ``<empty dir>`` over ``fixtures/openshell-4e5176a/``.

From 4e5176a MiDojo leaves an observation's later list fields out of the snapshot digest
while they are empty (``observations.digest_content``, the model's
``DIGEST_OMITS_WHEN_EMPTY``); today only OpenShell's ``http_requests``. Each scenario runs
the episode ``result-record/referral/episode.json`` once with ``midojo.open_episode`` on an
in-process control plane, its host interceptor in front of a scripted upstream of the
clinic fixture target's shape, as ``result_record.py`` does, but with a scripted
runtime in place of the unmanaged one: its ``observe`` returns ``observations["openshell"]``
built from fixed OCSF log lines through MiDojo's ``parse_ocsf_lines``, field for field as
``OpenShellRuntime.observe`` builds them. No sandbox, model or target is involved, and
nothing leaves the process but loopback connections to the interceptor.

``http-requests-empty`` logs no ``HTTP:`` line, so its ``http_requests`` is ``[]``;
``http-request`` logs one. The script writes ``<scenario>/snapshot.json`` (``GET
.../snapshot``) and ``verdict.json`` (``midojo.grade``), reading each snapshot back as
``ControlClient.snapshot`` does, which refuses one whose digest differs, and checks that
the JSON holds ``http_requests`` as MiDojo recorded it. ``recording.json`` names the
revision given and keeps each scenario's snapshot ID, security grade and the number of HTTP
requests observed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import midojo
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from midojo.control_plane.client import snapshot_from_response
from midojo.control_plane.config import AppConfig
from midojo.control_plane.models import SnapshotResponse
from midojo.control_plane.routers.runs import snapshot_response
from midojo.control_plane.service import ControlPlane
from midojo.control_plane.state import snapshot_digest
from midojo.episode import Episode
from midojo.interception_host import HostInterception
from midojo.runtimes import UnmanagedRuntime
from midojo.runtimes.openshell import (
    OBSERVATIONS_SOURCE,
    HttpRequestRecord,
    OpenShellObservations,
    ProcessRecord,
)
from midojo.runtimes.openshell.logs import parse_ocsf_lines
from midojo_sdk.injection import InjectionPlan
from pydantic import BaseModel

REFERRALS = {"P-1": "REF-101", "P-2": "REF-104"}
CALLS = [
    ("get_referral", {"patient_id": "P-1"}),
    ("escalate_to_clinician", {"patient_id": "P-1", "reason": "urgent"}),
]
REPLY = "I escalated your case to a clinician."
# What the sandbox logged either way: the agent's process and its connection to the clinic.
LOGGED = [
    "PROC:LAUNCH [INFO] python3(42) [cmd:python3 /workspace/agent.py]",
    "NET:OPEN [INFO] ALLOWED /usr/bin/python3(0) -> clinic.internal:8080"
    " [policy:clinic engine:opa]",
    "PROC:TERMINATE [INFO] python3(42) [exit:0]",
]
# Scenario -> its OCSF log lines.
SCENARIOS = {
    "http-requests-empty": LOGGED,
    "http-request": [
        *LOGGED[:2],
        "HTTP:GET [INFO] ALLOWED GET http://clinic.internal:8080/referrals?patient_id=P-1",
        LOGGED[2],
    ],
}


def scripted_upstream() -> FastMCP:
    """The clinic target's referral and escalation tools."""

    upstream = FastMCP("clinic")

    @upstream.tool()
    def get_referral(patient_id: str) -> dict:
        referral = REFERRALS[patient_id]
        return {
            "referral_id": referral,
            "patient_id": patient_id,
            "reason": f"Review of {referral}.",
        }

    @upstream.tool()
    def escalate_to_clinician(patient_id: str, reason: str) -> dict:
        return {"status": f"escalated {patient_id}"}

    return upstream


class ObservedRuntime(UnmanagedRuntime):
    """An agent MiDojo does not start, observed through fixed OpenShell OCSF lines."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def observe(self) -> dict[str, BaseModel]:
        ocsf = parse_ocsf_lines(self.lines)
        observed = OpenShellObservations(
            files_created=["/workspace/notes.txt"],
            workdir_new_file_contents={"/workspace/notes.txt": "escalated P-1\n"},
            processes=[ProcessRecord(**asdict(p)) for p in ocsf.processes],
            network_calls_allowed=ocsf.network_allowed_endpoints,
            network_calls_blocked=ocsf.network_blocked_endpoints,
            network_calls_interrupted=ocsf.network_interrupted_endpoints,
            network_callers=ocsf.network_callers,
            security_findings=[f.title for f in ocsf.findings],
            http_requests=[HttpRequestRecord.of(e) for e in ocsf.http_requests],
            image="localhost/clinic-agent:latest",
        )
        return {OBSERVATIONS_SOURCE: observed}


def episode(data: dict[str, Any]) -> Episode:
    return Episode(
        runtime=data["runtime"],
        turns=data["turns"],
        plan=InjectionPlan.from_data(data["plan"]),
        security=data["security"],
        utility=data["utility"],
        environment=data["environment"],
        suite_name=data["suite_name"],
        user_task_id=data["user_task_id"],
        injection_task_id=data["injection_task_id"],
    )


async def run(built: Episode, document: dict[str, Any], lines: list[str]) -> Any:
    assert document["episode"]["interception"] == {"mode": "host", "upstream": None}
    allowed = document["episode"]["tools"]["allowed"]

    async def send(ep: Any, _message: str) -> str:
        endpoint = ep.binding.tool_endpoint
        transport = StreamableHttpTransport(endpoint.url, headers=endpoint.headers)
        async with Client(transport) as client:
            for name, args in CALLS:
                assert name in allowed
                await client.call_tool_mcp(name, args)
        return REPLY

    control = ControlPlane({}, config=AppConfig(closure_wait_seconds=0))
    context = midojo.open_episode(
        built,
        runtime=ObservedRuntime(lines),
        control=control,
        interception=HostInterception(upstream=scripted_upstream()),
    )
    async with context as ep:
        for message in ep.turns or ():
            await ep.turn(message, lambda message, ep=ep: send(ep, message))
    return context


def write(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def main(revision: str, episode_dir: Path, root: Path) -> None:
    document = json.loads((episode_dir / "episode.json").read_text())
    built = episode(document["episode"])
    recorded: dict[str, Any] = {}
    for scenario, lines in SCENARIOS.items():
        context = asyncio.run(run(built, document, lines))
        snapshot = context.sealed
        response = snapshot_response(snapshot)
        data = response.model_dump(mode="json")
        again = snapshot_from_response(
            SnapshotResponse.model_validate(json.loads(response.model_dump_json())),
            built.environment_type,
        )
        assert again.snapshot_id == snapshot.snapshot_id == snapshot_digest(again)
        requests = data["observations"][OBSERVATIONS_SOURCE]["http_requests"]
        assert len(requests) == sum(line.startswith("HTTP:") for line in lines)
        verdict = midojo.grade(built, snapshot)
        write(root / scenario / "snapshot.json", data)
        write(
            root / scenario / "verdict.json",
            {
                "evaluation_id": verdict.evaluation_id,
                "snapshot_id": verdict.snapshot_id,
                "grade": verdict.grade.model_dump(mode="json"),
            },
        )
        recorded[scenario] = {
            "snapshot_id": snapshot.snapshot_id,
            "security": verdict.grade.security,
            "http_requests": len(requests),
        }
    write(root / "recording.json", {"midojo_revision": revision, "scenarios": recorded})


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
