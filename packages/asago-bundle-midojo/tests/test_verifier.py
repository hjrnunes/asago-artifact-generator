"""The bundle's verifiers: the neutral utility check and the registration of the plugins."""

from __future__ import annotations

import importlib
import subprocess
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from asago_bundle_midojo.compiler import SUITE_PACKAGE, compile_package, write_bundle_code
from conftest import compile_options


@pytest.fixture(scope="module")
def verifiers(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ModuleType]:
    """The verifier module as a bundle imports it, from a suite package the compiler writes."""

    root = tmp_path_factory.mktemp("suite")
    write_bundle_code(root)
    sys.path.insert(0, str(root))
    try:
        yield importlib.import_module(f"{SUITE_PACKAGE}.asago_verifiers")
    finally:
        sys.path.remove(str(root))
        for name in [n for n in sys.modules if n.split(".")[0] == SUITE_PACKAGE]:
            del sys.modules[name]


def record(name: str, arguments: Any, error: str | None = None) -> SimpleNamespace:
    """A MiDojo function-call record, reduced to the fields the verifier reads."""

    return SimpleNamespace(function=name, args=arguments, error=error, result="", blocked=False)


def context(calls: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        agent_output="", function_calls=calls, pre_environment=None, post_environment=None
    )


def captured(vector: dict[str, Any], unsettled: str) -> list[SimpleNamespace]:
    """Return the vector's calls as records; an incomplete capture ends in an unsettled call."""

    calls = [record(call["name"], call["arguments"]) for call in vector["calls"]]
    if vector["complete"]:
        return calls
    if calls:
        calls[-1].error = unsettled
        return calls
    return [record("unsettled", {}, unsettled)]


def test_the_neutral_check_always_passes(
    verifiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Result:
        def __init__(self, passed: bool, reason: str) -> None:
            self.passed, self.reason = passed, reason

    module = types.ModuleType("midojo.verifiers")
    module.VerificationResult = Result  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "midojo", types.ModuleType("midojo"))
    monkeypatch.setitem(sys.modules, "midojo.verifiers", module)
    neutral = verifiers.NeutralVerifier()

    parsed = neutral.parse({})

    assert neutral.evaluate(parsed, context([])) is True
    assert neutral.assess(parsed, context([])).passed is True
    assert "no utility check" in neutral.assess(parsed, context([])).reason
    with pytest.raises(ValueError):
        neutral.parse({"anything": 1})


def test_register_adds_the_verifiers_once(
    verifiers: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered: list[str] = []

    def register_verifier(verifier: Any) -> None:
        if verifier.name in registered:
            raise ValueError("already registered")
        registered.append(verifier.name)

    module = types.ModuleType("midojo.verifiers")
    module.register_verifier = register_verifier  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "midojo", types.ModuleType("midojo"))
    monkeypatch.setitem(sys.modules, "midojo.verifiers", module)

    verifiers.register()
    verifiers.register()

    assert registered == ["asago_rubric_judge", "asago_neutral"]


def test_the_bundle_code_runs_without_the_consumer_or_midojo(
    command_package: Path, tmp_path: Path
) -> None:
    template = tmp_path / "template"
    compile_package(command_package, template, **compile_options())
    code = (
        "import importlib.util, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "for name in ('asago_artifact_generator', 'asago_bundle_midojo', 'midojo'):\n"
        "    assert importlib.util.find_spec(name) is None, name\n"
        "from asago_suite import asago_verifiers as v\n"
        "print(v.NeutralVerifier().evaluate(None, None))\n"
    )

    done = subprocess.run(
        [sys.executable, "-S", "-E", "-s", "-c", code, str(template)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "True"
