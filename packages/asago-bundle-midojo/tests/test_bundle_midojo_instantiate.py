"""instantiate: fill a MiDojo bundle template with orch's values."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from asago_bundle_core.schema import validate_manifest
from asago_bundle_core.slots import contains_marker
from asago_bundle_core.testing import CONDITION
from asago_bundle_core.text import canonical_text
from asago_bundle_midojo.instantiate import InstantiateError, instantiate_bundle
from conftest import PROMPT, VALUES


def suite_of(bundle: Path) -> dict[str, Any]:
    return yaml.safe_load((bundle / "asago_suite/suite.yaml").read_text(encoding="utf-8"))


def manifest_of(bundle: Path) -> dict[str, Any]:
    return json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))


def test_the_suite_file_loads_as_yaml_with_the_prompt_and_the_verdict_path(
    bundle: Path,
) -> None:
    suite = suite_of(bundle)

    assert suite["agent_runtime"] == {"type": "unmanaged"}
    (user_task,) = suite["user_tasks"]
    (injection_task,) = suite["injection_tasks"]
    assert user_task["prompt"] == PROMPT
    assert user_task["utility"] == {"asago_neutral": {}}
    assert injection_task["security"] == {
        "asago_tool_call_condition": {
            "condition": CONDITION,
            "verdict_file": str(bundle.resolve() / "verdict.json"),
        }
    }


def test_the_concrete_manifest_validates_and_renders_the_commands(bundle: Path) -> None:
    manifest = manifest_of(bundle)
    root = str(bundle.resolve())

    validate_manifest(manifest)
    assert manifest["templates"] == {}
    assert manifest["entrypoint"][:3] == ["{tool_python}", f"{root}/run_midojo.py", "--bundle"]
    assert f"{root}/midojo-logs" in manifest["entrypoint"]
    assert "http://127.0.0.1:18123" in manifest["entrypoint"]
    assert "http://127.0.0.1:18997/v1" in manifest["entrypoint"]
    assert "http://127.0.0.1:18996/sse" in manifest["entrypoint"]
    assert "fixture-model" in manifest["entrypoint"]
    assert manifest["serve"]["argv"][:3] == ["{tool_python}", "-m", "midojo.serve"]
    assert manifest["serve"]["argv"][6] == "18123"
    assert manifest["serve"]["ready_url"] == "http://127.0.0.1:18123/health"


def test_no_placeholder_is_left_in_the_commands(bundle: Path) -> None:
    manifest = manifest_of(bundle)
    words = [*manifest["entrypoint"], *manifest["serve"]["argv"], manifest["serve"]["ready_url"]]

    left = [word for word in words if "{" in word and word != "{tool_python}"]

    assert left == []


def test_the_values_digest_hashes_the_canonical_values(bundle: Path) -> None:
    expected = hashlib.sha256(canonical_text(VALUES).encode()).hexdigest()

    assert manifest_of(bundle)["values_digest"] == expected


def test_instantiate_returns_the_manifest_it_wrote(template: Path, tmp_path: Path) -> None:
    returned = instantiate_bundle(template, VALUES, tmp_path / "out")

    assert returned == manifest_of(tmp_path / "out")


def test_the_bundle_code_matches_the_template(template: Path, bundle: Path) -> None:
    for name in (
        "asago_suite/__init__.py",
        "asago_suite/suite.py",
        "asago_suite/asago_verifiers.py",
        "run_midojo.py",
    ):
        assert (bundle / name).read_bytes() == (template / name).read_bytes()


def test_no_slot_marker_is_left_in_the_suite(bundle: Path) -> None:
    assert not contains_marker(suite_of(bundle))


@pytest.mark.parametrize(
    "text",
    [
        "Plain text.",
        "Quotes \" and ' and backslash \\ and colon: yes",
        "line one\nline two\ttabbed\r\nand a carriage return",
        "Unicode: caf\u00e9 \u4e2d\u6587 \U0001f600 and a surrogate pair",
        "Separators: \u2028 \u2029 \u0085 and delete \x7f and a control \x1b",
        "Reserved YAML: - [ ] { } # & * ! | > % @ ` null true 1.5 ~",
        "  leading and trailing spaces  ",
        "Braces {not_a_probe} and {1:2} and {a b:c}",
        "\ufeff byte order mark inside",
    ],
)
def test_any_prompt_survives_the_yaml_file(template: Path, tmp_path: Path, text: str) -> None:
    values = {**VALUES, "messages": [{"role": "user", "content": text}]}

    out = tmp_path / "special"
    instantiate_bundle(template, values, out)

    (user_task,) = suite_of(out)["user_tasks"]
    assert user_task["prompt"] == text


@pytest.mark.parametrize("key", sorted(VALUES))
def test_a_missing_value_is_refused(template: Path, tmp_path: Path, key: str) -> None:
    values = {name: value for name, value in VALUES.items() if name != key}

    with pytest.raises(InstantiateError, match=f"values lack {key}"):
        instantiate_bundle(template, values, tmp_path / "out")


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("gateway_url", "ftp://x"),
        ("gateway_url", 3),
        ("mcp_url", "not a url"),
        ("model", ""),
        ("model", "  "),
        ("model", ["m"]),
        ("service_port", "18123"),
        ("service_port", 0),
        ("service_port", 65536),
        ("service_port", True),
        ("messages", []),
        ("messages", "text"),
        ("messages", [{"role": "assistant", "content": "x"}]),
        ("messages", [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]),
        ("messages", [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]),
        ("messages", [{"role": "user", "content": ""}]),
        ("messages", [{"role": "user", "content": 3}]),
        ("messages", [{"role": "user"}]),
        ("messages", [{"role": "user", "content": "Use {injection_task_0:main} here"}]),
    ],
)
def test_a_malformed_value_is_refused(template: Path, tmp_path: Path, key: str, bad: Any) -> None:
    values = copy.deepcopy(VALUES)
    values[key] = bad

    with pytest.raises(InstantiateError, match=f"malformed {key}"):
        instantiate_bundle(template, values, tmp_path / "out")


def test_values_that_are_not_an_object_are_refused(template: Path, tmp_path: Path) -> None:
    with pytest.raises(InstantiateError, match="JSON object"):
        instantiate_bundle(template, ["x"], tmp_path / "out")  # type: ignore[arg-type]


def test_a_non_empty_output_directory_is_refused(template: Path, tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x").write_text("x", encoding="utf-8")

    with pytest.raises(InstantiateError, match="not empty"):
        instantiate_bundle(template, VALUES, tmp_path / "out")


def test_a_directory_that_is_not_a_template_is_refused(tmp_path: Path) -> None:
    (tmp_path / "t").mkdir()

    with pytest.raises(InstantiateError, match="unreadable"):
        instantiate_bundle(tmp_path / "t", VALUES, tmp_path / "out")


def test_a_manifest_that_is_not_a_template_is_refused(template: Path, tmp_path: Path) -> None:
    (template / "bundle.json").write_text("{}", encoding="utf-8")

    with pytest.raises(InstantiateError, match="not a bundle template"):
        instantiate_bundle(template, VALUES, tmp_path / "out")


def test_a_template_without_its_bundle_code_is_refused(template: Path, tmp_path: Path) -> None:
    (template / "run_midojo.py").unlink()

    with pytest.raises(InstantiateError, match="run_midojo.py"):
        instantiate_bundle(template, VALUES, tmp_path / "out")
