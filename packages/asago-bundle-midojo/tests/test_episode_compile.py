"""compile: a planted_item package becomes one resolved MiDojo episode.

``fixtures/episode/<case>/`` holds what compile wrote for each case of
``episode_cases.CASES``; MiDojo built each of those episodes from its data at b6e0f8f
and wrote it back unchanged (``fixtures/episode/generate.py``), so a compile that still
writes those bytes still writes MiDojo's ``Episode`` shape.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from episode_cases import (
    BASE,
    CASES,
    CONDITION,
    DOMAIN,
    EPISODES,
    JUDGE,
    PLANTED,
    SEQUENTIAL_TURNS,
    SINGLE_TURNS,
    planted_stimulus,
    seed_document,
    seed_item,
    write_case_package,
    write_planted_package,
)

from asago_bundle_core.conformance import check_gap
from asago_bundle_core.gap import CapabilityGap
from asago_bundle_core.testing import sequential_stimulus, single_stimulus
from asago_bundle_midojo.bundle_files.asago_episode_judge import EPISODE_RUBRIC_NAME
from asago_bundle_midojo.episode import EPISODE_FIELDS
from asago_bundle_midojo.episode_compile import CompileError, compile_episode

LEDGER = json.loads(BASE.read_text(encoding="utf-8"))["targets"][DOMAIN]["environment"]
INSTRUCTION_FIELDS = {
    "channel",
    "payload",
    "target",
    "occurrence",
    "transformation",
    "size_limit",
    "mismatch_policy",
    "mode",
    "probe_key",
}
TARGET_FIELDS = {
    "tool",
    "surface",
    "content_block_index",
    "content_block_type",
    "structured_content_path",
    "template_path",
    "template_key",
}


def compiled(package: Path, out: Path, **options: Any) -> dict[str, Any]:
    return compile_episode(package, out, midojo_base=BASE, domain=DOMAIN, **options)


def written(root: Path) -> dict[str, bytes]:
    """Return what compile wrote under ``root``; a fixture's scenario files are not compile's."""

    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.name not in ("snapshot.json", "verdict.json")
    }


@pytest.mark.parametrize("case", sorted(CASES))
def test_compile_writes_the_episode_midojo_built_from_the_fixture(
    tmp_path: Path, case: str
) -> None:
    compiled(write_case_package(tmp_path / "packages", case), tmp_path / "out")

    assert written(tmp_path / "out") == written(EPISODES / case)


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_same_package_compiles_to_the_same_bytes(tmp_path: Path, case: str) -> None:
    package = write_case_package(tmp_path / "packages", case)
    compiled(package, tmp_path / "first")
    compiled(package, tmp_path / "second")

    assert written(tmp_path / "first") == written(tmp_path / "second")


def test_the_returned_document_is_the_one_written(tmp_path: Path) -> None:
    document = compiled(
        write_case_package(tmp_path / "packages", "command-single"), tmp_path / "out"
    )

    assert document == json.loads((tmp_path / "out/episode.json").read_text(encoding="utf-8"))


def test_the_document_names_the_package_and_holds_an_episode_of_midojo_s_fields(
    tmp_path: Path,
) -> None:
    package = write_case_package(tmp_path / "packages", "command-single")
    document = compiled(package, tmp_path / "out")

    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert document["schema_version"] == "midojo-episode-v1"
    assert (document["tool"], document["tool_revision_range"]) == ("midojo", ">=b6e0f8fb")
    assert document["package_id"] == "SCN-101-SCN-101"
    assert document["scenario_id"] == "SCN-101"
    assert document["package_digest"] == manifest["manifest_digest"]
    assert (document["claim_level"], document["delivery"]) == ("command_attempt", "single")
    assert set(document["episode"]) == set(EPISODE_FIELDS)


def test_a_command_attempt_episode_sends_the_user_turn_and_checks_the_condition(
    tmp_path: Path,
) -> None:
    episode = compiled(
        write_case_package(tmp_path / "packages", "command-single"), tmp_path / "out"
    )["episode"]

    assert episode["turns"] == list(SINGLE_TURNS)
    assert episode["security"] == {"tool_call_condition": CONDITION}
    assert episode["utility"] is None
    assert episode["runtime"] == {"type": "unmanaged"}
    assert (episode["suite_name"], episode["user_task_id"], episode["injection_task_id"]) == (
        None,
        None,
        None,
    )


def test_a_seed_item_becomes_an_environment_template_instruction(tmp_path: Path) -> None:
    episode = compiled(
        write_case_package(tmp_path / "packages", "command-single"), tmp_path / "out"
    )["episode"]

    (instruction,) = episode["plan"]["instructions"]
    assert set(instruction) == INSTRUCTION_FIELDS
    assert set(instruction["target"]) == TARGET_FIELDS
    assert instruction == {
        "channel": "environment_state",
        "payload": PLANTED,
        "target": {
            "tool": None,
            "surface": "template",
            "content_block_index": None,
            "content_block_type": None,
            "structured_content_path": [],
            "template_path": ["orders", "ORD-101", "item"],
            "template_key": "planted_item:seed_0",
        },
        "occurrence": {"kind": "first", "nth": None},
        "transformation": "identity",
        "size_limit": None,
        "mismatch_policy": "strict",
        "mode": "replace",
        "probe_key": "planted_item:seed_0",
    }
    assert episode["plan"]["probe_payloads"] == [["planted_item:seed_0", PLANTED]]
    assert episode["plan"]["generator_permissions"] == {"tools": [], "fields": [], "modes": []}


def test_the_episode_environment_is_resolved_with_the_planted_text(tmp_path: Path) -> None:
    episode = compiled(
        write_case_package(tmp_path / "packages", "command-single"), tmp_path / "out"
    )["episode"]

    environment = episode["environment"]
    assert environment["orders"]["ORD-101"]["item"] == PLANTED
    assert environment["orders"]["ORD-102"] == LEDGER["orders"]["ORD-102"]
    assert {key: value for key, value in environment.items() if key != "orders"} == {
        key: value for key, value in LEDGER.items() if key != "orders"
    }


def test_an_append_item_follows_the_field_text_and_items_apply_in_order(tmp_path: Path) -> None:
    seed = seed_document(
        seed_item("first"),
        seed_item("second", mode="append"),
        seed_item("policy", slot="klarna.policy_doc.body", record="POL-REFUND", mode="append"),
    )
    package = write_planted_package(tmp_path / "packages", seed=seed)
    episode = compiled(package, tmp_path / "out")["episode"]

    assert episode["environment"]["orders"]["ORD-101"]["item"] == "first second"
    assert episode["environment"]["policy_docs"]["POL-REFUND"]["body"] == (
        f"{LEDGER['policy_docs']['POL-REFUND']['body']} policy"
    )
    instructions = episode["plan"]["instructions"]
    assert [item["target"]["template_path"] for item in instructions] == [
        ["orders", "ORD-101", "item"],
        ["orders", "ORD-101", "item"],
        ["policy_docs", "POL-REFUND", "body"],
    ]
    assert [item["probe_key"] for item in instructions] == [
        "planted_item:seed_0",
        "planted_item:seed_1",
        "planted_item:seed_2",
    ]
    assert episode["plan"]["probe_payloads"] == [
        ["planted_item:seed_0", "first"],
        ["planted_item:seed_1", "second"],
        ["planted_item:seed_2", "policy"],
    ]


def test_a_sequential_package_sends_each_user_turn(tmp_path: Path) -> None:
    document = compiled(
        write_case_package(tmp_path / "packages", "command-sequential"), tmp_path / "out"
    )

    assert document["delivery"] == "sequential"
    assert document["episode"]["turns"] == list(SEQUENTIAL_TURNS)


def test_a_reply_episode_names_the_rubric_check_and_writes_its_verifier(tmp_path: Path) -> None:
    out = tmp_path / "out"
    document = compiled(write_case_package(tmp_path / "packages", "reply-single"), out)

    assert document["episode"]["security"] == {
        EPISODE_RUBRIC_NAME: {"rubric": JUDGE, "turns": list(SINGLE_TURNS), "judge_turns": "final"}
    }
    assert document["verifiers"] == [
        {"check": EPISODE_RUBRIC_NAME, "module": "asago_verifiers.asago_episode_judge"}
    ]
    authoring = files("asago_artifact_generator.authoring")
    bundled = files("asago_bundle_midojo.bundle_files")
    assert (out / "asago_verifiers/asago_rubric.py").read_bytes() == (
        authoring.joinpath("rubric_judge.py").read_bytes()
    )
    assert (out / "asago_verifiers/asago_episode_judge.py").read_bytes() == (
        bundled.joinpath("asago_episode_judge.py").read_bytes()
    )
    assert (out / "asago_verifiers/__init__.py").is_file()


def test_a_reply_episode_judges_the_turns_judge_turns_names(tmp_path: Path) -> None:
    package = write_planted_package(
        tmp_path / "packages", claim_level="reply", stimulus=planted_stimulus(SEQUENTIAL_TURNS)
    )
    spec = compiled(package, tmp_path / "out", judge_turns="all")["episode"]["security"]

    assert spec[EPISODE_RUBRIC_NAME]["turns"] == list(SEQUENTIAL_TURNS)
    assert spec[EPISODE_RUBRIC_NAME]["judge_turns"] == "all"


def test_a_command_attempt_episode_writes_no_verifier(tmp_path: Path) -> None:
    out = tmp_path / "out"
    document = compiled(write_case_package(tmp_path / "packages", "command-single"), out)

    assert document["verifiers"] == []
    assert sorted(path.name for path in out.iterdir()) == ["episode.json"]


GAPS = {
    "direct-single": (
        {"stimulus": single_stimulus()},
        "MiDojo runs planted_item packages only; this stimulus delivers its attack in the "
        "user turns ('direct')",
    ),
    "direct-user-message": (
        {"stimulus": {**single_stimulus(), "delivery": "direct_user_message"}},
        "MiDojo runs planted_item packages only; this stimulus delivers its attack in the "
        "user turns ('direct_user_message')",
    ),
    "user-turns": (
        {"stimulus": {**sequential_stimulus(), "delivery": "sequential_user_turns"}},
        "MiDojo runs planted_item packages only; this stimulus delivers its attack in the "
        "user turns ('sequential_user_turns')",
    ),
    "single-with-history": (
        {
            "stimulus": {
                **planted_stimulus(),
                "history": [{"role": "user", "content": "Hello."}],
            }
        },
        "a single package sends its history and user text in one prompt; MiDojo would "
        "send each as its own turn",
    ),
    "non-user-history": (
        {
            "stimulus": {
                **planted_stimulus(SEQUENTIAL_TURNS),
                "history": [{"role": "assistant", "content": "Hello."}],
            }
        },
        "MiDojo sends user turns only; this stimulus holds a non-user message",
    ),
    "bindings": (
        {"stimulus": {**planted_stimulus(), "slots": [{"name": "order"}]}},
        "an episode sends fixed user turns; this stimulus fills run-time bindings into them",
    ),
    "state-effect": (
        {"claim_level": "state_effect"},
        "the state_effect claim is not graded by this adapter",
    ),
}


@pytest.mark.parametrize("name", sorted(GAPS))
def test_a_package_midojo_cannot_run_is_a_capability_gap_and_writes_nothing(
    tmp_path: Path, name: str
) -> None:
    options, reason = GAPS[name]
    package = write_planted_package(tmp_path / "packages", **options)

    with pytest.raises(CapabilityGap) as raised:
        compiled(package, tmp_path / "out")

    assert raised.value.record["reason"] == reason
    assert raised.value.record["tool"] == "midojo"
    assert check_gap(raised.value.record, package) == []
    assert not (tmp_path / "out").exists()


def test_a_gap_names_every_cause(tmp_path: Path) -> None:
    package = write_planted_package(
        tmp_path / "packages", stimulus=single_stimulus(), claim_level="state_effect"
    )

    with pytest.raises(CapabilityGap) as raised:
        compiled(package, tmp_path / "out")

    assert raised.value.record["reason"] == "; ".join(
        [GAPS["direct-single"][1], GAPS["state-effect"][1]]
    )
    assert raised.value.record["delivery"] == "single"


def test_a_planted_package_without_a_seed_cannot_be_compiled(tmp_path: Path) -> None:
    from asago_bundle_core.testing import write_test_package

    package = write_test_package(tmp_path / "packages", stimulus=planted_stimulus())

    with pytest.raises(CompileError, match="planted_item package has no seed.json"):
        compiled(package, tmp_path / "out")


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (seed_item(slot="klarna.nowhere"), "unknown slot"),
        (seed_item(record="ORD-999"), "is not a record of slot"),
        (seed_item(mode="prepend"), "unknown operation"),
    ],
)
def test_a_seed_the_base_cannot_hold_cannot_be_compiled(
    tmp_path: Path, item: dict[str, str], message: str
) -> None:
    package = write_planted_package(tmp_path / "packages", seed=seed_document(item))

    with pytest.raises(CompileError, match=message):
        compiled(package, tmp_path / "out")


def test_a_turn_count_that_disagrees_cannot_be_compiled(tmp_path: Path) -> None:
    stimulus = {**planted_stimulus(SEQUENTIAL_TURNS), "turn_count": 3}
    package = write_planted_package(tmp_path / "packages", stimulus=stimulus)

    with pytest.raises(CompileError, match="turn_count 3 does not match 2 user turns"):
        compiled(package, tmp_path / "out")


def test_an_unknown_judge_turns_cannot_be_compiled(tmp_path: Path) -> None:
    package = write_case_package(tmp_path / "packages", "reply-single")

    with pytest.raises(CompileError, match="judge_turns"):
        compiled(package, tmp_path / "out", judge_turns="some")


def test_compile_refuses_a_directory_that_is_not_empty(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "left.json").write_text("{}", encoding="utf-8")

    with pytest.raises(CompileError, match="not empty"):
        compiled(write_case_package(tmp_path / "packages", "command-single"), out)
