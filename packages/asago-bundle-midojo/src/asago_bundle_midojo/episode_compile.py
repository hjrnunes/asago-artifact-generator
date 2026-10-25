"""compile: turn a planted_item package into one resolved MiDojo episode.

The output directory holds ``episode.json`` (``midojo-episode-v1``): the package's
identity, its claim level and delivery, the MiDojo revisions the episode needs, and
``episode``, the MiDojo ``Episode`` as plain data (``episode.py``). Everything in it is
fixed by the package and the MiDojo base: the user turns, the injection plan, the checks
and the environment, so the same inputs give the same bytes.

- The turns are the stimulus's user turns: its history's, then its user text.
- The seed's items are planted in the domain's ledger from the MiDojo base, and each
  becomes an instruction of the plan (``environment.planted_environment``).
- A command attempt is checked with MiDojo's builtin ``tool_call_condition`` predicate,
  whose value is the package's condition. A reply is checked with the rubric judge
  (``bundle_files/asago_episode_judge.py``): the episode names the check and its spec,
  and the directory holds the verifier as the ``asago_verifiers`` package, which the
  runner registers with its judge's URL and model before it builds the episode. An
  episode runs no package setup, so the judge gets no runtime facts.
- There is no utility check, and the runtime is ``unmanaged``: the runner connects the
  agent.

MiDojo runs planted_item packages only. A package whose stimulus delivers its attack in
the user turns (a direct or user-turn package), a single package with history, a
history message that is not the user's, a stimulus with run-time bindings, and a
state_effect claim are capability gaps: ``compile`` raises ``CapabilityGap`` with the
shared record and writes nothing.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring.condition_eval import validate_condition
from asago_artifact_generator.package_io import ArtifactPackage, load_package
from asago_bundle_core.errors import BundleError
from asago_bundle_core.gap import CapabilityGap, capability_gap_record
from asago_bundle_core.slots import contains_marker
from asago_bundle_core.text import canonical_text

from .bundle_files.asago_episode_judge import EPISODE_RUBRIC_NAME, FINAL_TURN, JUDGE_TURNS
from .environment import EnvironmentFault, base_target, planted_environment
from .episode import episode, injection_plan

SCHEMA_VERSION = "midojo-episode-v1"
TOOL = "midojo"
# ``Episode`` with its injection plan, and ``midojo.grade`` over a sealed snapshot,
# are on asago/main at b6e0f8f.
TOOL_REVISION_RANGE = ">=b6e0f8fb"
EPISODE_FILE = "episode.json"
SEED_MEMBER = "seed.json"
CONDITION_MEMBER = "tool_call_condition.json"
JUDGE_MEMBER = "judge.json"
PLANTED_ITEM = "planted_item"
COMMAND_ATTEMPT = "command_attempt"
REPLY = "reply"
CLAIM_LEVELS = ("command_attempt", "reply", "state_effect")
SINGLE = "single"
SEQUENTIAL = "sequential"
# MiDojo's builtin predicate; its value is the condition object itself.
TOOL_CALL_CONDITION = "tool_call_condition"
VERIFIERS = "asago_verifiers"
BUNDLE_FILES = "asago_bundle_midojo.bundle_files"
# Path in the output -> (package, file of that package it copies).
VERIFIER_CODE = {
    f"{VERIFIERS}/asago_rubric.py": ("asago_artifact_generator.authoring", "rubric_judge.py"),
    f"{VERIFIERS}/asago_episode_judge.py": (BUNDLE_FILES, "asago_episode_judge.py"),
}
VERIFIERS_INIT = f"{VERIFIERS}/__init__.py"
VERIFIERS_DOC = '"""The verifiers a reply episode names; see asago_episode_judge.register."""\n'


class CompileError(BundleError):
    """The package cannot become a MiDojo episode."""


def compile_episode(
    package_dir: str | Path,
    out_dir: str | Path,
    *,
    midojo_base: str | Path,
    domain: str,
    judge_turns: str = FINAL_TURN,
) -> dict[str, Any]:
    """Write the episode of one package into ``out_dir`` and return the document.

    ``midojo_base`` is mini-agents' ``mini-agents-midojo-base-v1`` file; ``domain`` picks
    the ledger the episode starts from and the slots the seed may target. ``judge_turns``
    (``final`` or ``all``) is which turns a reply's judge grades.
    """

    if judge_turns not in JUDGE_TURNS:
        raise CompileError(f"judge_turns is one of {sorted(JUDGE_TURNS)}, not {judge_turns!r}")
    out = Path(out_dir)
    package = _load(Path(package_dir))
    stimulus = _json_member(package, "stimulus.json")
    level = claim_level(_json_member(package, "plan.json"))
    delivery = SEQUENTIAL if delivery_mode(stimulus) == SEQUENTIAL else SINGLE
    reasons = gap_reasons(level, stimulus)
    if reasons:
        raise CapabilityGap(capability_gap_record(TOOL, package, delivery, "; ".join(reasons)))
    if SEED_MEMBER not in package.members:
        raise CompileError(f"a {PLANTED_ITEM} package has no {SEED_MEMBER}")
    turns = user_turns(stimulus)
    base = _read_base(Path(midojo_base))
    try:
        environment, instructions = planted_environment(
            base_target(base, domain), _json_member(package, SEED_MEMBER)
        )
    except EnvironmentFault as exc:
        raise CompileError(str(exc)) from exc
    security = _security_check(package, level, turns, judge_turns)
    document = {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL,
        "tool_revision_range": TOOL_REVISION_RANGE,
        "package_id": package.manifest.package_id,
        "scenario_id": package.manifest.scenario_id,
        "package_digest": package.manifest.manifest_digest,
        "claim_level": level,
        "delivery": delivery,
        "verifiers": _verifiers(level),
        "episode": episode(
            turns=turns,
            plan=injection_plan(instructions),
            security=security,
            environment=environment,
        ),
    }
    _write(out, document, VERIFIER_CODE if level == REPLY else {})
    return document


def claim_level(plan: Any) -> str:
    claim = plan.get("observation_claim") if isinstance(plan, dict) else None
    level = claim.get("claim_level") if isinstance(claim, dict) else None
    if level not in CLAIM_LEVELS:
        raise CompileError(f"unsupported claim level: {level!r}")
    return level


def delivery_mode(stimulus: Any) -> Any:
    return stimulus.get("mode", SINGLE) if isinstance(stimulus, dict) else SINGLE


def gap_reasons(level: str, stimulus: Any) -> list[str]:
    """Return why MiDojo cannot run the package, one text per cause; empty when it can."""

    reasons = [
        _planted_reason(stimulus),
        _delivery_reason(stimulus),
        _binding_reason(stimulus),
        _claim_reason(level),
    ]
    return [reason for reason in reasons if reason]


def _planted_reason(stimulus: Any) -> str | None:
    """MiDojo runs a package whose attack is a planted item, and no other."""

    delivery = stimulus.get("delivery") if isinstance(stimulus, dict) else None
    if delivery == PLANTED_ITEM:
        return None
    return (
        f"MiDojo runs {PLANTED_ITEM} packages only; this stimulus delivers its attack "
        f"in the user turns ({delivery!r})"
    )


def _delivery_reason(stimulus: Any) -> str | None:
    """An episode sends user turns only, each as its own message in one conversation.

    A sequential package's user turns are those turns. A single package sends its
    history and user text in one request; as separate turns the target would answer
    each, which changes the attack, so a single package with history is a gap.
    """

    history = stimulus.get("history") if isinstance(stimulus, dict) else None
    sequential = delivery_mode(stimulus) == SEQUENTIAL
    if not history and not sequential:
        return None
    if not (
        isinstance(history, list)
        and all(isinstance(item, dict) and item.get("role") == "user" for item in history)
    ):
        return "MiDojo sends user turns only; this stimulus holds a non-user message"
    if sequential:
        return None
    return (
        "a single package sends its history and user text in one prompt; MiDojo would "
        "send each as its own turn"
    )


def _binding_reason(stimulus: Any) -> str | None:
    """An episode's turns are fixed text; a binding would need a value at run time."""

    slots = stimulus.get("slots") if isinstance(stimulus, dict) else None
    if not slots:
        return None
    return "an episode sends fixed user turns; this stimulus fills run-time bindings into them"


def _claim_reason(level: str) -> str | None:
    """The command attempt and the reply are graded here; the state effect is not."""

    if level not in (COMMAND_ATTEMPT, REPLY):
        return f"the {level} claim is not graded by this adapter"
    return None


def user_turns(stimulus: dict[str, Any]) -> list[str]:
    """Return the user turns: the history's, then the user text.

    A sequential stimulus's ``turn_count`` must state their number.
    """

    turns = [item.get("content") for item in stimulus.get("history") or []]
    turns.append(stimulus.get("user_text"))
    if not all(isinstance(turn, str) and turn for turn in turns):
        raise CompileError("the stimulus's user turns are not all non-empty text")
    if delivery_mode(stimulus) == SEQUENTIAL:
        stated = stimulus.get("turn_count")
        if type(stated) is not int or stated != len(turns) or len(turns) < 2:
            raise CompileError(
                f"sequential turn_count {stated!r} does not match {len(turns)} user turns"
            )
    if contains_marker(turns):
        raise CompileError("package content contains a slot marker")
    return turns


def _security_check(
    package: ArtifactPackage, level: str, turns: list[str], judge_turns: str
) -> dict[str, Any]:
    if level == REPLY:
        return {
            EPISODE_RUBRIC_NAME: {
                "rubric": _rubric(package),
                "turns": list(turns),
                "judge_turns": judge_turns,
            }
        }
    return {TOOL_CALL_CONDITION: _condition(package)}


def _verifiers(level: str) -> list[dict[str, str]]:
    """Return the verifiers the runner registers before it builds the episode."""

    if level != REPLY:
        return []
    return [{"check": EPISODE_RUBRIC_NAME, "module": f"{VERIFIERS}.asago_episode_judge"}]


def _condition(package: ArtifactPackage) -> dict[str, Any]:
    condition = _json_member(package, CONDITION_MEMBER)
    if contains_marker(condition):
        raise CompileError("package content contains a slot marker")
    problem = validate_condition(condition)
    if problem:
        raise CompileError(f"{CONDITION_MEMBER}: condition is invalid: {problem}")
    return condition


def _rubric(package: ArtifactPackage) -> dict[str, Any]:
    judge = _json_member(package, JUDGE_MEMBER)
    if not isinstance(judge, dict) or not isinstance(judge.get("question"), str):
        raise CompileError(f"{JUDGE_MEMBER} has no question text")
    rubric = {
        "question": judge["question"],
        "criteria": judge.get("criteria", []),
        "facts": judge.get("facts", []),
    }
    if contains_marker(rubric):
        raise CompileError("package content contains a slot marker")
    return rubric


def _read_base(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompileError(f"the MiDojo base cannot be read: {exc}") from exc


def _load(package_dir: Path) -> ArtifactPackage:
    try:
        return load_package(package_dir)
    except (OSError, ValueError) as exc:
        raise CompileError(f"package cannot be loaded: {exc}") from exc


def _json_member(package: ArtifactPackage, name: str) -> Any:
    content = package.members.get(name)
    if content is None:
        raise CompileError(f"package has no {name}")
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompileError(f"package {name} is not JSON: {exc}") from exc


def _write(out: Path, document: dict[str, Any], code: dict[str, tuple[str, str]]) -> None:
    if out.exists() and any(out.iterdir()):
        raise CompileError(f"output directory is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    (out / EPISODE_FILE).write_text(canonical_text(document), encoding="utf-8")
    if code:
        (out / VERIFIERS).mkdir()
        (out / VERIFIERS_INIT).write_text(VERIFIERS_DOC, encoding="utf-8")
    for name, (package, original) in code.items():
        (out / name).write_bytes(files(package).joinpath(original).read_bytes())
