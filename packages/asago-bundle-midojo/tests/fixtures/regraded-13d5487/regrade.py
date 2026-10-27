"""Read the sealed fixture runs with MiDojo at 13d5487: snapshot form, grade and delivery facts.

Run with MiDojo's interpreter at 13d5487 (see ``tests/episode_cases.py``), naming that
revision, since the interpreter may run from an export with no Git history::

    uv run --frozen python regrade.py <midojo revision> <consumer>/.../fixtures/episode <out dir>

It runs no episode and starts no server. From 13d5487 each ``CallInjectionOutcome`` also
holds ``selected``, ``transformation`` and ``emitted``, None when the interception layer
did not report them, and the snapshot digest encodes them, so a record sealed at 3824922
reads back at 13d5487 with the three null and under a new snapshot ID. For each scenario
under ``fixtures/episode/`` this script takes the case's ``episode.json`` and the snapshot
MiDojo sealed at 3824922, adds the three as null to each outcome, names the result by
MiDojo's encoding of its content (``state._canonical`` and ``state._dumps``) and rebuilds it
as ``ControlClient.snapshot`` does (``snapshot_from_response``, which refuses a snapshot
whose ``snapshot_digest`` differs from its ID). It writes:

- ``<case>/<scenario>/snapshot.json``: that snapshot, as ``GET .../snapshot`` returns it
  at 13d5487;
- ``verdict.json``: ``midojo.grade`` of it, whose grade carries ``attempted``,
  ``blocked``, ``effect_observed`` and ``injection_delivered``; every other field of the
  grade must be the 3824922 verdict's;
- ``delivery.json``: the ``midojo-delivery-v1`` document a runner writes beside the sealed
  snapshot: the evaluation and snapshot IDs, ``midojo.delivery.delivery_facts`` of the
  episode's plan and the snapshot's calls, and ``midojo.delivery.exposure`` of them. No
  record reports what it emitted, so the exposure is unknown.

``reported/command-single/detected/`` is that scenario with each applied outcome's
delivery facts as the 13d5487 interceptor reports a placement in a structured field that
its text block mirrors (``selected`` true, the instruction's ``transformation``, and
``emitted`` both parts, in the order ``midojo_sdk.mcp._emitted`` lists them), named,
rebuilt, graded and written the same way. No interceptor sealed it: the facts are derived;
the snapshot ID, the grade and the delivery document are MiDojo's.

``recording.json`` names the revision given and, per scenario, the four grade fields and
the combined exposure at each boundary.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import midojo
from midojo.control_plane.client import snapshot_from_response
from midojo.control_plane.models import SnapshotResponse
from midojo.control_plane.state import _canonical, _dumps
from midojo.delivery import delivery_facts, exposure
from midojo.episode import Episode
from midojo.verifiers import register_verifier
from midojo_sdk.injection import InjectionPlan

ATTACK_OUTCOME = ("attempted", "blocked", "effect_observed", "injection_delivered")
BOUNDARIES = ("emitted", "agent_tool_result", "model_input")
SUPPORTED = json.dumps({"verdict": "supported", "evidence_refs": ["messages[0].content"]})
# The judge reply each reply scenario was graded with at 3824922 (``episode/generate.py``).
JUDGE_REPLIES = {"supported": SUPPORTED, "unresolved": "not json"}
REPORTED = ("command-single", "detected")
# What a 3824922 record's outcome lacks: unreported at 13d5487.
UNREPORTED = {"selected": None, "transformation": None, "emitted": None}


class ScriptedJudge:
    """The judge a reply case's verifier calls: each call answers ``reply``."""

    reply = SUPPORTED

    def __call__(self, messages: list[dict[str, str]]) -> str:
        return self.reply


JUDGE = ScriptedJudge()


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


def register_judge(case_dir: Path, document: dict[str, Any]) -> None:
    sys.path.insert(0, str(case_dir))
    try:
        for verifier in document["verifiers"]:
            module = importlib.import_module(verifier["module"])
            register_verifier(module.EpisodeRubricJudgeVerifier(JUDGE))
    finally:
        sys.path.remove(str(case_dir))


def with_facts(response: dict[str, Any], facts: Any) -> dict[str, Any]:
    """``response`` with ``facts(outcome)`` merged into each outcome, named by its content."""

    changed = copy.deepcopy(response)
    for call in changed["function_calls"]:
        for outcome in call.get("injection_outcomes") or ():
            outcome.update(facts(outcome))
    content = {key: value for key, value in changed.items() if key != "snapshot_id"}
    digest = hashlib.sha256(_dumps(_canonical(content)).encode()).hexdigest()
    changed["snapshot_id"] = f"sha256:{digest}"
    return changed


def reported_facts(plan: dict[str, Any]) -> Any:
    def facts(outcome: dict[str, Any]) -> dict[str, Any]:
        assert outcome["status"] == "applied"
        instruction = plan["instructions"][outcome["instruction_index"]]
        return {
            "selected": True,
            "transformation": instruction["transformation"],
            "emitted": ["structured_content", "content_block"],
        }

    return facts


def graded(built: Episode, response: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the verdict and the delivery document of the snapshot ``response`` carries."""

    snapshot = snapshot_from_response(
        SnapshotResponse.model_validate(response), built.environment_type
    )
    verdict = midojo.grade(built, snapshot)
    delivery = {
        "schema_version": "midojo-delivery-v1",
        "evaluation_id": snapshot.evaluation_id,
        "snapshot_id": snapshot.snapshot_id,
        "delivery": [
            fact.model_dump(mode="json")
            for fact in delivery_facts(built.plan, snapshot.function_calls)
        ],
        "exposure": exposure(
            built.plan,
            snapshot.function_calls,
            turns=len(snapshot.turns),
            evidence_status=snapshot.evidence_status,
        ).model_dump(mode="json"),
    }
    written = {
        "evaluation_id": verdict.evaluation_id,
        "snapshot_id": verdict.snapshot_id,
        "grade": verdict.grade.model_dump(mode="json"),
    }
    return written, delivery


def write(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def record(out: Path, built: Episode, response: dict[str, Any]) -> dict[str, Any]:
    """Grade ``response``, write the run into ``out`` and return its verdict."""

    verdict, delivery = graded(built, response)
    write(out / "snapshot.json", response)
    write(out / "verdict.json", verdict)
    write(out / "delivery.json", delivery)
    return {
        **{name: verdict["grade"][name] for name in ATTACK_OUTCOME},
        "exposure": {boundary: delivery["exposure"][boundary] for boundary in BOUNDARIES},
        "verdict": verdict,
    }


def main(revision: str, episodes: Path, out: Path) -> None:
    recorded: dict[str, Any] = {}
    for case_dir in sorted(path for path in episodes.iterdir() if path.is_dir()):
        case = case_dir.name
        if not (case_dir / "episode.json").is_file():
            continue
        document = json.loads((case_dir / "episode.json").read_text())
        register_judge(case_dir, document)
        built = episode(document["episode"])
        for scenario_dir in sorted(path for path in case_dir.iterdir() if path.is_dir()):
            if not (scenario_dir / "snapshot.json").is_file():
                continue
            scenario = scenario_dir.name
            JUDGE.reply = JUDGE_REPLIES.get(scenario, SUPPORTED)
            sealed = json.loads((scenario_dir / "snapshot.json").read_text())
            sealed_verdict = json.loads((scenario_dir / "verdict.json").read_text())
            # (where the run is written, its recording, the snapshot)
            runs = [(out, recorded, with_facts(sealed, lambda _outcome: UNREPORTED))]
            if (case, scenario) == REPORTED:
                facts = reported_facts(document["episode"]["plan"])
                reported = recorded.setdefault("reported", {})
                runs.append((out / "reported", reported, with_facts(sealed, facts)))
            for root, recording, response in runs:
                summary = record(root / case / scenario, built, response)
                verdict = summary.pop("verdict")
                grade = {k: v for k, v in verdict["grade"].items() if k not in ATTACK_OUTCOME}
                assert grade == sealed_verdict["grade"], f"{case}/{scenario}: the grade changed"
                assert verdict["evaluation_id"] == sealed_verdict["evaluation_id"]
                recording.setdefault(case, {})[scenario] = summary
    write(out / "recording.json", {"midojo_revision": revision, "scenarios": recorded})


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
