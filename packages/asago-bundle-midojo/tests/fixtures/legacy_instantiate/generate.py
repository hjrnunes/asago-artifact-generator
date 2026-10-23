"""Record what ``instantiate TEMPLATE --values VALUES --out OUT`` did at c59a8fb.

usage: C=<checkout of c59a8fb>
       PYTHONPATH=$C/packages/asago-bundle-core/src:$C/packages/asago-bundle-midojo/src:$C/src \
         uv run python generate.py "$C"

Everything runs with that checkout's code, which the script checks: its test package
builders write a command, a reply and a sequential package, its compiler writes each
template (for the ``klarna`` domain of the checkout's ``tests/fixtures/midojo-base.json``)
into ``templates/<kind>/``, with the suite code every template copies stored once in
``templates/code/``, and its command line fills each template with good
values and with unusable ones. ``cases.json`` holds, per case, the values file's text
(none: the file is absent), the exit code, stdout, stderr and the SHA-256 of every file of
the bundle (``dir`` for a directory). The template, the values and the bundle sit in one
temporary directory (left in place), written ``{tmp}`` in the recorded output and file
contents.
``test_bundle_midojo_cli.py`` replays every case with the current code. To regenerate,
make the checkout with ``git worktree add --detach "$(mktemp -d)/c59a8fb" c59a8fb``.

pytest does not collect this file (its name has no ``test_`` prefix).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REVISION = "c59a8fb"
HERE = Path(__file__).resolve().parent
MODULES = ("asago_bundle_core", "asago_bundle_midojo", "asago_artifact_generator")
DOMAIN = "klarna"
VALUES: dict[str, Any] = {
    "gateway_url": "http://127.0.0.1:18997/v1",
    "mcp_url": "http://127.0.0.1:18996/sse",
    "judge_url": "http://127.0.0.1:18998/v1",
    "model": "fixture-model",
    "judge_model": "fixture-judge",
    "messages": [{"role": "user", "content": "Show the referral for PAT-201."}],
    "judge_runtime_facts": {"state": {"actor": "GST001"}},
    "judge_turns": "final",
    "service_port": 18123,
    "user_turn_1": "Hello.",
    "user_turn_2": "Show PAT-201.",
}


def value_cases(requires: list[str]) -> dict[str, str | None]:
    """Return each case's values file text: good values, then unusable ones."""

    good = {key: VALUES[key] for key in requires}
    return {
        "good": json.dumps(good, indent=2),
        "lacks-model": json.dumps({k: v for k, v in good.items() if k != "model"}),
        "malformed-gateway-url": json.dumps({**good, "gateway_url": "ftp://gateway"}),
        "unreadable": "{not json",
        "absent": None,
    }


def normalise(text: str, tmp: Path) -> str:
    return text.replace(str(tmp.resolve()), "{tmp}").replace(str(tmp), "{tmp}")


def digests(bundle: Path, tmp: Path) -> dict[str, str]:
    return {
        path.relative_to(bundle).as_posix(): (
            hashlib.sha256(normalise(path.read_text("utf-8"), tmp).encode()).hexdigest()
            if path.is_file()
            else "dir"
        )
        for path in sorted(bundle.rglob("*"))
    }


def store_template(template: Path, kind: str) -> None:
    """Keep a compiled template as ``templates/<kind>/`` and its code as ``templates/code/``.

    Every template copies the same suite code, so it is stored once; a kind whose code
    differs is refused.
    """

    for path in sorted(p for p in template.rglob("*") if p.is_file()):
        name = path.relative_to(template)
        store = HERE / "templates" / ("code" if path.suffix == ".py" else kind) / name
        if path.suffix == ".py" and store.exists():
            assert store.read_bytes() == path.read_bytes(), name
        store.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, store)


def assemble_template(kind: str, template: Path) -> None:
    """Write the template of ``kind`` from its own files and the shared code."""

    shutil.copytree(HERE / "templates" / kind, template)
    shutil.copytree(HERE / "templates" / "code", template, dirs_exist_ok=True)


def main(checkout: Path) -> None:
    for name in MODULES:
        module = __import__(name)
        assert Path(module.__file__).resolve().is_relative_to(checkout.resolve()), module
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert head.startswith(REVISION), head
    from asago_bundle_core.testing import sequential_stimulus, write_test_package
    from asago_bundle_midojo.cli import main as cli
    from asago_bundle_midojo.compiler import compile_package

    packages = {
        "command": {},
        "reply": {"claim_level": "reply"},
        "sequential": {"stimulus": sequential_stimulus()},
    }
    base = (
        checkout / "packages" / "asago-bundle-midojo" / "tests" / "fixtures" / "midojo-base.json"
    )
    cases = []
    for kind, options in packages.items():
        scratch = Path(tempfile.mkdtemp())
        package = write_test_package(scratch / "packages", **options)
        compile_package(package, scratch / "t", midojo_base=base, domain=DOMAIN)
        store_template(scratch / "t", kind)
        requires = json.loads((HERE / "templates" / kind / "bundle.json").read_text("utf-8"))
        for case, text in value_cases(requires["requires"]).items():
            tmp = Path(tempfile.mkdtemp())
            assemble_template(kind, tmp / "template")
            if text is not None:
                (tmp / "values.json").write_text(text, encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            argv = ["instantiate", str(tmp / "template"), "--values"]
            argv += [str(tmp / "values.json"), "--out", str(tmp / "bundle")]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli(argv)
            cases.append(
                {
                    "template": kind,
                    "case": case,
                    "values": text,
                    "exit": code,
                    "stdout": normalise(out.getvalue(), tmp),
                    "stderr": normalise(err.getvalue(), tmp),
                    "files": digests(tmp / "bundle", tmp),
                }
            )
    record = {"revision": head, "cases": cases}
    (HERE / "cases.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
