"""parse: the HTTP requests OpenShell sealed, as the receipt's ``runtime_observations``.

``fixtures/openshell-4e5176a/`` holds the referral run MiDojo 4e5176a sealed with OpenShell
observations: one allowed request in ``http-request``, none in ``http-requests-empty``. The
receipt's ``http_requests`` copies the sealed records; it is left out, not observed, when
the snapshot has no OpenShell observation or no ``http_requests``, or an empty list from a
log read that is not complete. A record of another shape fails the receipt. The other cases
edit a copy of a snapshot and reseal it (``snapshot_digest``), the verdict renamed to it.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from episode_cases import FIXTURES
from result_record_cases import RESULT_RECORDS

from asago_bundle_midojo.sealed import parse_sealed
from asago_bundle_midojo.snapshot_digest import snapshot_digest
from conftest import RECEIPT_SCHEMA, REPO

SCHEMA = json.loads(RECEIPT_SCHEMA.read_text(encoding="utf-8"))
OPENSHELL = FIXTURES / "openshell-4e5176a"
REVISION = json.loads((OPENSHELL / "recording.json").read_text(encoding="utf-8"))[
    "midojo_revision"
]
CASES = REPO / "contracts/execution-receipt/execution-receipt-v1/valid"
DENIED = {
    "decision": "denied",
    "host": "exfil.test",
    "method": "POST",
    "path": "/collect",
    "port": 8443,
    "query": "patient_id=P-1",
    "raw": "HTTP:POST [MED] DENIED POST https://exfil.test:8443/collect?patient_id=P-1",
    "url": "https://exfil.test:8443/collect?patient_id=P-1",
}


def sealed(scenario: str) -> dict[str, Any]:
    return json.loads((OPENSHELL / scenario / "snapshot.json").read_text(encoding="utf-8"))


def receipt_of(
    tmp_path: Path, scenario: str, snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Parse ``scenario``'s run; ``snapshot`` replaces its snapshot, resealed, with the
    verdict renamed to it and given its evidence status."""

    work = tmp_path / "run"
    work.mkdir()
    shutil.copy(OPENSHELL / scenario / "snapshot.json", work / "snapshot.json")
    shutil.copy(OPENSHELL / scenario / "verdict.json", work / "verdict.json")
    if snapshot is not None:
        snapshot = copy.deepcopy(snapshot)
        snapshot["snapshot_id"] = snapshot_digest(snapshot)
        verdict = json.loads((work / "verdict.json").read_text(encoding="utf-8"))
        verdict["snapshot_id"] = snapshot["snapshot_id"]
        verdict["grade"]["evidence_status"] = snapshot["evidence_status"]
        (work / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        (work / "verdict.json").write_text(json.dumps(verdict), encoding="utf-8")
    receipt = parse_sealed(
        RESULT_RECORDS / "referral",
        work / "snapshot.json",
        work / "verdict.json",
        work / "receipt.json",
        tool_revision=REVISION,
    )
    jsonschema.validate(receipt, SCHEMA)
    return receipt


def observed(receipt: dict[str, Any]) -> dict[str, Any]:
    (attempt,) = receipt["attempts"]
    return attempt["runtime_observations"]


def with_requests(requests: Any, scenario: str = "http-request") -> dict[str, Any]:
    snapshot = sealed(scenario)
    snapshot["observations"]["openshell"]["http_requests"] = requests
    return snapshot


def allowed_and_denied() -> dict[str, Any]:
    """The ``http-request`` snapshot with a denied request after its allowed one."""

    snapshot = sealed("http-request")
    snapshot["observations"]["openshell"]["http_requests"].append(DENIED)
    return snapshot


# --- observed -----------------------------------------------------------------------------


def test_a_sealed_request_is_copied_as_sealed(tmp_path: Path) -> None:
    requests = sealed("http-request")["observations"]["openshell"]["http_requests"]

    assert observed(receipt_of(tmp_path, "http-request"))["http_requests"] == requests


def test_allowed_and_denied_requests_keep_their_log_order(tmp_path: Path) -> None:
    requests = observed(receipt_of(tmp_path, "http-request", allowed_and_denied()))

    assert [request["decision"] for request in requests["http_requests"]] == [
        "allowed",
        "denied",
    ]
    assert requests["http_requests"][1] == DENIED


def test_a_complete_log_read_with_no_request_is_an_empty_list(tmp_path: Path) -> None:
    assert observed(receipt_of(tmp_path, "http-requests-empty"))["http_requests"] == []


def test_a_record_without_its_optional_fields_is_copied_without_them(tmp_path: Path) -> None:
    bare = {key: DENIED[key] for key in ("method", "url", "decision", "raw")}

    receipt = receipt_of(tmp_path, "http-request", with_requests([bare]))

    assert observed(receipt)["http_requests"] == [bare]


def test_a_record_with_null_parts_is_copied_with_them(tmp_path: Path) -> None:
    unsplit = {
        "decision": "denied",
        "host": None,
        "method": "GET",
        "path": None,
        "port": None,
        "query": None,
        "raw": "HTTP:GET [INFO] DENIED GET http://[::1/steal",
        "url": "http://[::1/steal",
    }

    receipt = receipt_of(tmp_path, "http-request", with_requests([unsplit]))

    assert observed(receipt)["http_requests"] == [unsplit]


@pytest.mark.parametrize("status", ["incomplete", "failed"])
def test_requests_read_from_a_log_read_in_part_are_kept(tmp_path: Path, status: str) -> None:
    snapshot = allowed_and_denied()
    snapshot["evidence_status"]["openshell.ocsf"] = status

    requests = observed(receipt_of(tmp_path, "http-request", snapshot))["http_requests"]

    assert [request["decision"] for request in requests] == ["allowed", "denied"]


# --- not observed -------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["incomplete", "failed", "not_collected", None])
def test_no_request_from_a_log_read_that_is_not_complete_is_not_observed(
    tmp_path: Path, status: str | None
) -> None:
    snapshot = sealed("http-requests-empty")
    if status is None:
        del snapshot["evidence_status"]["openshell.ocsf"]
    else:
        snapshot["evidence_status"]["openshell.ocsf"] = status

    assert "http_requests" not in observed(receipt_of(tmp_path, "http-requests-empty", snapshot))


def test_an_observation_sealed_before_http_requests_is_not_observed(tmp_path: Path) -> None:
    snapshot = sealed("http-requests-empty")
    del snapshot["observations"]["openshell"]["http_requests"]

    receipt = receipt_of(tmp_path, "http-requests-empty", snapshot)

    assert receipt["episode"]["snapshot_id"] == sealed("http-requests-empty")["snapshot_id"]
    assert "http_requests" not in observed(receipt)


def test_a_snapshot_without_openshell_observations_is_not_observed(tmp_path: Path) -> None:
    snapshot = sealed("http-request")
    del snapshot["observations"]["openshell"]

    assert "http_requests" not in observed(receipt_of(tmp_path, "http-request", snapshot))


def test_another_source_s_http_requests_are_not_read(tmp_path: Path) -> None:
    snapshot = sealed("http-request")
    snapshot["observations"]["other"] = snapshot["observations"].pop("openshell")

    assert "http_requests" not in observed(receipt_of(tmp_path, "http-request", snapshot))


def test_the_rest_of_the_runtime_observations_is_unchanged(tmp_path: Path) -> None:
    rest = {key: value for key, value in observed(receipt_of(tmp_path, "http-request")).items()}
    del rest["http_requests"]

    assert rest == {
        "source": None,
        "processes": [],
        "network_calls_allowed": [],
        "network_calls_blocked": [],
        "files_created": [],
        "files_modified": [],
        "files_deleted": [],
        "security_findings": [],
    }


# --- malformed ----------------------------------------------------------------------------


def _changed(**changes: Any) -> dict[str, Any]:
    return {**DENIED, **changes}


def _without(key: str) -> dict[str, Any]:
    return {name: value for name, value in DENIED.items() if name != key}


@pytest.mark.parametrize(
    "requests",
    [
        None,
        {},
        "GET http://example.test/",
        [None],
        ["HTTP:GET [INFO] ALLOWED GET http://example.test/"],
        [_changed(decision="blocked")],
        [_changed(decision="ALLOWED")],
        [_changed(decision=["denied"])],
        [_changed(method=None)],
        [_changed(url=None)],
        [_changed(raw=1)],
        [_changed(host=8443)],
        [_changed(path=["/collect"])],
        [_changed(query={})],
        [_changed(port="8443")],
        [_changed(port=True)],
        [_changed(port=8443.0)],
        [_without("method")],
        [_without("url")],
        [_without("decision")],
        [_without("raw")],
        [_changed(status=403)],
        [DENIED, _changed(decision="blocked")],
    ],
    ids=repr,
)
def test_a_malformed_http_requests_fails_the_receipt(tmp_path: Path, requests: Any) -> None:
    receipt = receipt_of(tmp_path, "http-request", with_requests(requests))

    assert (receipt["execution_status"], receipt["incomplete_reason"]) == (
        "failed",
        "snapshot_malformed",
    )
    assert receipt["attempts"] == []


def test_the_snapshot_id_is_computed_as_before() -> None:
    for scenario in ("http-request", "http-requests-empty"):
        snapshot = sealed(scenario)
        assert snapshot_digest(snapshot) == snapshot["snapshot_id"]


# --- the contract's cases -----------------------------------------------------------------


def test_the_contract_s_http_requests_case_is_parse_s_receipt_of_an_allowed_and_denied_run(
    tmp_path: Path,
) -> None:
    receipt = receipt_of(tmp_path, "http-request", allowed_and_denied())

    assert receipt == json.loads(
        (CASES / "midojo-openshell-http-requests.json").read_text(encoding="utf-8")
    )


def test_the_contract_s_empty_case_is_parse_s_receipt_of_the_run_with_no_request(
    tmp_path: Path,
) -> None:
    receipt = receipt_of(tmp_path, "http-requests-empty")

    assert receipt == json.loads(
        (CASES / "midojo-openshell-http-requests-empty.json").read_text(encoding="utf-8")
    )
