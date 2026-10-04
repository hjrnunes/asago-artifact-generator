"""Offline tests for the author replay gate.

The end-to-end cases record one ``author`` item through the real CLI with a
scripted chat client standing in for the provider, lay the output out like an
orch author stage, and replay it through the gate in a guarded subprocess.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from asago_artifact_generator import cli, replay_gate
from asago_artifact_generator.authoring.transport import PrivateModelAuthoringTransport
from asago_artifact_generator.replay_gate import (
    ALLOWED_DIFFERENCES,
    Difference,
    GateResult,
    ItemResult,
    RecordedCall,
    ReplayChatClient,
    ReplayRecordError,
    _summary,
    compare_item_outputs,
    load_recorded_calls,
    replay_environment,
    run_gate,
)

from .test_profile_bridge import HANDOFF
from .test_profile_bridge import _inputs as _target_inputs

TASK_ID = "SCN-901"


class _ScriptedChatClient:
    """Stands in for ``openai.OpenAI`` while a test records an author item."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.chat = SimpleNamespace(completions=self)

    def create(self, **request: object) -> SimpleNamespace:
        content = self.contents.pop(0)
        return SimpleNamespace(
            model="profile-model",
            usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=content, reasoning=None),
                )
            ],
        )


def _record_author_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: list[str]
) -> Path:
    """Record one author item as an orch run would and return the stage directory."""

    run = tmp_path / "runs" / "run-test"
    scenarios = run / "stages" / "generate" / "output" / "scenarios"
    scenarios.mkdir(parents=True)
    scenario = scenarios / f"{TASK_ID}.json"
    shutil.copyfile(HANDOFF, scenario)
    discover = run / "stages" / "discover" / "output"
    discover.mkdir(parents=True)
    target_profile, runtime_contract = _target_inputs(discover)
    profiles_file = tmp_path / "profiles.yaml"
    profiles_file.write_text(
        yaml.safe_dump(
            {
                "gemma4-oc": {
                    "base_url": "https://profile.example.invalid/v1",
                    "api_key": "profile-secret-value",
                    "model": "profile-model",
                }
            }
        ),
        encoding="utf-8",
    )
    stage = run / "stages" / "author"
    output = stage / "output"
    argv = [
        "/venv/bin/asago-artifact-generator",
        "generate",
        str(scenario),
        "--output-dir",
        str(output),
        "--task-id",
        TASK_ID,
        "--target-profile",
        str(target_profile),
        "--runtime-contract",
        str(runtime_contract),
        "--profile",
        "gemma4-oc",
        "--profiles-file",
        str(profiles_file),
    ]
    client = _ScriptedChatClient(contents)

    class _RecordingTransport(PrivateModelAuthoringTransport):
        def __init__(self, **options: object) -> None:
            super().__init__(**options)
            self._client = client

    monkeypatch.setattr(cli, "PrivateModelAuthoringTransport", _RecordingTransport)
    result = CliRunner().invoke(cli.app, argv[1:])
    assert not client.contents, "every scripted response is consumed"
    items = stage / "items"
    items.mkdir()
    (items / f"{TASK_ID}.log").write_text(result.output, encoding="utf-8")
    (stage / "stage.json").write_text(
        json.dumps(
            {
                "argv": argv,
                "items": [
                    {"id": "SCN-900", "skipped": True, "status": "skipped"},
                    {
                        "argv": argv,
                        "exit_code": result.exit_code,
                        "id": TASK_ID,
                        "log_path": f"runs/run-test/stages/author/items/{TASK_ID}.log",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return stage


@pytest.fixture
def unresolved_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Two unparseable plans exhaust the default plan correction allowance
    # before any detector control or review runs, so no Docker is needed.
    return _record_author_stage(
        tmp_path, monkeypatch, ["not a json plan", "still not a json plan"]
    )


def _evidence_path(stage: Path) -> Path:
    return stage / "output" / f"{TASK_ID}.failure-evidence.json"


def _rewrite_json(path: Path, change) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


# --- recorded calls -----------------------------------------------------------


def test_recorded_calls_follow_dispatch_order_with_exact_bytes(unresolved_stage: Path) -> None:
    calls = load_recorded_calls(_evidence_path(unresolved_stage))

    assert [call.stage for call in calls] == ["call1", "correction"]
    assert [call.raw for call in calls] == [b"not a json plan", b"still not a json plan"]
    assert calls[0].requested_model == "profile-model"
    assert calls[0].usage == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert calls[0].system and calls[0].user


def test_recorded_call_that_cannot_be_rebuilt_is_rejected(unresolved_stage: Path) -> None:
    def make_non_text(document: dict) -> None:
        document["attempts"][0]["response_capture"]["final_answer"] = {
            "state": "non_text",
            "value_type": "list",
        }

    _rewrite_json(_evidence_path(unresolved_stage), make_non_text)

    with pytest.raises(ReplayRecordError, match="final_answer"):
        load_recorded_calls(_evidence_path(unresolved_stage))


def _call(**changes: object) -> RecordedCall:
    values: dict[str, object] = {
        "dispatch_index": 1,
        "stage": "call1",
        "prompt_version": "authoring-call1-test",
        "system": "system text",
        "user": "user text",
        "requested_model": "profile-model",
        "returned_model": "returned-model",
        "raw": b"answer",
        "final_answer": {"state": "text", "content": "answer"},
        "reasoning": {"state": "null", "source_field": "reasoning"},
        "finish_reason": {"state": "value", "value": "stop"},
        "usage": {"total_tokens": 3},
    }
    values.update(changes)
    return RecordedCall(**values)  # type: ignore[arg-type]


def _request(system: str = "system text", user: str = "user text") -> dict:
    return {
        "model": "profile-model",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }


def test_replay_client_rebuilds_the_recorded_provider_fields() -> None:
    client = ReplayChatClient([_call()])

    response = client.chat.completions.create(**_request())

    choice = response.choices[0]
    assert response.model == "returned-model"
    assert response.usage == {"total_tokens": 3}
    assert choice.finish_reason == "stop"
    assert choice.message == {"content": "answer", "reasoning": None}
    assert client.served == 1
    assert client.mismatches == []


def test_replay_client_reports_a_prompt_mismatch_and_refuses_to_answer() -> None:
    client = ReplayChatClient([_call()])

    with pytest.raises(ConnectionError, match="prompt mismatch"):
        client.chat.completions.create(**_request(user="changed user text"))

    assert client.mismatches == [
        {
            "dispatch_index": 1,
            "stage": "call1",
            "prompt_version": "authoring-call1-test",
            "detail": "user message differs from the recording",
        }
    ]


def test_replay_client_reports_a_request_beyond_the_recording() -> None:
    client = ReplayChatClient([])

    with pytest.raises(ConnectionError, match="no recorded response"):
        client.chat.completions.create(**_request())

    assert client.mismatches[0]["detail"] == "request beyond the recorded dispatches"


# --- environment and network ------------------------------------------------------


def test_replay_environment_strips_provider_settings() -> None:
    env = replay_environment(
        {
            "PATH": "/bin",
            "OPENAI_BASE_URL": "x",
            "OPENROUTER_API_KEY": "x",
            "REDTEAM_MODEL": "x",
            "GEMINI_API_KEY": "x",
            "GOOGLE_API_KEY": "x",
            "ASAGO_ORCH_CONSUMER": "x",
            "FORCE_COLOR": "1",
        }
    )

    assert env == {"PATH": "/bin"}


def test_network_guard_refuses_and_logs_outbound_connections(tmp_path: Path) -> None:
    log = tmp_path / "network.log"
    script = (
        "import socket, sys\n"
        "from asago_artifact_generator.replay_gate import install_network_guard\n"
        f"install_network_guard(__import__('pathlib').Path({str(log)!r}))\n"
        "for attempt in (lambda: socket.create_connection(('127.0.0.1', 9), timeout=1),\n"
        "                lambda: socket.getaddrinfo('example.invalid', 443)):\n"
        "    try:\n"
        "        attempt()\n"
        "    except ConnectionRefusedError:\n"
        "        pass\n"
        "    else:\n"
        "        sys.exit('connection was not refused')\n"
    )

    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "resolve 127.0.0.1",
        "resolve example.invalid",
    ]


# --- comparison -------------------------------------------------------------------


def test_allowance_list_is_explicit_and_limited_to_the_output_location() -> None:
    assert [(item.file, item.fields) for item in ALLOWED_DIFFERENCES] == [
        ("*.failure-evidence.json", ("package_path",)),
        ("*.blocked.json", ("package_path",)),
        ("items/*.log", ("package",)),
    ]


def _write_tree(root: Path, package_path: str, status: str = "unresolved") -> None:
    (root / "output").mkdir(parents=True)
    (root / "items").mkdir()
    (root / "output" / f"{TASK_ID}.failure-evidence.json").write_text(
        json.dumps({"package_path": package_path, "status": status}), encoding="utf-8"
    )
    (root / "items" / f"{TASK_ID}.log").write_text(
        json.dumps({"status": status, "package": None}) + "\nfinding: detail\n",
        encoding="utf-8",
    )


def test_package_path_must_match_after_mapping_the_output_directory(tmp_path: Path) -> None:
    recorded, replayed = tmp_path / "recorded", tmp_path / "replayed"
    _write_tree(recorded, f"{recorded / 'output'}/{TASK_ID}")
    _write_tree(replayed, f"{replayed / 'output'}/{TASK_ID}")

    compared, differences = compare_item_outputs(
        TASK_ID,
        recorded_output=recorded / "output",
        recorded_log=recorded / "items" / f"{TASK_ID}.log",
        replayed_output=replayed / "output",
        replayed_log=replayed / "items" / f"{TASK_ID}.log",
    )

    assert compared == 2
    assert differences == []


def test_package_path_elsewhere_and_other_fields_still_differ(tmp_path: Path) -> None:
    recorded, replayed = tmp_path / "recorded", tmp_path / "replayed"
    _write_tree(recorded, f"{recorded / 'output'}/{TASK_ID}")
    _write_tree(replayed, f"/elsewhere/{TASK_ID}", status="accepted")

    _, differences = compare_item_outputs(
        TASK_ID,
        recorded_output=recorded / "output",
        recorded_log=recorded / "items" / f"{TASK_ID}.log",
        replayed_output=replayed / "output",
        replayed_log=replayed / "items" / f"{TASK_ID}.log",
    )

    details = {item.path: item.detail for item in differences}
    assert "$.package_path" in details[f"{TASK_ID}.failure-evidence.json"]
    assert "$.status" in details[f"items/{TASK_ID}.log"]


def test_missing_and_extra_files_are_differences(tmp_path: Path) -> None:
    recorded, replayed = tmp_path / "recorded", tmp_path / "replayed"
    _write_tree(recorded, f"{recorded / 'output'}/{TASK_ID}")
    _write_tree(replayed, f"{replayed / 'output'}/{TASK_ID}")
    (recorded / "output" / TASK_ID).mkdir()
    (recorded / "output" / TASK_ID / "plan.json").write_text("{}", encoding="utf-8")
    (replayed / "output" / f"{TASK_ID}.blocked.json").write_text("{}", encoding="utf-8")
    (replayed / "output" / "SCN-9010.failure-evidence.json").write_text("{}", encoding="utf-8")

    _, differences = compare_item_outputs(
        TASK_ID,
        recorded_output=recorded / "output",
        recorded_log=recorded / "items" / f"{TASK_ID}.log",
        replayed_output=replayed / "output",
        replayed_log=replayed / "items" / f"{TASK_ID}.log",
    )

    assert [(item.path, item.detail) for item in differences] == [
        (f"{TASK_ID}/plan.json", "only in recording"),
        (f"{TASK_ID}.blocked.json", "only in replay"),
    ]


# --- end to end -------------------------------------------------------------------


def test_gate_replays_a_recorded_stage_offline_and_passes(
    unresolved_stage: Path, tmp_path: Path
) -> None:
    before = {path: path.read_bytes() for path in unresolved_stage.rglob("*") if path.is_file()}

    result = run_gate(unresolved_stage, work=tmp_path / "work")

    assert result.passed, result.report()
    [item] = result.items
    assert item.item_id == TASK_ID
    assert item.exit_code == item.recorded_exit_code == 1
    assert item.status == item.recorded_status == "unresolved"
    assert item.served == 2 and item.unused == 0
    assert item.mismatches == [] and item.network_attempts == []
    assert item.files_compared == 2
    assert result.skipped == 1
    after = {path: path.read_bytes() for path in unresolved_stage.rglob("*") if path.is_file()}
    assert after == before, "the recorded stage is never written"


def test_gate_fails_on_a_prompt_the_code_no_longer_renders(
    unresolved_stage: Path, tmp_path: Path
) -> None:
    def change_prompt(document: dict) -> None:
        document["attempts"][0]["prompt"]["user"] += "\nrecorded under older code"

    _rewrite_json(_evidence_path(unresolved_stage), change_prompt)

    result = run_gate(unresolved_stage, work=tmp_path / "work")

    assert not result.passed
    [item] = result.items
    assert item.mismatches[0]["stage"] == "call1"
    assert item.mismatches[0]["detail"] == "user message differs from the recording"


def test_gate_fails_on_an_output_or_exit_code_difference(
    unresolved_stage: Path, tmp_path: Path
) -> None:
    _rewrite_json(
        _evidence_path(unresolved_stage), lambda document: document.update(status="accepted")
    )

    def change_exit(document: dict) -> None:
        document["items"][1]["exit_code"] = 0

    _rewrite_json(unresolved_stage / "stage.json", change_exit)

    result = run_gate(unresolved_stage, work=tmp_path / "work")

    assert not result.passed
    [item] = result.items
    assert item.exit_code == 1 and item.recorded_exit_code == 0
    assert [difference.path for difference in item.differences] == [
        f"{TASK_ID}.failure-evidence.json"
    ]
    assert "$.status" in item.differences[0].detail


# --- report and command line ------------------------------------------------------


def _item(item_id: str, recorded_status: str, **changes: object) -> ItemResult:
    values: dict[str, object] = {
        "item_id": item_id,
        "recorded_exit_code": 1,
        "recorded_status": recorded_status,
        "exit_code": 1,
        "status": recorded_status,
        "served": 3,
        "files_compared": 4,
    }
    values.update(changes)
    return ItemResult(**values)  # type: ignore[arg-type]


def _failing_item() -> ItemResult:
    return _item(
        "SCN-2",
        "unresolved",
        exit_code=0,
        status="accepted",
        served=2,
        unused=1,
        error="the replayed item wrote no replay status",
        mismatches=[{"dispatch_index": 2, "detail": "user message"}],
        network_attempts=["resolve a.invalid", "resolve b.invalid"],
        differences=[
            Difference("SCN-2.failure-evidence.json", "$.status: a != b"),
            Difference("items/SCN-2.log", "only in recording"),
        ],
        log_tail=["x" * 301],
    )


def test_stage_report_lists_totals_and_each_failure_in_order() -> None:
    result = GateResult(
        stage_dir=Path("/runs/r/stages/author"),
        work=Path("/scratch/00-r"),
        items=[_item("SCN-1", "accepted"), _failing_item(), _item("SCN-3", "unresolved")],
        skipped=2,
        seconds=12.345,
    )

    assert result.report(limit=1).splitlines() == [
        "stage:      /runs/r/stages/author",
        "scratch:    /scratch/00-r",
        "items:      3 replayed, 2 skipped by the recording",
        "statuses:   accepted 1, unresolved 2",
        "dispatches: 8 served",
        "files:      12 compared",
        "time:       12.3s",
        "failed:     1",
        "  SCN-2: exit 0 (recorded 1), status accepted (recorded unresolved), unused records 1",
        "    error: the replayed item wrote no replay status",
        "    prompt mismatch: {'dispatch_index': 2, 'detail': 'user message'}",
        "    network: resolve a.invalid",
        "    SCN-2.failure-evidence.json: $.status: a != b",
        "    log: " + "x" * 300,
        "FAIL",
    ]


def test_stage_report_passes_only_when_every_item_passes() -> None:
    passing = GateResult(stage_dir=Path("/s"), work=Path("/w"), items=[_item("SCN-1", "accepted")])
    empty = GateResult(stage_dir=Path("/s"), work=Path("/w"))

    assert passing.report().splitlines()[-2:] == ["failed:     0", "PASS"]
    assert empty.report().splitlines()[-1] == "FAIL"


def test_summary_totals_every_stage() -> None:
    first = GateResult(stage_dir=Path("/a"), work=Path("/w"), items=[_item("SCN-1", "accepted")])
    second = GateResult(
        stage_dir=Path("/b"), work=Path("/w"), items=[_failing_item(), _item("SCN-3", "blocked")]
    )

    assert _summary([first, second], 61.04) == "\n".join(
        [
            "== summary",
            "stages:          2",
            "items:           3 replayed, 2 identical",
            "statuses:        accepted 2, blocked 1",
            "dispatches:      8 served",
            "prompt mismatch: 1",
            "wall time:       61.0s",
            "FAIL",
        ]
    )
    assert _summary([first], 0.0).splitlines()[-1] == "PASS"
    assert _summary([], 0.0).splitlines()[-1] == "FAIL"


def test_main_passes_a_recorded_stage_and_removes_its_temporary_scratch(
    unresolved_stage: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(replay_gate.tempfile, "mkdtemp", lambda prefix: str(scratch))
    report = tmp_path / "items.json"

    code = replay_gate.main(["check", str(unresolved_stage), "--json", str(report)])

    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"== {unresolved_stage}"
    assert "items:           1 replayed, 1 identical" in lines
    assert lines[-3] == "prompt mismatch: 0"
    assert lines[-1] == "PASS"
    [stage] = json.loads(report.read_text(encoding="utf-8"))
    assert stage["stage_dir"] == str(unresolved_stage.resolve())
    assert [item["item_id"] for item in stage["items"]] == [TASK_ID]
    assert stage["items"][0]["served"] == 2
    assert not scratch.exists()


def test_main_keeps_the_scratch_directory_when_asked(
    unresolved_stage: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(replay_gate.tempfile, "mkdtemp", lambda prefix: str(scratch))

    assert replay_gate.main(["check", str(unresolved_stage), "--keep"]) == 0

    assert (scratch / "00-run-test" / "items" / f"{TASK_ID}.log").is_file()


def test_main_fails_on_a_difference_and_keeps_the_work_dir(
    unresolved_stage: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _rewrite_json(
        _evidence_path(unresolved_stage), lambda document: document.update(status="accepted")
    )
    work = tmp_path / "work"

    code = replay_gate.main(
        ["check", str(unresolved_stage), "--work-dir", str(work), "--item", TASK_ID]
    )

    assert code == 1
    output = capsys.readouterr().out
    assert f"    {TASK_ID}.failure-evidence.json: $.status: " in output
    assert output.splitlines()[-1] == "FAIL"
    assert (work / "00-run-test" / "items" / f"{TASK_ID}.log").is_file()


def test_main_rejects_missing_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        replay_gate.main(["check"])

    assert raised.value.code == 2
    assert "stage_dirs" in capsys.readouterr().err


def test_main_reads_sys_argv_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["replay_gate", "unknown-command"])

    with pytest.raises(SystemExit) as raised:
        replay_gate.main()

    assert raised.value.code == 2


def test_main_runs_one_replay_item_with_the_generate_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(replay_gate, "_replay_item", lambda *args: calls.append(args))

    code = replay_gate.main(["_replay-item", "r.json", "s.json", "n.log", "--", "generate", "x"])

    assert code == 0
    assert calls == [(Path("r.json"), Path("s.json"), Path("n.log"), ["generate", "x"])]


def test_main_rejects_a_replay_item_without_the_separator() -> None:
    with pytest.raises(SystemExit, match="usage: _replay-item"):
        replay_gate.main(["_replay-item", "r.json", "s.json", "n.log", "generate"])


def _attempt(content: str | None = '{"ok":true}', **changes: object) -> dict:
    import base64
    import hashlib

    raw = content.encode("utf-8") if content is not None else b""
    attempt = {
        "dispatch_index": 3,
        "stage": "author",
        "raw_response": {
            "availability": "available",
            "base64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "response_capture": {
            "final_answer": (
                {"state": "text", "content": content} if content else {"state": "absent"}
            ),
            "reasoning": {"state": "absent"},
            "finish_reason": {"state": "value", "value": "stop"},
        },
        "prompt": {"version": "v1", "system": "system text", "user": "user text"},
        "model_identity": {
            "requested_model": "model-a",
            "returned_model": {"availability": "available", "value": "model-a-1"},
        },
        "usage": {"availability": "unavailable"},
    }
    attempt.update(changes)
    return attempt


def _capture(**states: dict) -> dict:
    capture = {
        "final_answer": {"state": "text", "content": '{"ok":true}'},
        "reasoning": {"state": "absent"},
        "finish_reason": {"state": "value", "value": "stop"},
    }
    capture.update(states)
    return capture


def test_recorded_call_rebuilds_the_attempt_fields() -> None:
    call = replay_gate._recorded_call(_attempt())

    assert call.dispatch_index == 3
    assert call.raw == b'{"ok":true}'
    assert call.prompt_version == "v1"
    assert call.returned_model == "model-a-1"
    assert call.usage is None
    assert replay_gate._recorded_call(_attempt(None)).raw == b""


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"raw_response": None}, "dispatch 3 \\(author\\): raw_response is not available"),
        ({"raw_response": {"availability": "unavailable"}}, "raw_response is not available"),
        (
            {"raw_response": {"availability": "available", "base64": "", "sha256": "0"}},
            "raw_response bytes do not match their sha256",
        ),
        ({"response_capture": None}, "final_answer state cannot be rebuilt: {}"),
        (
            {"response_capture": _capture(reasoning={"state": "redacted"})},
            "reasoning state cannot be rebuilt",
        ),
        (
            {"response_capture": _capture(finish_reason={"state": "text"})},
            "finish_reason cannot be rebuilt",
        ),
        (
            {"response_capture": _capture(final_answer={"state": "null"})},
            "raw_response differs from final_answer content",
        ),
    ],
)
def test_recorded_call_rejects_an_attempt_it_cannot_rebuild(changes: dict, message: str) -> None:
    with pytest.raises(ReplayRecordError, match=message):
        replay_gate._recorded_call(_attempt(**changes))


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ({"a": 1}, {"a": 1}, None),
        ([1, [2]], [1, [2]], None),
        (1, "1", '$: 1 != "1"'),
        ({"a": 1}, {"a": 2}, "$.a: 1 != 2"),
        ({"a": 1}, {"a": 1, "b": 2}, "$.b: only in replay"),
        ({"a": 1, "b": 2}, {"a": 1}, "$.b: only in recording"),
        ({"a": 1, "b": 2}, {"b": 2, "a": 1}, "$: key order ['a', 'b'] != ['b', 'a']"),
        ({"a": [1, {"b": "x"}]}, {"a": [1, {"b": "y"}]}, '$.a[1].b: "x" != "y"'),
        ([1, 2], [1], "$: length 2 != 1"),
        ("x" * 200, "y", '$: "' + "x" * 159 + '... != "y"'),
    ],
)
def test_first_difference_names_the_first_differing_location(
    left: object, right: object, expected: str | None
) -> None:
    # Callers pass the recording as the left side and the replay as the right.
    assert replay_gate._first_difference(left, right) == expected
