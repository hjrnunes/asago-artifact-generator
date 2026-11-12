#!/usr/bin/env python3
"""Write or check the lock of the MiDojo vector copies.

    python3 scripts/midojo_lock.py          # rewrite the lock
    python3 scripts/midojo_lock.py --check  # change nothing; exit 1 and name each difference

The lock names the MiDojo revision that ``pyproject.toml`` pins ``midojo-spec`` to, and
the SHA-256 of every byte copy of MiDojo's injection-spec vectors under
``packages/asago-injection-spec/tests/fixtures/midojo/``. A repin changes the revision in
``pyproject.toml``, copies the vectors from that revision and runs this script once.

The script needs only the standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path("packages/asago-injection-spec/tests/fixtures")
LOCK = FIXTURES / "midojo.lock"
VECTORS = FIXTURES / "midojo"


def pinned_revision(root: Path) -> str:
    """The revision ``pyproject.toml`` pins ``midojo-spec`` to."""

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(pyproject["tool"]["uv"]["sources"]["midojo-spec"]["rev"])


def build_lock(root: Path) -> str:
    """The lock text the pin and the copies on disk call for."""

    vectors = root / VECTORS
    files = {
        path.relative_to(vectors).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(vectors.rglob("*"))
        if path.is_file()
    }
    document = {"source": f"midojo local {pinned_revision(root)}", "files": files}
    return json.dumps(document, indent=2) + "\n"


def differences(root: Path) -> list[str]:
    """Name every way the committed lock differs from the one the pin and copies call for."""

    wanted = json.loads(build_lock(root))
    lock = root / LOCK
    if not lock.is_file():
        return [f"{LOCK} is missing"]
    held = json.loads(lock.read_text(encoding="utf-8"))
    found: list[str] = []
    if held.get("source") != wanted["source"]:
        found.append(f"source is {held.get('source')!r}, the pin calls for {wanted['source']!r}")
    held_files, wanted_files = held.get("files", {}), wanted["files"]
    for name in sorted(held_files.keys() | wanted_files.keys()):
        if name not in held_files:
            found.append(f"{name} is a copy the lock does not list")
        elif name not in wanted_files:
            found.append(f"{name} is listed but no copy is on disk")
        elif held_files[name] != wanted_files[name]:
            found.append(f"{name} differs from the digest the lock records")
    if not found and lock.read_text(encoding="utf-8") != build_lock(root):
        found.append(f"{LOCK} is not in the form this script writes")
    return found


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="change nothing; exit 1 on a difference"
    )
    arguments = parser.parse_args(argv)
    found = differences(root)
    if arguments.check:
        for line in found:
            print(f"- {line}")
        print(f"{LOCK}: {'DIFFERS' if found else 'verified'}")
        return 1 if found else 0
    (root / LOCK).write_text(build_lock(root), encoding="utf-8")
    print(f"{LOCK}: written ({pinned_revision(root)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
