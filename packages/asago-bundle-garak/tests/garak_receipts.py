"""Build a ``bundle-garak`` receipt from one sealed ``fixtures/ipi`` case.

The fixture tests and the fixture generator's ``--check`` both read the seven cases this
way. This module reads only the Garak adapter, so its importers need no MiDojo adapter.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from asago_bundle_core.bundle_contract import write_bundle
from asago_bundle_garak.report_parse import parse_report

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ipi"


def load(case: str, name: str) -> Any:
    """Return one JSON document of ``case``."""

    return json.loads((FIXTURES / case / name).read_text(encoding="utf-8"))


def canonical(value: Any) -> str:
    """The byte form the two receipts compare by: canonical JSON, no whitespace."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def garak_receipt(root: Path, case: str) -> dict[str, Any]:
    """Read the case through ``bundle-garak``'s ``parse`` of the report the probe wrote."""

    source = load(case, "ipi-episodes.json")
    (entry,) = source
    metadata = entry["metadata"]
    bundle = root / "bundle"
    (bundle / "reports").mkdir(parents=True)
    (bundle / "ipi-episodes.json").write_text(canonical(source), encoding="utf-8")
    manifest = {
        "schema_version": "tool-bundle-v2",
        "tool": "garak",
        "scenario_id": metadata["scenario_id"],
        "package_id": metadata["package_id"],
        "package_digest": metadata["package_digest"],
        "claim_level": metadata["claim_level"],
        "conversation_mode": metadata["delivery"],
        "template": "garak-ipi-episode",
        "tool_revision_range": ">=968cc224",
        "plugins": ["midojo.InjectionSpecProbe", "midojo.EvaluationVerdict", "midojo.EvaluationEffect"],
        "values_required": [],
        "environment": [],
        "templates": {},
        "entrypoint": ["true"],
        "native_outputs": [f"reports/{case}.report.jsonl"],
    }
    manifest = write_bundle(manifest, writer="tests.garak_receipts", writer_version="0.1.0")
    (bundle / "bundle.json").write_text(canonical(manifest), encoding="utf-8")
    messages = [{"role": "user", "content": turn} for turn in entry["injection_spec"]["turns"]]
    (bundle / "conversations.jsonl").write_text(
        canonical({"messages": messages}) + "\n", encoding="utf-8"
    )
    shutil.copyfile(FIXTURES / case / "report.jsonl", bundle / "reports" / f"{case}.report.jsonl")
    return parse_report(bundle, root / "garak-receipt.json")
