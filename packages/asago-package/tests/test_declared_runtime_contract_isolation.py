"""The runtime-contract builder loads no authoring or adapter module.

The orchestrator runs the builder in the consumer's interpreter on a target's
declaration, so importing it must not load the authoring application, a bundle
adapter, the injection-spec library or YAML. The probe runs in a fresh
interpreter because the rest of this suite imports other modules; ``sys.path``
puts this library's ``src`` first, so the probe reads the tree under test rather
than an editable install of another one.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

LIBRARY = Path(__file__).resolve().parents[1]
SRC = LIBRARY / "src"
BUILDER = "asago_package.declared_runtime_contract"
APPLICATION = (
    "asago_artifact_generator",
    "asago_bundle_core",
    "asago_bundle_garak",
    "asago_injection_spec",
    "yaml",
)

_PROBE = f"""\
import json, sys
sys.path.insert(0, {str(SRC)!r})
import {BUILDER}
print(json.dumps({{
    "file": {BUILDER}.__file__,
    "modules": sorted(sys.modules),
}}))
"""


def _probe() -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def test_the_probe_reads_the_builder_in_this_library() -> None:
    assert Path(_probe()["file"]).resolve().is_relative_to(SRC)


def test_importing_the_builder_loads_no_application_module() -> None:
    loaded = _probe()["modules"]

    assert BUILDER in loaded
    assert [name for name in loaded if name.partition(".")[0] in APPLICATION] == []
