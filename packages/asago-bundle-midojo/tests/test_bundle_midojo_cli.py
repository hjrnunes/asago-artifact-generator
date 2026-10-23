"""The console script: subcommands and exit codes (0 done, 1 input, 2 usage, 3 gap)."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from asago_bundle_core.testing import MOCK_TARGET, write_test_package
from asago_bundle_midojo.cli import main
from asago_bundle_midojo.instantiate import instantiate_bundle
from conftest import BASE, DOMAIN, REPO, TARGET_CONFIG, VALUES, write_native

FLAGS = ["--midojo-base", str(BASE), "--domain", DOMAIN]
# Written by c18e46d's package writer (see its generate.py).
PACKAGE_WITH_REQUEST_COPIES = REPO / "tests/fixtures/package_with_request_copies/package"
# What the legacy form did at c59a8fb, recorded with that revision's code (see generate.py).
LEGACY = Path(__file__).parent / "fixtures" / "legacy_instantiate"
LEGACY_CASES = json.loads((LEGACY / "cases.json").read_text(encoding="utf-8"))["cases"]


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])

    assert raised.value.code == 0
    assert "asago-bundle-midojo" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["compile"],
        ["unknown"],
        ["compile", "x", "--out"],
        ["compile", "x", "--out", "o"],
        ["compile", "x", "--out", "o", "--midojo-base", "b"],
        ["compile", "x", "--out", "o", "--domain", "klarna"],
    ],
)
def test_a_usage_error_exits_two(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == 2


def test_compile_writes_the_template_and_exits_zero(command_package: Path, tmp_path: Path) -> None:
    code = main(["compile", str(command_package), "--out", str(tmp_path / "template"), *FLAGS])

    assert code == 0
    assert (tmp_path / "template" / "bundle.json").is_file()


def test_compile_takes_a_package_written_before_the_request_copies_were_retired(
    tmp_path: Path,
) -> None:
    # Its authoring/ request copies and source-hashes.json are verified, then ignored.
    template = tmp_path / "template"

    code = main(["compile", str(PACKAGE_WITH_REQUEST_COPIES), "--out", str(template), *FLAGS])

    assert code == 0
    manifest = json.loads((template / "bundle.json").read_text(encoding="utf-8"))
    package = json.loads((PACKAGE_WITH_REQUEST_COPIES / "manifest.json").read_text("utf-8"))
    assert manifest["package_digest"] == package["manifest_digest"]
    assert manifest["claim_level"] == "command_attempt"


def test_compile_of_a_sequential_package_writes_the_template_and_exits_zero(
    sequential_package: Path, tmp_path: Path
) -> None:
    code = main(["compile", str(sequential_package), "--out", str(tmp_path / "template"), *FLAGS])

    assert code == 0
    manifest = json.loads((tmp_path / "template" / "bundle.json").read_text(encoding="utf-8"))
    assert manifest["delivery"] == "sequential"


def test_compile_of_a_gap_prints_the_record_and_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = write_test_package(tmp_path / "packages", claim_level="state_effect")

    code = main(["compile", str(package), "--out", str(tmp_path / "template"), *FLAGS])

    assert code == 3
    record = json.loads(capsys.readouterr().out)
    assert record["tool"] == "midojo"
    assert record["delivery"] == "single"
    assert "state_effect" in record["reason"]
    assert not (tmp_path / "template").exists()


def test_compile_of_an_unknown_domain_exits_one(
    command_package: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["compile", str(command_package), "--out", str(tmp_path / "t")]
    code = main([*argv, "--midojo-base", str(BASE), "--domain", "nowhere"])

    assert code == 1
    assert "nowhere" in capsys.readouterr().err


def test_compile_of_an_unreadable_package_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["compile", str(tmp_path / "missing"), "--out", str(tmp_path / "template"), *FLAGS]
    )

    assert code == 1
    assert capsys.readouterr().err.startswith("compile: ")


def test_instantiate_writes_the_bundle_and_exits_zero(template: Path, tmp_path: Path) -> None:
    values = tmp_path / "values.json"
    values.write_text(json.dumps(VALUES), encoding="utf-8")

    code = main(
        [
            "instantiate",
            str(template),
            "--values",
            str(values),
            "--target",
            str(TARGET_CONFIG),
            "--out",
            str(tmp_path / "b"),
        ]
    )

    assert code == 0
    assert (tmp_path / "b" / "asago_suite" / "suite.yaml").is_file()


@pytest.mark.parametrize("content", [None, "{not json", json.dumps({"model": "m"})])
def test_instantiate_with_unusable_values_exits_one(
    template: Path, tmp_path: Path, content: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    values = tmp_path / "values.json"
    if content is not None:
        values.write_text(content, encoding="utf-8")

    code = main(
        [
            "instantiate",
            str(template),
            "--values",
            str(values),
            "--target",
            str(TARGET_CONFIG),
            "--out",
            str(tmp_path / "b"),
        ]
    )

    assert code == 1
    assert capsys.readouterr().err.startswith("instantiate: ")


@pytest.mark.parametrize(
    "values_text",
    [
        pytest.param(json.dumps(VALUES), id="usable-values"),
        # The target config is checked before the values are read.
        pytest.param("{not json", id="unreadable-values"),
    ],
)
def test_instantiate_refuses_a_target_not_authorized_for_a_run(
    template: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], values_text: str
) -> None:
    values = tmp_path / "values.json"
    values.write_text(values_text, encoding="utf-8")
    target = tmp_path / "target.yaml"
    target.write_text("target: production\n", encoding="utf-8")

    code = main(
        [
            "instantiate",
            str(template),
            "--values",
            str(values),
            "--target",
            str(target),
            "--out",
            str(tmp_path / "b"),
        ]
    )

    assert code == 1
    assert "authorized_as" in capsys.readouterr().err
    assert not (tmp_path / "b").exists()


def _refuse(*_: Any, **__: Any) -> Any:
    raise AssertionError("the legacy form read a target config or prepared the package")


@pytest.fixture
def no_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything reads a target config, prepares a package or calls a target."""

    for module, name in [
        ("asago_bundle_core.target", "load_target_config"),
        ("asago_bundle_core.target", "authorized_target"),
        ("asago_bundle_core.target", "mcp_call_target"),
        ("asago_bundle_core.preparation", "prepare_package"),
        ("asago_bundle_midojo.cli", "authorized_target"),
        ("asago_bundle_midojo.instantiate", "authorized_target"),
    ]:
        monkeypatch.setattr(importlib.import_module(module), name, _refuse)


def files(bundle: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(bundle)): path.read_bytes() if path.is_file() else b""
        for path in sorted(bundle.rglob("*"))
    }


def test_the_legacy_form_without_target_fills_the_template_and_touches_no_target(
    template: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    no_target: None,
) -> None:
    values = tmp_path / "values.json"
    values.write_text(json.dumps(VALUES), encoding="utf-8")
    bundle = tmp_path / "b"

    code = main(["instantiate", str(template), "--values", str(values), "--out", str(bundle)])

    assert code == 0
    assert capsys.readouterr() == ("", "")
    legacy = files(bundle)
    bundle.rename(tmp_path / "legacy")
    monkeypatch.undo()
    # The same template and values make the same bundle as with an authorized target.
    instantiate_bundle(template, VALUES, bundle, target=MOCK_TARGET)
    assert legacy == files(bundle)
    assert "asago_suite/suite.yaml" in legacy


@pytest.mark.parametrize("content", [None, "{not json", json.dumps({"model": "m"})])
def test_the_legacy_form_with_unusable_values_exits_one(
    template: Path,
    tmp_path: Path,
    content: str | None,
    capsys: pytest.CaptureFixture[str],
    no_target: None,
) -> None:
    values = tmp_path / "values.json"
    if content is not None:
        values.write_text(content, encoding="utf-8")

    code = main(
        ["instantiate", str(template), "--values", str(values), "--out", str(tmp_path / "b")]
    )

    assert code == 1
    assert capsys.readouterr().err.startswith("instantiate: ")
    assert not (tmp_path / "b").exists()


@pytest.mark.parametrize("extra", [[], ["--target", str(TARGET_CONFIG)]])
def test_instantiate_without_values_is_a_usage_error(
    template: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["instantiate", str(template), "--out", str(tmp_path / "b"), *extra])

    assert raised.value.code == 2
    assert "--values" in capsys.readouterr().err
    assert not (tmp_path / "b").exists()


def test_parse_writes_the_receipt_and_exits_zero(bundle: Path) -> None:
    write_native(bundle)

    code = main(
        [
            "parse",
            str(bundle),
            "--out",
            str(bundle / "receipt.json"),
            "--tool-revision",
            "9ceb22e3",
            "--adapter-revision",
            "abc1234",
        ]
    )

    receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    assert code == 0
    assert receipt["tool"] == {
        "id": "midojo",
        "revision": "9ceb22e3",
        "adapter_revision": "abc1234",
    }


def test_parse_of_a_run_without_records_still_exits_zero(bundle: Path) -> None:
    code = main(["parse", str(bundle), "--out", str(bundle / "receipt.json")])

    receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    assert code == 0
    assert receipt["execution_status"] == "failed"


def test_parse_takes_the_boundary_records(bundle: Path, tmp_path: Path) -> None:
    write_native(bundle)
    records = tmp_path / "records"
    (records / "mcp_capture").mkdir(parents=True)
    (records / "mcp_capture" / "calls.jsonl").write_text("", encoding="utf-8")

    code = main(
        ["parse", str(bundle), "--out", str(bundle / "receipt.json"), "--records", str(records)]
    )

    receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    assert code == 0
    assert receipt["incomplete_reason"] == "tool_call_count_mismatch"


def test_parse_of_an_unreadable_bundle_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["parse", str(tmp_path), "--out", str(tmp_path / "receipt.json")])

    assert code == 1
    assert capsys.readouterr().err.startswith("parse: ")


def case_directory(base: Path) -> Path:
    """Return ``base/given``, a symlink to ``base/real``, as the directory a case runs in.

    The adapters write some paths as given and some resolved, and the two get different
    tokens, so their spellings must differ on every system: on Linux a temporary directory
    is often its own resolved path. A run where they coincide, or one holds the other, fails.
    """

    (base / "real").mkdir()
    given = base / "given"
    given.symlink_to(base / "real", target_is_directory=True)
    spelled, resolved = str(given), str(given.resolve())
    if spelled in resolved or resolved in spelled:
        raise AssertionError(f"the as-given and resolved paths must differ: {spelled}, {resolved}")
    return given


def spellings(tmp: Path) -> list[tuple[str, str]]:
    """Return each spelling of ``tmp`` with its token, the longer spelling first."""

    pairs = [(str(tmp.resolve()), "{tmp_resolved}"), (str(tmp), "{tmp}")]
    return sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)


def normalise(text: str, tmp: Path) -> str:
    for spelling, token in spellings(tmp):
        text = text.replace(spelling, token)
    return text


def normalise_bytes(data: bytes, tmp: Path) -> bytes:
    """Write the case directory's spellings as their tokens in a file's bytes."""

    for spelling, token in spellings(tmp):
        data = data.replace(spelling.encode(), token.encode())
    return data


def digests(bundle: Path, tmp: Path) -> dict[str, str]:
    return {
        path.relative_to(bundle).as_posix(): (
            hashlib.sha256(normalise_bytes(path.read_bytes(), tmp)).hexdigest()
            if path.is_file()
            else "dir"
        )
        for path in sorted(bundle.rglob("*"))
    }


@pytest.mark.parametrize(
    "case", LEGACY_CASES, ids=[f"{case['template']}-{case['case']}" for case in LEGACY_CASES]
)
def test_the_legacy_form_does_what_it_did_at_c59a8fb(
    case: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str], no_target: None
) -> None:
    tmp = case_directory(tmp_path)
    # A template is its own files plus the suite code every template copies.
    template = tmp / "template"
    shutil.copytree(LEGACY / "templates" / case["template"], template)
    shutil.copytree(LEGACY / "templates" / "code", template, dirs_exist_ok=True)
    if case["values"] is not None:
        (tmp / "values.json").write_text(case["values"], encoding="utf-8")

    code = main(
        [
            "instantiate",
            str(template),
            "--values",
            str(tmp / "values.json"),
            "--out",
            str(tmp / "bundle"),
        ]
    )

    out, err = capsys.readouterr()
    assert (code, normalise(out, tmp), normalise(err, tmp)) == (
        case["exit"],
        case["stdout"],
        case["stderr"],
    )
    assert digests(tmp / "bundle", tmp) == case["files"]
