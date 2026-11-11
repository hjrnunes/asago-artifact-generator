"""The episode verifier's two import paths: its sibling rubric, and the judge fallback.

``tools/fixture-generators/asago_verifiers/asago_episode_judge.py`` is the verifier a
reply episode names. Beside an episode it sits with ``asago_rubric.py``, a byte-for-byte
MiDojo-adapter copy that imports the standard library only, and the verifier imports
that sibling first; without it, the verifier imports ``midojo_spec.rubric_judge``
directly, the path the removed ``authoring/rubric_judge.py`` shim used to serve. Both
paths must bind every name the verifier uses, so each test loads the file from a fresh
copy of the package, once without the sibling and once with it.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

from midojo_spec import rubric_judge

REPO = Path(__file__).resolve().parents[1]
VERIFIERS = REPO / "tools" / "fixture-generators" / "asago_verifiers"
# Every name the verifier's import binds, apart from the aliased ``_record``.
NAMES = ("RUBRIC_NAME", "build_evidence", "field", "http_judge", "is_complete", "judge_output")


def _verifier(tmp_path: Path, *, sibling: bool) -> Any:
    """Load the episode verifier from a fresh ``asago_verifiers`` package in ``tmp_path``."""

    package = tmp_path / "asago_verifiers"
    package.mkdir()
    for name in ("__init__.py", "asago_episode_judge.py") + (
        ("asago_rubric.py",) if sibling else ()
    ):
        (package / name).write_bytes((VERIFIERS / name).read_bytes())
    sys.path.insert(0, str(tmp_path))
    try:
        for name in [n for n in sys.modules if n.startswith("asago_verifiers")]:
            del sys.modules[name]
        return importlib.import_module("asago_verifiers.asago_episode_judge")
    finally:
        for name in [n for n in sys.modules if n.startswith("asago_verifiers")]:
            del sys.modules[name]
        sys.path.remove(str(tmp_path))


def test_the_fallback_binds_the_contracts_judge(tmp_path: Path) -> None:
    verifier = _verifier(tmp_path, sibling=False)

    assert not (tmp_path / "asago_verifiers" / "asago_rubric.py").exists()
    for name in NAMES:
        assert getattr(verifier, name) is getattr(rubric_judge, name)
    assert verifier._record is rubric_judge.record
    assert verifier.RUBRIC_NAME == "asago_rubric_judge"
    assert verifier.EPISODE_RUBRIC_NAME == "asago_rubric_judge_episode"


def test_the_sibling_rubric_wins_beside_the_verifier(tmp_path: Path) -> None:
    verifier = _verifier(tmp_path, sibling=True)

    assert verifier.http_judge.__module__ == "asago_verifiers.asago_rubric"
    assert verifier._record.__module__ == "asago_verifiers.asago_rubric"
    assert verifier._record.__name__ == "_record"
    assert verifier._record is not rubric_judge.record
    assert verifier.RUBRIC_NAME == "asago_rubric_judge"
