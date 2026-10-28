"""The snapshot ID of a snapshot with OpenShell observations, as MiDojo 4e5176a digests it.

From 4e5176a MiDojo leaves an observation's later list fields out of the snapshot digest
while each is exactly an empty list (``observations.digest_content``), so a snapshot
recorded before the field existed keeps its ID; today only OpenShell's ``http_requests``.
``fixtures/openshell-4e5176a/<scenario>/`` holds the snapshot and verdict MiDojo sealed and
graded at 4e5176a from the referral episode of ``fixtures/result-record/``, with OpenShell
observations from fixed OCSF lines (``fixtures/openshell-4e5176a/generate.py``): no HTTP
request in ``http-requests-empty``, one in ``http-request``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from episode_cases import FIXTURES
from result_record_cases import RESULT_RECORDS

from asago_bundle_midojo.sealed import parse_sealed
from asago_bundle_midojo.snapshot_digest import (
    CONTENT_FIELDS,
    call_content,
    canonical,
    dumps,
    snapshot_digest,
)
from conftest import RECEIPT_SCHEMA

SCHEMA = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
OPENSHELL = FIXTURES / "openshell-4e5176a"
RECORDING = json.loads((OPENSHELL / "recording.json").read_text(encoding="utf-8"))
SCENARIOS = sorted(RECORDING["scenarios"])
EMPTY = "http-requests-empty"


def sealed(scenario: str) -> dict[str, Any]:
    return json.loads((OPENSHELL / scenario / "snapshot.json").read_text(encoding="utf-8"))


def as_dumped(snapshot: dict[str, Any]) -> str:
    """The digest of every field as the JSON holds it, as MiDojo computed before 4e5176a."""

    content = {name: snapshot[name] for name in CONTENT_FIELDS}
    content["function_calls"] = [call_content(call) for call in content["function_calls"]]
    return f"sha256:{hashlib.sha256(dumps(canonical(content)).encode()).hexdigest()}"


def with_requests(snapshot: dict[str, Any], value: Any, source: str = "openshell") -> str:
    """The digest of ``snapshot`` with its OpenShell observation under ``source`` and its
    ``http_requests`` set to ``value``."""

    edited = copy.deepcopy(snapshot)
    observed = edited["observations"].pop("openshell")
    observed["http_requests"] = value
    edited["observations"][source] = observed
    return snapshot_digest(edited)


def without(snapshot: dict[str, Any], name: str, source: str = "openshell") -> str:
    edited = copy.deepcopy(snapshot)
    observed = edited["observations"].pop("openshell")
    del observed[name]
    edited["observations"][source] = observed
    return snapshot_digest(edited)


# --- what MiDojo sealed ------------------------------------------------------------------


def test_the_fixture_names_the_revision_midojo_sealed_it_at() -> None:
    assert RECORDING["midojo_revision"].startswith("4e5176a")


def test_the_fixtures_hold_no_http_request_and_one() -> None:
    assert sealed(EMPTY)["observations"]["openshell"]["http_requests"] == []
    (request,) = sealed("http-request")["observations"]["openshell"]["http_requests"]
    assert (request["method"], request["host"], request["decision"]) == (
        "GET",
        "clinic.internal",
        "allowed",
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_port_reproduces_the_snapshot_id_midojo_computed(scenario: str) -> None:
    snapshot = sealed(scenario)

    assert RECORDING["scenarios"][scenario]["snapshot_id"] == snapshot["snapshot_id"]
    assert snapshot_digest(snapshot) == snapshot["snapshot_id"]


def test_an_empty_http_requests_is_not_encoded_as_the_json_holds_it() -> None:
    snapshot = sealed(EMPTY)

    assert as_dumped(snapshot) != snapshot["snapshot_id"]
    assert as_dumped(sealed("http-request")) == sealed("http-request")["snapshot_id"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_a_4e5176a_openshell_snapshot_parses_into_a_completed_receipt(
    tmp_path: Path, scenario: str
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    for name in ("snapshot.json", "verdict.json"):
        shutil.copy(OPENSHELL / scenario / name, run / name)

    receipt = parse_sealed(
        RESULT_RECORDS / "referral",
        run / "snapshot.json",
        run / "verdict.json",
        run / "receipt.json",
        tool_revision="4e5176a0",
        adapter_revision="abc123",
    )

    jsonschema.validate(receipt, SCHEMA)
    assert (receipt["execution_status"], receipt["incomplete_reason"]) == ("completed", None)
    assert receipt["episode"]["snapshot_id"] == RECORDING["scenarios"][scenario]["snapshot_id"]
    (attempt,) = receipt["attempts"]
    assert attempt["result"]["outcome"] == "detected"


# --- the rule's edges --------------------------------------------------------------------


def test_an_absent_http_requests_digests_as_an_empty_one() -> None:
    """A snapshot sealed before the field existed holds none, and kept its ID."""

    snapshot = sealed(EMPTY)

    assert without(snapshot, "http_requests") == snapshot["snapshot_id"]
    assert with_requests(sealed("http-request"), []) == without(
        sealed("http-request"), "http_requests"
    )


@pytest.mark.parametrize("value", [None, {}, 0, False, "", [[]]], ids=repr)
def test_any_other_value_is_encoded_as_it_is(value: Any) -> None:
    snapshot = sealed(EMPTY)

    assert with_requests(snapshot, value) != snapshot["snapshot_id"]


def test_a_non_empty_http_requests_is_encoded() -> None:
    snapshot = sealed("http-request")

    assert without(snapshot, "http_requests") != snapshot["snapshot_id"]


def test_another_empty_list_of_the_openshell_observation_is_encoded() -> None:
    snapshot = sealed(EMPTY)

    assert snapshot["observations"]["openshell"]["files_deleted"] == []
    assert without(snapshot, "files_deleted") != snapshot["snapshot_id"]


def test_a_source_without_the_rule_encodes_an_empty_list_as_it_is() -> None:
    snapshot = sealed(EMPTY)

    assert with_requests(snapshot, [], source="other") != without(
        snapshot, "http_requests", source="other"
    )
