"""Installed wheels load a handoff and check the vendored contracts without the source tree.

The consumer wheel carries the seven kits the consumer still owns, and the ``asago-package``
wheel carries the three the package library owns, so both wheels are built and installed
before the probe imports the consumer module that re-exports the library.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.support import HANDOFFS

_REPO = Path(__file__).resolve().parents[1]
_LIBRARY = _REPO / "packages" / "asago-package"
_PROBE = """
import json, sys
from pathlib import Path

import asago_artifact_generator
import asago_package
from asago_artifact_generator import package_io, target_inputs
from asago_artifact_generator.input_adapter import load_input

view = load_input(Path(sys.argv[1]))
package_io.validate_artifact_package_contract()
target_inputs._validate_contract_lock()
target_inputs._runtime_context_schema()
print(
    json.dumps(
        {
            "package": asago_artifact_generator.__file__,
            "library": asago_package.__file__,
            "scenario_id": view.scenario_id,
        }
    )
)
"""


def _uv() -> str:
    found = os.environ.get("UV") or shutil.which("uv")
    assert found, "the wheel install test needs the uv executable"
    return found


def _run(args: list[str], cwd: Path) -> str:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"}
    }
    env["NO_COLOR"] = "1"
    done = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, f"{args[:3]} failed:\n{done.stdout}\n{done.stderr}"
    return done.stdout


def test_installed_wheel_loads_a_handoff_from_its_own_contract_copy(tmp_path: Path) -> None:
    uv = _uv()
    dist, venv = tmp_path / "dist", tmp_path / "venv"
    for source in (_REPO, _LIBRARY):
        _run([uv, "build", "--wheel", "--offline", "--out-dir", str(dist), str(source)], _REPO)
    pylock = tmp_path / "pylock.toml"
    _run(
        [uv, "export", "--frozen", "--no-dev", "--no-emit-project", "--no-emit-workspace"]
        + ["--format", "pylock.toml", "--output-file", str(pylock)],
        _REPO,
    )
    _run([uv, "venv", "--offline", "--no-config", "--python", sys.executable, str(venv)], tmp_path)
    python = venv / "bin" / "python"
    install = [uv, "pip", "install", "--offline", "--no-config", "--python", str(python)]
    _run(install + ["-r", str(pylock)], tmp_path)
    (library_wheel,) = dist.glob("asago_package-*.whl")
    (wheel,) = dist.glob("asago_artifact_generator-*.whl")
    _run(install + ["--no-deps", str(library_wheel)], tmp_path)
    _run(install + ["--no-deps", str(wheel)], tmp_path)
    handoff = tmp_path / "handoff.json"
    shutil.copyfile(HANDOFFS / "refund-bound.json", handoff)

    probe = json.loads(_run([str(python), "-I", "-c", _PROBE, str(handoff)], tmp_path))

    package = Path(probe["package"]).resolve()
    library = Path(probe["library"]).resolve()
    assert package.is_relative_to(venv.resolve())
    assert not package.is_relative_to(_REPO)
    assert library.is_relative_to(venv.resolve())
    assert not library.is_relative_to(_REPO)
    assert probe["scenario_id"] == json.loads(handoff.read_text(encoding="utf-8"))["scenario_id"]
