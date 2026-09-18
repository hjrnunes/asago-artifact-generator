"""Reassemble the fresh G07 package from pinned evidence without transport."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from asago_artifact_generator.authoring import (
    PromptPacket,
    _decode_json_response,
    _package_from_responses,
    assert_no_secrets,
    collect_artifact_findings,
    collect_plan_findings,
)
from asago_artifact_generator.failure_evidence import load_failure_evidence
from asago_artifact_generator.input_adapter import InputKind, load_input
from asago_artifact_generator.metadata_policy import secret_metadata_paths
from asago_artifact_generator.package_io import load_package, write_package

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = ROOT / "runs" / "authoring" / "g07-fresh-20260918"
FAILURE_EVIDENCE = EVIDENCE_DIR / "G07-fresh-20260918.failure-evidence.json"
TERMINAL_RECORD = EVIDENCE_DIR / "g07-fresh-terminal-failure.json"
PACKAGE_PATH = EVIDENCE_DIR / "G07-fresh-20260918"
INVENTORY_PATH = EVIDENCE_DIR.parent / "g07-live-input" / "inventory.json"
RUNTIME_PATH = EVIDENCE_DIR.parent / "g07-live-input" / "runtime-contract.json"
SOURCE_HASHES_PATH = EVIDENCE_DIR.parent / "g07-live-input" / "source-hashes.json"
GOLD_ROOT = Path(
    "/Users/hjrnunes/workspace/redhat/hjrnunes/asago-scenario-generator/data/gold/miniklarna"
)

EXPECTED_FAILURE_EVIDENCE_SHA256 = (
    "08d1a9cf454c491c9758fe8097dcbf7453a59d7d06b2cb7fbeb74df1697672d2"
)
EXPECTED_TERMINAL_RECORD_SHA256 = (
    "f5bee6c9df096250d99caf305593b947e656be88ac0339c1c7c13dd13cfd1ecb"
)
EXPECTED_RAW_SHA256 = {
    "call1": "3bbbb50f7ed4396f12ef628ea648e81a8255e250779ad9531d6febec0ea83d10",
    "call2": "b9796b620993f46165a7e29ca74fa7156ff00c4cb2a89c998153fb1b1354a636",
}
EXPECTED_SOURCE_HASHES = {
    "gold_cases": "752adc33d01678664191d0ed6a3fc8d125c87c90b873a4a1e232617166a49b92",
    "benchmark_v4": "9db76badc3690bfd1e5e5c480ae206195fdd47703e0dfc211380540c35d4f0bd",
    "inventory": "97cada5c20e6a95a611f9afd4389ea05f2f723fb9f0979ecaced314fcb3ba050",
    "runtime_contract": "3d5f4039436d30bbfc108c37213ff3d92d0391661e572d8f6556e4b2fc740649",
}
ZERO_CONTACTS = {
    "authoring": 0,
    "judge": 0,
    "discovery": 0,
    "setup_capture": 0,
    "target_generation": 0,
    "garak": 0,
    "mcp": 0,
    "endpoint": 0,
    "service": 0,
    "producer_generation": 0,
}


def main() -> None:
    original_hashes = {
        "failure_evidence": _sha256_file(FAILURE_EVIDENCE),
        "terminal_record": _sha256_file(TERMINAL_RECORD),
    }
    _require(original_hashes["failure_evidence"] == EXPECTED_FAILURE_EVIDENCE_SHA256)
    _require(original_hashes["terminal_record"] == EXPECTED_TERMINAL_RECORD_SHA256)
    evidence = load_failure_evidence(FAILURE_EVIDENCE)
    terminal = _load_json(TERMINAL_RECORD)
    inventory = _load_json(INVENTORY_PATH)
    runtime_contract = _load_json(RUNTIME_PATH)
    source_hashes = _load_json(SOURCE_HASHES_PATH)
    _verify_pinned_sources(source_hashes)

    attempts = evidence["attempts"]
    _require(len(attempts) == 2)
    _require([attempt["stage"] for attempt in attempts] == ["call1", "call2"])
    _require([attempt["dispatch_index"] for attempt in attempts] == [1, 2])
    _require(all(attempt["task_id"] == "G07-fresh-20260918" for attempt in attempts))
    _require(all(attempt["findings"] == [] for attempt in attempts))
    _require(all(attempt["controls"]["value"]["max_retries"] == 0 for attempt in attempts))
    _require(all(attempt["controls"]["value"]["temperature"] == 0.0 for attempt in attempts))
    _require(all(attempt["transformation"] == "outer_fence_removed" for attempt in attempts))
    _require(terminal["fresh_attempts"] == _terminal_attempt_summary(attempts))
    _require(terminal["contact_totals"]["authoring"] == 2)
    _require(
        all(value == 0 for key, value in terminal["contact_totals"].items() if key != "authoring")
    )

    raw_responses: dict[str, bytes] = {}
    decoded_responses: dict[str, Any] = {}
    prompt_packets: dict[str, PromptPacket] = {}
    ledger: list[dict[str, Any]] = []
    transformations: list[str] = []
    for attempt in attempts:
        stage = attempt["stage"]
        raw = base64.b64decode(attempt["raw_response"]["base64"])
        raw_hash = _sha256(raw)
        _require(raw_hash == EXPECTED_RAW_SHA256[stage])
        _require(raw_hash == attempt["raw_response"]["sha256"])
        decoded, transformation = _decode_json_response(raw)
        _require(transformation == attempt["transformation"])
        _require(decoded == attempt["decoded_output"])
        assert_no_secrets(decoded)
        raw_responses[stage] = raw
        raw_responses[f"dispatch:{attempt['dispatch_index']}"] = raw
        decoded_responses[stage] = decoded
        prompt = attempt["prompt"]
        prompt_payload = json.loads(prompt["user"])
        prompt_packets[stage] = PromptPacket(
            stage=stage,
            version=prompt["version"],
            system=prompt["system"],
            user=prompt["user"],
            payload=prompt_payload,
        )
        ledger.append(
            {
                "dispatch_index": attempt["dispatch_index"],
                "stage": stage,
                "task_id": attempt["task_id"],
                "prompt_version": prompt["version"],
                "controls": attempt["controls"]["value"],
                "raw_response_key": stage,
                "usage": attempt["usage"]["value"],
                "validation": "passed",
                "transformation": transformation,
                "decoded_output": decoded,
            }
        )
        transformations.append(transformation)

    plan = decoded_responses["call1"]
    artifact = decoded_responses["call2"]
    _require(not collect_plan_findings(plan, inventory, runtime_contract))
    _require(
        not collect_artifact_findings(
            plan=plan,
            artifact=artifact,
            inventory=inventory,
            runtime_contract=runtime_contract,
        )
    )
    view = load_input(
        GOLD_ROOT / "gold-cases.yaml",
        kind=InputKind.REFERENCE_TASK,
        reference_label="miniklarna-gold-development-reference",
        reference_id="G07",
        benchmark_source_path=GOLD_ROOT / "benchmark-v4.yaml",
    )
    _require(
        view.source_digests
        == {
            "input": EXPECTED_SOURCE_HASHES["gold_cases"],
            "benchmark": EXPECTED_SOURCE_HASHES["benchmark_v4"],
        }
    )
    _require(_sha256_file(INVENTORY_PATH) == EXPECTED_SOURCE_HASHES["inventory"])
    _require(_sha256_file(RUNTIME_PATH) == EXPECTED_SOURCE_HASHES["runtime_contract"])
    _require(inventory == prompt_packets["call1"].payload["environment_inventory"])
    _require(runtime_contract == prompt_packets["call1"].payload["runtime_contract"])

    package = _package_from_responses(
        view=view,
        plan=plan,
        artifact=artifact,
        task_id="G07-fresh-20260918",
        ledger=ledger,
        raw_responses=raw_responses,
        decoded_responses=decoded_responses,
        prompt_packets=prompt_packets,
        transformations=transformations,
        inventory=inventory,
        runtime_contract=runtime_contract,
    )
    _require(not secret_metadata_paths(package.manifest.authoring))
    _require(not secret_metadata_paths(package.manifest.creation_model))
    write_package(PACKAGE_PATH, package)
    loaded = load_package(PACKAGE_PATH)
    _require(loaded.manifest.to_dict() == package.manifest.to_dict())

    with tempfile.TemporaryDirectory(prefix="g07-package-tamper-") as temporary:
        tampered = Path(temporary) / "package"
        shutil.copytree(PACKAGE_PATH, tampered)
        (tampered / "detector.py").write_bytes(b"# tampered\n")
        try:
            load_package(tampered)
        except ValueError as exc:
            tamper_result = {"status": "rejected", "detail": str(exc)}
        else:
            raise AssertionError("tampered package unexpectedly loaded")

    package_manifest = loaded.manifest.to_dict()
    replay_evidence = {
        "schema_version": "g07-offline-package-replay-v1",
        "source": {
            "failure_evidence": str(FAILURE_EVIDENCE),
            "failure_evidence_sha256": original_hashes["failure_evidence"],
            "terminal_record": str(TERMINAL_RECORD),
            "terminal_record_sha256": original_hashes["terminal_record"],
            "inventory_sha256": _sha256_file(INVENTORY_PATH),
            "runtime_contract_sha256": _sha256_file(RUNTIME_PATH),
            "gold_cases_sha256": view.source_digests["input"],
            "benchmark_v4_sha256": view.source_digests["benchmark"],
        },
        "attempts": [
            {
                "stage": attempt["stage"],
                "dispatch_index": attempt["dispatch_index"],
                "task_id": attempt["task_id"],
                "raw_response_sha256": EXPECTED_RAW_SHA256[attempt["stage"]],
                "decoded_output_sha256": _sha256_json(attempt["decoded_output"]),
                "controls": attempt["controls"]["value"],
                "findings": attempt["findings"],
                "transformation": attempt["transformation"],
            }
            for attempt in attempts
        ],
        "contacts": ZERO_CONTACTS,
        "private_model_client_constructed": False,
        "transport_constructed": False,
        "outputs_replaced_or_edited": False,
        "package_assembly": "replayed_from_exact_captured_outputs",
    }
    assembly_evidence = {
        "schema_version": "g07-offline-package-assembly-v1",
        "package_path": str(PACKAGE_PATH),
        "package_manifest_digest": loaded.manifest.manifest_digest,
        "package_member_digests": loaded.manifest.members,
        "manifest_source_digests": loaded.manifest.source_digests,
        "source_identity": replay_evidence["source"],
        "raw_response_sha256": EXPECTED_RAW_SHA256,
        "original_failure_evidence_unchanged": _sha256_file(FAILURE_EVIDENCE)
        == EXPECTED_FAILURE_EVIDENCE_SHA256,
        "original_terminal_record_unchanged": _sha256_file(TERMINAL_RECORD)
        == EXPECTED_TERMINAL_RECORD_SHA256,
        "manifest_reload": "passed",
        "secret_scan": "passed",
        "source_and_input_pins": "passed",
        "tamper_check": tamper_result,
        "contacts": ZERO_CONTACTS,
        "detector_checks": "not_run_by_consumer_worker",
        "safe_execution": "not_run_by_consumer_worker",
    }
    _atomic_json(EVIDENCE_DIR / "G07-fresh-20260918.offline-replay.json", replay_evidence)
    _atomic_json(EVIDENCE_DIR / "G07-fresh-20260918.offline-assembly.json", assembly_evidence)
    print(
        json.dumps(
            {
                "package_manifest_digest": package_manifest["manifest_digest"],
                "package_members": len(loaded.members),
                "tamper": tamper_result["status"],
            },
            sort_keys=True,
        )
    )


def _verify_pinned_sources(source_hashes: dict[str, str]) -> None:
    expected_paths = {
        str(GOLD_ROOT / "gold-cases.yaml"): EXPECTED_SOURCE_HASHES["gold_cases"],
        str(GOLD_ROOT / "benchmark-v4.yaml"): EXPECTED_SOURCE_HASHES["benchmark_v4"],
    }
    for path, expected in expected_paths.items():
        _require(source_hashes[path]["sha256"] == expected)
        _require(_sha256_file(Path(path)) == expected)


def _terminal_attempt_summary(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "stage": attempt["stage"],
            "dispatch_index": attempt["dispatch_index"],
            "status": "spent",
            "controls": attempt["controls"],
            "findings": attempt["findings"],
            "raw_response": {
                "availability": "available",
                "byte_length": attempt["raw_response"]["byte_length"],
                "sha256": attempt["raw_response"]["sha256"],
            },
            "usage": {
                "availability": "available",
                "total_tokens": attempt["usage"]["value"]["total_tokens"],
            },
            "transformations": attempt["transformation"],
        }
        for attempt in attempts
    ]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    return _sha256(path.read_bytes())


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _require(condition: bool) -> None:
    if not condition:
        raise AssertionError("pinned offline replay verification failed")


if __name__ == "__main__":
    main()
