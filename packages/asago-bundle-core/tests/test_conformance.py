"""The conformance helper passes a faithful adapter and names each deliberate break.

``FakeAdapter`` is a small adapter over the core helpers. Each flaw switches on one defect,
so every check in ``asago_bundle_core.conformance`` is shown failing on its own.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from asago_artifact_generator.package_io import load_package
from asago_bundle_core.conformance import (
    AdapterUnderTest,
    check_compile,
    check_gap,
    check_instantiate,
    check_parse,
    check_round_trip,
)
from asago_bundle_core.errors import BundleError
from asago_bundle_core.gap import CapabilityGap, capability_gap_record
from asago_bundle_core.slots import BUNDLE, bundle_slot, fill, value_slot
from asago_bundle_core.testing import write_test_package
from asago_bundle_core.text import canonical_text
from asago_bundle_core.values import check_values, render_entrypoint

RECEIPT_SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "contracts/execution-receipt/execution-receipt-v1.schema.json"
)
VALUES = {
    "messages": [{"role": "user", "content": "Show PAT-201."}],
    "mcp_url": "http://127.0.0.1:1/mcp",
    "gateway_url": "http://127.0.0.1:2/v1",
    "model": "fixture-model",
}
OTHER_DIGEST = "1" * 64
MANIFEST_FLAWS: dict[str, dict[str, Any]] = {
    "unknown-key": {"surprise": 1},
    "wrong-digest": {"package_digest": "0" * 64},
    "wrong-scenario": {"scenario_id": "SCN-999"},
    "wrong-package-id": {"package_id": "other-package"},
    "missing-template": {
        "templates": {"conversation": "absent.template.json", "run": "run.template.json"}
    },
    "requires-lacks": {"requires": ["gateway_url", "messages", "model"]},
    "repeats-native": {"repeats": {"native": True}},
}


class FakeError(BundleError):
    """The fake adapter's own error."""


def is_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("http")


def is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


CHECKS = {"mcp_url": is_url, "gateway_url": is_url, "model": is_text}


def attempt() -> dict[str, Any]:
    return {
        "index": 0,
        "generation": {"request_count": None, "response_status": "completed", "error_type": None},
        "turns": [
            {
                "index": 0,
                "request": {"messages": VALUES["messages"]},
                "response": {"assistant_messages": [], "tool_call_indices": []},
            }
        ],
        "result": {"outcome": "detected", "claim_level": "command_attempt", "reason": "r"},
        "observation": {"source": "tool_native", "assistant_messages": [], "tool_calls": []},
        "runtime_observations": {
            "source": None,
            **{
                name: []
                for name in (
                    "processes",
                    "network_calls_allowed",
                    "network_calls_blocked",
                    "files_created",
                    "files_modified",
                    "files_deleted",
                    "security_findings",
                )
            },
        },
        "detections": [],
        "judge": None,
    }


class FakeAdapter:
    def __init__(self, *flaws: str, attempts: int = 1) -> None:
        self.flaws = set(flaws)
        self.attempts = attempts
        self.compiles = 0

    def under_test(self) -> AdapterUnderTest:
        return AdapterUnderTest(
            compile=self.compile,
            instantiate=self.instantiate,
            parse=self.parse,
            write_native=self.write_native,
            entrypoint_keys=("model",),
        )

    def compile(self, package_dir: Path, out: Path) -> dict[str, Any]:
        package = load_package(package_dir)
        self.compiles += 1
        if "refuses" in self.flaws:
            raise FakeError("the package cannot be compiled")
        if self.flaws & {"gap", "gap-bad-digest", "gap-missing-field", "gap-empty-reason"}:
            raise CapabilityGap(self.gap_record(package))
        conversation = {
            "messages": value_slot("messages"),
            "tools": [{"server_url": value_slot("mcp_url")}],
        }
        run = {"uri": value_slot("gateway_url"), "report_dir": bundle_slot("reports")}
        if "nondeterministic" in self.flaws:
            run["salt"] = self.compiles
        requires = ["gateway_url", "mcp_url", "messages", "model"]
        manifest = {
            "schema_version": "tool-bundle-v1",
            "tool": "fake",
            "tool_revision_range": ">=1",
            "package_id": package.manifest.package_id,
            "scenario_id": package.manifest.scenario_id,
            "package_digest": package.manifest.manifest_digest,
            "claim_level": "command_attempt",
            "delivery": "single",
            "target_mode": "orch_hosted",
            "plugins": ["fake.probe"],
            "requires": [*requires, "repeats"] if "repeats-native" in self.flaws else requires,
            "environment": ["FAKE_API_KEY"],
            "templates": {
                "conversation": "conversation.template.json",
                "run": "run.template.json",
            },
            "entrypoint": ["{tool_python}", "--config", "{bundle}/run.json", "--name", "{model}"],
            "native_outputs": ["reports/fake.report.jsonl"],
        }
        for flaw in self.flaws:
            manifest.update(MANIFEST_FLAWS.get(flaw, {}))
        out.mkdir(parents=True)
        (out / "conversation.template.json").write_text(canonical_text(conversation))
        (out / "run.template.json").write_text(canonical_text(run))
        (out / "bundle.json").write_text(canonical_text(manifest))
        return manifest

    def gap_record(self, package: Any) -> dict[str, Any]:
        record = capability_gap_record("fake", package, "sequential", "the fake cannot send it")
        if "gap-bad-digest" in self.flaws:
            record["package_digest"] = OTHER_DIGEST
        if "gap-missing-field" in self.flaws:
            del record["delivery"]
        if "gap-empty-reason" in self.flaws:
            record["reason"] = ""
        return record

    def instantiate(self, template: Path, values: dict[str, Any], out: Path) -> dict[str, Any]:
        manifest = json.loads((template / "bundle.json").read_text())
        if "accepts-missing" not in self.flaws:
            check_values(manifest["requires"], values, CHECKS.get, FakeError)
        templates = {
            role: json.loads((template / name).read_text())
            for role, name in manifest["templates"].items()
        }

        def resolve(kind: str, argument: Any) -> Any:
            return str(out / argument) if kind == BUNDLE else values.get(argument)

        run = templates["run"] if "slot-left" in self.flaws else fill(templates["run"], resolve)
        concrete = {
            **manifest,
            "templates": {},
            "entrypoint": render_entrypoint(
                manifest["entrypoint"], out, {"model": "", **values}, ("model",)
            ),
            "values_digest": hashlib.sha256(canonical_text(values).encode()).hexdigest(),
        }
        if "no-values-digest" in self.flaws:
            del concrete["values_digest"]
        if "wrong-values-digest" in self.flaws:
            concrete["values_digest"] = OTHER_DIGEST
        out.mkdir(parents=True)
        (out / "conversation.json").write_text(
            canonical_text(fill(templates["conversation"], resolve))
        )
        (out / "run.json").write_text(canonical_text(run))
        (out / "bundle.json").write_text(canonical_text(concrete))
        (out / "reports").mkdir()
        return concrete

    def parse(self, bundle: Path, out: Path) -> dict[str, Any]:
        manifest = json.loads((bundle / "bundle.json").read_text())
        report = bundle / manifest["native_outputs"][0]
        failed = not report.is_file() and "native-missing-ok" not in self.flaws
        reason = None if "no-reason" in self.flaws else "report_missing"
        receipt = {
            "schema_version": "execution-receipt-v1",
            "tool": {"id": "fake", "revision": None, "adapter_revision": None},
            "package": {
                "id": manifest["scenario_id"],
                "digest": OTHER_DIGEST
                if "other-package" in self.flaws
                else manifest["package_digest"],
            },
            "bundle_digest": hashlib.sha256((bundle / "bundle.json").read_bytes()).hexdigest(),
            "target": {
                "mode": manifest["target_mode"],
                "image_digest": None,
                "policy_digest": None,
                "sandbox_id": None,
            },
            "execution_status": "failed" if failed else "completed",
            "runtime_status": None if failed else "completed",
            "incomplete_reason": reason if failed else None,
            "attempts": [] if failed else [attempt() for _ in range(self.attempts)],
            "native": []
            if not report.is_file()
            else [
                {
                    "path": manifest["native_outputs"][0],
                    "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                }
            ],
        }
        if "bad-receipt" in self.flaws:
            del receipt["native"]
        out.write_text(canonical_text(receipt))
        return receipt

    def write_native(self, bundle: Path, manifest: dict[str, Any]) -> None:
        report = bundle / manifest["native_outputs"][0]
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("{}\n")


@pytest.fixture
def package(tmp_path: Path) -> Path:
    return write_test_package(tmp_path / "packages")


def round_trip(
    tmp_path: Path, package: Path, *flaws: str, values: dict[str, Any] = VALUES, attempts: int = 1
) -> list[str]:
    adapter = FakeAdapter(*flaws, attempts=attempts).under_test()
    return check_round_trip(adapter, package, values, tmp_path / "work", RECEIPT_SCHEMA)


def test_a_faithful_adapter_has_no_failures(tmp_path: Path, package: Path) -> None:
    assert round_trip(tmp_path, package) == []


def test_each_check_runs_on_its_own(tmp_path: Path, package: Path) -> None:
    adapter = FakeAdapter().under_test()
    work = tmp_path / "work"

    assert check_compile(adapter, package, work) == []
    assert check_instantiate(adapter, work / "template", VALUES, work) == []
    assert check_parse(adapter, work / "bundle", VALUES, RECEIPT_SCHEMA) == []


@pytest.mark.parametrize(
    ("flaw", "expected"),
    [
        ("unknown-key", "manifest does not validate"),
        ("wrong-digest", "package_digest"),
        ("wrong-scenario", "scenario_id"),
        ("wrong-package-id", "package_id"),
        ("nondeterministic", "two compiles of one package differ in bytes"),
        ("missing-template", "template file absent.template.json"),
        ("requires-lacks", "requires lacks ['mcp_url']"),
    ],
)
def test_a_compile_defect_is_named(
    tmp_path: Path, package: Path, flaw: str, expected: str
) -> None:
    failures = round_trip(tmp_path, package, flaw)

    assert any(expected in failure for failure in failures), failures


def test_a_compile_that_refuses_the_package_is_reported(tmp_path: Path, package: Path) -> None:
    failures = round_trip(tmp_path, package, "refuses")

    assert failures == ["compile refused the package: the package cannot be compiled"]


@pytest.mark.parametrize(
    ("flaw", "expected"),
    [
        ("accepts-missing", "instantiate accepted values without gateway_url"),
        ("no-values-digest", "values_digest is missing"),
        ("wrong-values-digest", "values_digest"),
        ("slot-left", "run.json still holds a slot marker"),
    ],
)
def test_an_instantiate_defect_is_named(
    tmp_path: Path, package: Path, flaw: str, expected: str
) -> None:
    failures = round_trip(tmp_path, package, flaw)

    assert any(expected in failure for failure in failures), failures


@pytest.mark.parametrize(
    ("flaw", "expected"),
    [
        ("bad-receipt", "receipt does not validate"),
        ("other-package", f"receipt names package digest {OTHER_DIGEST}"),
        ("native-missing-ok", "without its native output did not give execution_status failed"),
        ("no-reason", "without its native output gave no incomplete_reason"),
    ],
)
def test_a_parse_defect_is_named(tmp_path: Path, package: Path, flaw: str, expected: str) -> None:
    failures = round_trip(tmp_path, package, flaw)

    assert any(expected in failure for failure in failures), failures


def test_a_receipt_with_two_attempts_is_named_when_the_tool_does_not_repeat(
    tmp_path: Path, package: Path
) -> None:
    failures = round_trip(tmp_path, package, attempts=2)

    assert failures == ["receipt holds 2 attempts, expected 1"]


def test_a_native_repeat_expects_the_requested_count(tmp_path: Path, package: Path) -> None:
    values = {**VALUES, "repeats": 3}

    assert round_trip(tmp_path, package, "repeats-native", values=values, attempts=3) == []


def test_a_native_repeat_that_ran_once_is_named(tmp_path: Path, package: Path) -> None:
    values = {**VALUES, "repeats": 3}

    failures = round_trip(tmp_path, package, "repeats-native", values=values, attempts=1)

    assert failures == ["receipt holds 1 attempts, expected 3"]


@pytest.mark.parametrize("repeats", [0, "3", True])
def test_a_native_repeat_needs_a_count_in_the_values(
    tmp_path: Path, package: Path, repeats: Any
) -> None:
    values = {**VALUES, "repeats": repeats}

    failures = round_trip(tmp_path, package, "repeats-native", values=values)

    assert failures == [
        "repeats.native is true but values carry no positive integer repeats count"
    ]


def test_a_repeats_key_the_values_lack_stops_at_instantiate(tmp_path: Path, package: Path) -> None:
    failures = round_trip(tmp_path, package, "repeats-native")

    assert failures == ["instantiate refused the values: values lack repeats"]


def test_a_faithful_capability_gap_has_no_failures(tmp_path: Path, package: Path) -> None:
    assert round_trip(tmp_path, package, "gap") == []


@pytest.mark.parametrize(
    ("flaw", "expected"),
    [
        ("gap-bad-digest", "gap record package_digest"),
        ("gap-missing-field", "gap record fields"),
        ("gap-empty-reason", "gap record reason"),
    ],
)
def test_a_gap_record_defect_is_named(
    tmp_path: Path, package: Path, flaw: str, expected: str
) -> None:
    failures = round_trip(tmp_path, package, flaw)

    assert any(expected in failure for failure in failures), failures


def test_check_gap_refuses_a_record_that_is_not_an_object(package: Path) -> None:
    assert check_gap(["capability_gap"], package) == ["gap record is not an object"]


def test_a_compile_failure_stops_the_round_trip_before_instantiate(
    tmp_path: Path, package: Path
) -> None:
    adapter = FakeAdapter("wrong-digest")

    check_round_trip(adapter.under_test(), package, VALUES, tmp_path / "work", RECEIPT_SCHEMA)

    assert not (tmp_path / "work" / "bundle").exists()
