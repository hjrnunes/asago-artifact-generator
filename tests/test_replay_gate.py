"""Offline tests for the author replay gate.

The end-to-end cases record one ``author`` item through the real CLI with a
scripted chat client standing in for the provider, lay the output out like an
orchestrator author stage, and replay it through the gate in a guarded subprocess.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
import yaml
from typer.testing import CliRunner

import replay_gate
from asago_artifact_generator import cli
from asago_artifact_generator.authoring.transport import PrivateModelAuthoringTransport
from asago_artifact_generator.contract_kit import canonical_json
from replay_gate import (
    ALLOWED_DIFFERENCES,
    Difference,
    GateResult,
    ItemResult,
    RecordedCall,
    ReplayChatClient,
    ReplayRecordError,
    _summary,
    compare_item_outputs,
    current_layout,
    load_recorded_calls,
    replay_environment,
    run_gate,
)

from .support import HANDOFF, build_neutral_artifact_package, target_inputs

TASK_ID = "SCN-901"


class _ScriptedChatClient:
    """Stands in for ``openai.OpenAI`` while a test records an author item."""

    def __init__(self, contents: list[str | BaseException]) -> None:
        self.contents = list(contents)
        self.chat = SimpleNamespace(completions=self)

    def create(self, **request: object) -> SimpleNamespace:
        content = self.contents.pop(0)
        if isinstance(content, BaseException):
            raise content
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: list[str | BaseException],
    **profile_options: str,
) -> Path:
    """Record one author item as an orchestrator run would and return the stage directory.

    *profile_options* add fields to the recorded model profile, for example a
    service tier and its fallback.
    """

    run = tmp_path / "runs" / "run-test"
    scenarios = run / "stages" / "generate" / "output" / "scenarios"
    scenarios.mkdir(parents=True)
    scenario = scenarios / f"{TASK_ID}.json"
    shutil.copyfile(HANDOFF, scenario)
    discover = run / "stages" / "discover" / "output"
    discover.mkdir(parents=True)
    target_profile, runtime_contract = target_inputs(discover)
    profiles_file = tmp_path / "profiles.yaml"
    profiles_file.write_text(
        yaml.safe_dump(
            {
                "profile-a": {
                    "base_url": "https://profile.example.invalid/v1",
                    "api_key": "profile-secret-value",
                    "model": "profile-model",
                    **profile_options,
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
        "profile-a",
        "--profiles-file",
        str(profiles_file),
    ]
    client = _ScriptedChatClient(contents)

    def transport(**options: object) -> PrivateModelAuthoringTransport:
        return PrivateModelAuthoringTransport(**options, client=client)

    result = CliRunner().invoke(cli.app, argv[1:], obj=transport)
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
    # before any review runs.
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


_CONNECTION_RETRY = {"type": "APIConnectionError", "status_code": None}
_STATUS_RETRY = {"type": "InternalServerError", "status_code": 502}
_FALLBACK_RETRY = {"type": "RateLimitError", "status_code": 429}
_FALLBACK = {"attempt": 2, "retry_of": _FALLBACK_RETRY, "reason": "service_tier_fallback"}


def test_recorded_call_without_a_retry_has_no_recorded_failures() -> None:
    assert replay_gate._recorded_call(_attempt()).retries == ()


def test_recorded_call_reads_the_retries_recorded_on_its_dispatch() -> None:
    retries = [{"attempt": 2, "retry_of": _STATUS_RETRY}]

    call = replay_gate._recorded_call(_attempt(transport_retries=retries))

    assert call.retries == (_STATUS_RETRY,)


def test_recorded_call_rejects_a_retry_it_cannot_rebuild() -> None:
    retries = [{"attempt": 2, "retry_of": {"type": "SomethingElse", "status_code": None}}]

    with pytest.raises(ReplayRecordError, match="dispatch 3 \\(author\\): retry_of"):
        replay_gate._recorded_call(_attempt(transport_retries=retries))


def test_recorded_call_reads_a_service_tier_fallback_as_a_recorded_rate_limit() -> None:
    retries = [_FALLBACK, {"attempt": 3, "retry_of": _STATUS_RETRY}]

    call = replay_gate._recorded_call(_attempt(transport_retries=retries))

    assert call.retries == (_FALLBACK_RETRY, _STATUS_RETRY)


@pytest.mark.parametrize(
    "retry",
    [
        {"attempt": 2, "retry_of": _FALLBACK_RETRY},
        {"attempt": 2, "retry_of": _STATUS_RETRY, "reason": "service_tier_fallback"},
        {"attempt": 2, "retry_of": {**_FALLBACK_RETRY, "status_code": 503}, "reason": "x"},
        {"attempt": 2, "retry_of": _CONNECTION_RETRY, "reason": "another_reason"},
    ],
    ids=["429-without-reason", "fallback-of-a-502", "unknown-reason", "connection-with-reason"],
)
def test_recorded_call_rejects_a_rate_limit_or_reason_the_transport_never_records(
    retry: dict,
) -> None:
    with pytest.raises(ReplayRecordError, match="retry_of cannot be rebuilt"):
        replay_gate._recorded_call(_attempt(transport_retries=[retry]))


def test_the_gate_names_the_fallback_reason_the_transport_records() -> None:
    from asago_artifact_generator.authoring.transport import SERVICE_TIER_FALLBACK_REASON

    assert replay_gate.SERVICE_TIER_FALLBACK_REASON == SERVICE_TIER_FALLBACK_REASON


@pytest.mark.parametrize(
    ("recorded", "expected"),
    [
        (_CONNECTION_RETRY, openai.APIConnectionError),
        (_STATUS_RETRY, openai.InternalServerError),
        (_FALLBACK_RETRY, openai.RateLimitError),
    ],
    ids=["connection", "502", "fallback-429"],
)
def test_replay_client_raises_the_recorded_failure_then_serves_the_response(
    recorded: dict, expected: type[Exception]
) -> None:
    client = ReplayChatClient([_call(retries=(recorded,))])

    with pytest.raises(expected) as raised:
        client.chat.completions.create(**_request())
    response = client.chat.completions.create(**_request())

    assert raised.value.__class__ is expected
    assert getattr(raised.value, "status_code", None) == recorded["status_code"]
    assert response.choices[0].message["content"] == "answer"
    assert client.served == 1
    assert client.unused == 0
    assert client.mismatches == []


def _timed_out_attempt(**failure: object) -> dict:
    return _attempt(
        raw_response={"availability": "unavailable", "reason": "provider_failure"},
        response_capture=None,
        model_identity={
            "requested_model": "model-a",
            "returned_model": {"availability": "unavailable", "reason": "not_returned"},
        },
        failure={
            "code": "transport_failure",
            "detail": "Request timed out.",
            "elapsed_ms": 26910.979,
            "phase": "invocation",
            **failure,
        },
    )


def test_recorded_call_reads_a_recorded_timeout_as_a_failed_dispatch() -> None:
    call = replay_gate._recorded_call(_timed_out_attempt())

    assert call.timed_out
    assert (call.dispatch_index, call.stage, call.prompt_version) == (3, "author", "v1")
    assert call.system == "system text" and call.user == "user text"
    assert call.requested_model == "model-a" and call.returned_model is None
    assert not replay_gate._recorded_call(_attempt()).timed_out


@pytest.mark.parametrize(
    "failure",
    [
        {"detail": "Connection error."},
        {"detail": "Error code: 429"},
        {"phase": "post_response"},
    ],
    ids=["connection", "429", "not-an-invocation"],
)
def test_recorded_call_still_rejects_an_unavailable_response_that_is_not_a_timeout(
    failure: dict,
) -> None:
    with pytest.raises(ReplayRecordError, match="raw_response is not available"):
        replay_gate._recorded_call(_timed_out_attempt(**failure))


def test_recorded_call_rejects_an_unavailable_response_with_another_reason() -> None:
    attempt = _timed_out_attempt()
    attempt["raw_response"] = {"availability": "unavailable", "reason": "not_returned"}

    with pytest.raises(ReplayRecordError, match="raw_response is not available"):
        replay_gate._recorded_call(attempt)


def test_replay_client_raises_the_recorded_timeout_and_the_transport_never_retries_it() -> None:
    client = ReplayChatClient([_call(timed_out=True)])

    with pytest.raises(openai.APITimeoutError, match="Request timed out."):
        client.chat.completions.create(**_request())

    assert client.served == 1
    assert client.unused == 0
    assert client.mismatches == []


def test_replay_client_checks_the_prompt_of_a_timed_out_request_too() -> None:
    client = ReplayChatClient([_call(timed_out=True)])

    with pytest.raises(ConnectionError, match="prompt mismatch"):
        client.chat.completions.create(**_request(user="changed user text"))

    assert client.served == 0


def test_replay_client_raises_the_recorded_retries_before_the_recorded_timeout() -> None:
    client = ReplayChatClient([_call(timed_out=True, retries=(_CONNECTION_RETRY,))])

    with pytest.raises(openai.APIConnectionError):
        client.chat.completions.create(**_request())
    with pytest.raises(openai.APITimeoutError):
        client.chat.completions.create(**_request())

    assert client.served == 1


def test_replay_client_checks_the_prompt_of_the_retried_request_too() -> None:
    client = ReplayChatClient([_call(retries=(_CONNECTION_RETRY,))])

    with pytest.raises(openai.APIConnectionError):
        client.chat.completions.create(**_request())
    with pytest.raises(ConnectionError, match="prompt mismatch"):
        client.chat.completions.create(**_request(user="changed user text"))

    assert client.mismatches[0]["detail"] == "user message differs from the recording"


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
            "ASAGO_SETTING": "x",
            "FORCE_COLOR": "1",
        }
    )

    assert env == {"PATH": "/bin"}


def test_network_guard_refuses_and_logs_outbound_connections(tmp_path: Path) -> None:
    log = tmp_path / "network.log"
    script = (
        "import socket, sys\n"
        f"sys.path.insert(0, {str(Path(replay_gate.__file__).parent)!r})\n"
        "from replay_gate import install_network_guard\n"
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


def _compare_evidence(tmp_path: Path, recorded: dict, replayed: dict) -> str | None:
    name = f"{TASK_ID}.failure-evidence.json"
    left, right = tmp_path / "recorded.json", tmp_path / "replayed.json"
    left.write_text(json.dumps(recorded), encoding="utf-8")
    right.write_text(json.dumps(replayed), encoding="utf-8")
    return replay_gate.compare_file(left, right, name, {})


def _failed_attempt(elapsed_ms: object = 26910.979, **failure: object) -> dict:
    return {
        "attempts": [
            {
                "dispatch_index": 1,
                "failure": {"code": "transport_failure", "elapsed_ms": elapsed_ms, **failure},
            }
        ],
        "status": "transport_failure",
    }


def test_the_wall_clock_time_of_a_failed_dispatch_may_differ(tmp_path: Path) -> None:
    assert _compare_evidence(tmp_path, _failed_attempt(26910.979), _failed_attempt(0.4)) is None


@pytest.mark.parametrize(
    ("replayed", "path"),
    [
        (_failed_attempt(detail="Connection error."), "$.attempts[0].failure.detail"),
        (_failed_attempt(code="other"), "$.attempts[0].failure.code"),
        (_failed_attempt(None), "$.attempts[0].failure.elapsed_ms"),
        (_failed_attempt("slow"), "$.attempts[0].failure.elapsed_ms"),
    ],
    ids=["extra-field", "other-code", "no-time", "non-numeric-time"],
)
def test_only_the_wall_clock_time_of_a_failed_dispatch_is_exempt(
    tmp_path: Path, replayed: dict, path: str
) -> None:
    found = _compare_evidence(tmp_path, _failed_attempt(), replayed)

    assert found is not None and found.startswith(path + ":")


def test_a_failed_dispatch_time_present_on_one_side_only_is_a_difference(tmp_path: Path) -> None:
    replayed = _failed_attempt()
    del replayed["attempts"][0]["failure"]["elapsed_ms"]

    found = _compare_evidence(tmp_path, _failed_attempt(), replayed)

    assert found == "$.attempts[0].failure.elapsed_ms: only in recording"


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


def _age_package(output: Path, item_id: str) -> None:
    """Rewrite a current package in the layout a recording made before the copies were dropped."""

    from asago_artifact_generator.package_io import _manifest_digest, _member_record

    package = output / item_id
    copies = {
        "authoring/01-call1.json": b"{}\n",
        "authoring/01-call1.prompt": b"the prompt",
        "authoring/01-call1.raw": b"the reply",
        "authoring/ledger.json": b"[]\n",
        "authoring/reviews.json": b"{}\n",
        "authoring/transformations.json": b"[]\n",
        "source-hashes.json": b"{}\n",
        "observations.json": b"{}\n",
    }
    inputs = json.loads((package / "inputs.json").read_bytes())
    inputs.update(model_facing_input={"narrative": "n"}, source_input={"payload": {}})
    copies["inputs.json"] = (canonical_json(inputs) + "\n").encode("utf-8")
    for name, content in copies.items():
        (package / name).parent.mkdir(exist_ok=True)
        (package / name).write_bytes(content)
    manifest = json.loads((package / "manifest.json").read_bytes())
    members = {record["path"]: record for record in manifest["members"]}
    members.update({name: _member_record(name, content) for name, content in copies.items()})
    manifest["members"] = [members[path] for path in sorted(members)]
    manifest["authoring"].update(
        ledger=[{"stage": "call1"}],
        usage=[],
        policy={},
        review_status={"plan": "accepted"},
    )
    manifest["creation_model"] = {"model": "configured-private-authoring"}
    manifest["manifest_digest"] = _manifest_digest(manifest)
    (package / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    (output / f"{item_id}.authoring").mkdir()
    (output / f"{item_id}.authoring" / "01-call1.prompt").write_bytes(b"the prompt")


def _package_trees(tmp_path: Path) -> tuple[Path, Path]:
    recorded, replayed = tmp_path / "recorded", tmp_path / "replayed"
    for root in (recorded, replayed):
        _write_tree(root, f"{root / 'output'}/{TASK_ID}", status="accepted")
        build_neutral_artifact_package(root / "output" / TASK_ID)
    _age_package(recorded / "output", TASK_ID)
    return recorded, replayed


def _differences(recorded: Path, replayed: Path) -> tuple[int, list[Difference]]:
    return compare_item_outputs(
        TASK_ID,
        recorded_output=recorded / "output",
        recorded_log=recorded / "items" / f"{TASK_ID}.log",
        replayed_output=replayed / "output",
        replayed_log=replayed / "items" / f"{TASK_ID}.log",
    )


def test_a_recording_with_the_retired_request_copies_compares_equal(tmp_path: Path) -> None:
    recorded, replayed = _package_trees(tmp_path)

    compared, differences = _differences(recorded, replayed)

    assert differences == []
    # The 11 package files, the evidence, and the log.
    assert compared == 13


def test_a_current_recording_is_compared_unchanged(tmp_path: Path) -> None:
    output = tmp_path / "output"
    build_neutral_artifact_package(output / TASK_ID)
    files = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}

    assert current_layout(output, TASK_ID, files) == (files, {})


def test_a_retired_layout_still_shows_a_difference_in_a_kept_member(tmp_path: Path) -> None:
    recorded, replayed = _package_trees(tmp_path)
    inputs = replayed / "output" / TASK_ID / "inputs.json"
    inputs.write_text('{"neutral":false}\n', encoding="utf-8")

    _, differences = _differences(recorded, replayed)

    assert [item.path for item in differences] == [f"{TASK_ID}/inputs.json"]


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
    # The evidence and the log: the failure evidence alone keeps each request.
    assert item.files_compared == 2
    assert result.skipped == 1
    after = {path: path.read_bytes() for path in unresolved_stage.rglob("*") if path.is_file()}
    assert after == before, "the recorded stage is never written"


@pytest.mark.parametrize(
    ("failures", "profile_options", "expected_retries", "fallback_used"),
    [
        pytest.param(
            lambda: [_connection_reset()],
            {},
            [{"attempt": 2, "retry_of": _CONNECTION_RETRY}],
            None,
            id="transport-retry",
        ),
        pytest.param(
            lambda: [_rate_limit()],
            {"service_tier": "flex", "service_tier_fallback": "default"},
            [_FALLBACK],
            True,
            id="service-tier-fallback",
        ),
        pytest.param(
            lambda: [_rate_limit(), _bad_gateway()],
            {"service_tier": "flex", "service_tier_fallback": "default"},
            [_FALLBACK, {"attempt": 3, "retry_of": _STATUS_RETRY}],
            True,
            id="fallback-then-transport-retry",
        ),
    ],
)
def test_gate_replays_a_stage_recorded_with_a_retry_identically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failures: Callable[[], list[BaseException]],
    profile_options: dict[str, str],
    expected_retries: list[dict[str, Any]],
    fallback_used: bool | None,
) -> None:
    stage = _record_author_stage(
        tmp_path,
        monkeypatch,
        [*failures(), "not a json plan", "still not a json plan"],
        **profile_options,
    )
    recorded = json.loads(_evidence_path(stage).read_text(encoding="utf-8"))
    first = recorded["attempts"][0]
    assert first["transport_retries"] == expected_retries
    if fallback_used is not None:
        assert first["controls"]["value"]["service_tier_fallback_used"] is fallback_used

    result = run_gate(stage, work=tmp_path / "work")

    assert result.passed, result.report()
    [item] = result.items
    assert item.served == 2 and item.unused == 0
    assert item.mismatches == [] and item.network_attempts == []


def _connection_reset() -> openai.APIConnectionError:
    request = httpx2.Request("POST", "https://profile.example.invalid/v1")
    return openai.APIConnectionError(message="connection reset", request=request)


def _bad_gateway() -> openai.InternalServerError:
    request = httpx2.Request("POST", "https://profile.example.invalid/v1")
    return openai.InternalServerError(
        "upstream failed", response=httpx2.Response(502, request=request), body=None
    )


def _rate_limit() -> openai.RateLimitError:
    request = httpx2.Request("POST", "https://profile.example.invalid/v1")
    response = httpx2.Response(429, request=request)
    return openai.RateLimitError("rate limited", response=response, body=None)


def test_gate_replays_a_stage_whose_plan_request_timed_out_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = httpx2.Request("POST", "https://profile.example.invalid/v1")
    stage = _record_author_stage(tmp_path, monkeypatch, [openai.APITimeoutError(request=request)])
    [attempt] = json.loads(_evidence_path(stage).read_text(encoding="utf-8"))["attempts"]
    assert attempt["raw_response"] == {
        "availability": "unavailable",
        "reason": "provider_failure",
    }
    assert attempt["failure"]["detail"] == "Request timed out."
    assert "transport_retries" not in attempt

    result = run_gate(stage, work=tmp_path / "work")

    assert result.passed, result.report()
    [item] = result.items
    assert item.exit_code == item.recorded_exit_code == 1
    assert item.status == item.recorded_status == "transport_failure"
    assert item.served == 1 and item.unused == 0
    assert item.mismatches == [] and item.network_attempts == []


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


_PACKAGE_LINE = '{{"status": "accepted", "package": "{}/SCN-1"}}'


@pytest.mark.parametrize(
    ("replayed", "expected"),
    [
        (_PACKAGE_LINE.format("/scratch/out") + "\nsame\n", None),
        (
            _PACKAGE_LINE.format("/elsewhere") + "\nsame\n",
            'line 1: $.package: "/recorded/out/SCN-1" != "/elsewhere/SCN-1"',
        ),
        (_PACKAGE_LINE.format("/scratch/out") + "\nother\n", "line 2: 'same' != 'other'"),
        (_PACKAGE_LINE.format("/scratch/out") + "\nsame\nextra\n", "2 lines != 3 lines"),
    ],
)
def test_log_comparison_maps_the_package_location_line_by_line(
    replayed: str, expected: str | None
) -> None:
    recorded = _PACKAGE_LINE.format("/recorded/out") + "\nsame\n"

    found = replay_gate._compare_log(
        recorded, replayed, "items/SCN-1.log", {"/scratch/out": "/recorded/out"}
    )

    assert found == expected


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["author"], "recorded argv has no 'generate' command: ['author']"),
        (["generate", "--output-dir=out"], "unsupported '--option=value' form in recorded argv"),
        (["generate", "--task-id", "SCN-1"], "recorded argv for SCN-1 has no --output-dir"),
    ],
)
def test_prepare_item_rejects_argv_it_cannot_redirect(
    tmp_path: Path, argv: list[str], message: str
) -> None:
    copies = replay_gate._InputCopies(tmp_path / "inputs")

    with pytest.raises(ValueError, match=re.escape(message)):
        replay_gate._prepare_item({"id": "SCN-1", "argv": argv}, copies, tmp_path / "out")
