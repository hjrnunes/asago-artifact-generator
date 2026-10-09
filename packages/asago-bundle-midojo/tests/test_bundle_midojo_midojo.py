"""The bundle in MiDojo's own interpreter (``ASAGO_MIDOJO_PYTHON``): load, grade, parse.

These tests start no model, no server and no network: the control plane is
MiDojo's FastAPI app driven through its test client, in a subprocess of the
interpreter that has ``midojo`` installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from asago_bundle_midojo.parse import parse_bundle
from conftest import PROMPT, RECEIPT_SCHEMA

INTERPRETER = os.environ.get("ASAGO_MIDOJO_PYTHON")
pytestmark = [
    pytest.mark.midojo,
    pytest.mark.skipif(not INTERPRETER, reason="ASAGO_MIDOJO_PYTHON is not set"),
]

REFERRAL = {
    "function": "get_referral",
    "args": {"patient_id": "PAT-201"},
    "result": '{"referral": "cardiology"}',
}
ALLOWED = {**REFERRAL, "args": {"patient_id": "PAT-104"}}

# Drives one evaluation through the control plane the way the orchestrator does,
# and writes the two records the run script would dump. Argument 1 is the bundle,
# argument 2 the function calls to record; the exit code and the printed JSON say
# what each step answered.
DRIVER = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from fastapi.testclient import TestClient
    from midojo.control_plane.main import create_app
    from asago_suite.suite import task_suite

    bundle, calls = Path(sys.argv[1]), json.loads(sys.argv[2])
    client = TestClient(
        create_app({"asago_suite.suite": task_suite}), raise_server_exceptions=False
    )
    run = client.post("/runs", json={"suite_name": "asago_suite.suite"}).json()["id"]
    base = f"/runs/{run}/evaluations"
    evaluation = client.post(
        base, json={"user_task_id": "asago_user_task", "injection_task_id": "asago_injection_task"}
    ).json()["id"]
    for call in calls:
        assert client.post(f"{base}/{evaluation}/function-calls", json=call).status_code == 201
    client.post(f"{base}/{evaluation}/complete", json={"agent_output": "Done."})
    graded = client.post(f"{base}/{evaluation}/grade")
    control = bundle / "control-plane"
    control.mkdir(exist_ok=True)
    (control / "run.json").write_text(client.get(f"/runs/{run}").text)
    (control / "evaluation.json").write_text(client.get(f"{base}/{evaluation}").text)
    print(json.dumps({"status": graded.status_code, "body": graded.text}))
    """
)


def midojo(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    assert INTERPRETER
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env["PYTHONPATH"] = str(cwd) if cwd else ""
    return subprocess.run(
        [INTERPRETER, *args], capture_output=True, text=True, cwd=cwd, env=env, timeout=120
    )


def drive(bundle: Path, calls: list[dict[str, Any]]) -> dict[str, Any]:
    done = midojo("-c", DRIVER, str(bundle), json.dumps(calls), cwd=bundle)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.splitlines()[-1])


def test_the_suite_loads_with_one_user_task_and_one_injection_task(bundle: Path) -> None:
    script = (
        "import json; from asago_suite.suite import task_suite as s; "
        "print(json.dumps([sorted(s.user_tasks), sorted(s.injection_tasks), "
        "s.user_tasks['asago_user_task'].prompt]))"
    )

    done = midojo("-c", script, cwd=bundle)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [["asago_user_task"], ["asago_injection_task"], PROMPT]


def test_importing_the_suite_module_twice_registers_the_verifiers_once(bundle: Path) -> None:
    script = (
        "import importlib; import asago_suite.suite as a; importlib.reload(a); "
        "from asago_suite import asago_verifiers as v; v.register(); v.register(); print('ok')"
    )

    done = midojo("-c", script, cwd=bundle)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


def test_the_orchestrator_module_runs_as_a_script_and_loads_the_suite(bundle: Path) -> None:
    done = midojo("-m", "midojo.orchestrator", "--help", cwd=bundle)

    assert done.returncode == 0, done.stderr
    assert "--suite" in done.stdout


def test_the_serve_module_loads_the_dotted_suite(bundle: Path) -> None:
    done = midojo("-m", "midojo.serve", "--help", cwd=bundle)

    assert done.returncode == 0, done.stderr
    assert "--load-suite" in done.stdout


def test_grade_is_true_when_the_condition_holds_and_the_verdict_file_says_detected(
    bundle: Path,
) -> None:
    graded = drive(bundle, [REFERRAL])

    body = json.loads(graded["body"])
    assert graded["status"] == 200
    assert (body["utility"], body["security"]) == (True, True)
    assert body["security_reason"] == "the condition holds on the captured calls"
    verdict = json.loads((bundle / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["outcome"] == "detected"
    assert verdict["matched_calls"] == [0]


def test_grade_is_false_when_the_condition_does_not_hold(bundle: Path) -> None:
    graded = drive(bundle, [ALLOWED])

    body = json.loads(graded["body"])
    assert (body["utility"], body["security"], body["security_reason"]) == (True, False, None)
    verdict = json.loads((bundle / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["outcome"] == "not_detected"


def test_grade_is_false_with_no_calls_and_the_verdict_file_tells_why(bundle: Path) -> None:
    graded = drive(bundle, [])

    assert json.loads(graded["body"])["security"] is False
    verdict = json.loads((bundle / "verdict.json").read_text(encoding="utf-8"))
    assert verdict["outcome"] == "not_detected"


def test_a_verifier_that_raises_fails_the_grade_request_and_leaves_the_evaluation_ungraded(
    bundle: Path,
) -> None:
    (bundle / "verdict.json").mkdir()

    graded = drive(bundle, [REFERRAL])

    assert graded["status"] == 500
    evaluation = json.loads((bundle / "control-plane/evaluation.json").read_text(encoding="utf-8"))
    assert evaluation["completed"] is True
    assert evaluation["security"] is None
    assert evaluation["utility"] is None


@pytest.mark.parametrize(
    ("calls", "outcome"), [([REFERRAL], "detected"), ([ALLOWED], "not_detected")]
)
def test_the_real_control_plane_records_parse_into_a_valid_receipt(
    bundle: Path, calls: list[dict[str, Any]], outcome: str
) -> None:
    drive(bundle, calls)
    (bundle / "midojo-logs").mkdir()
    (bundle / "midojo-logs/results.json").write_text("{}", encoding="utf-8")

    receipt = parse_bundle(bundle, bundle / "receipt.json")

    validator = jsonschema.Draft202012Validator(json.loads(RECEIPT_SCHEMA.read_text("utf-8")))
    assert list(validator.iter_errors(receipt)) == []
    assert receipt["execution_status"] == "completed"
    (attempt,) = receipt["attempts"]
    assert attempt["result"]["outcome"] == outcome
    assert [call["name"] for call in attempt["observation"]["tool_calls"]] == ["get_referral"]
    assert attempt["observation"]["tool_calls"][0]["result"] == {"referral": "cardiology"}


def test_an_ungraded_evaluation_parses_into_a_failed_receipt(bundle: Path) -> None:
    (bundle / "verdict.json").mkdir()
    drive(bundle, [REFERRAL])

    receipt = parse_bundle(bundle, bundle / "receipt.json")

    assert receipt["execution_status"] == "failed"
    assert receipt["incomplete_reason"] == "evaluation_ungraded"
