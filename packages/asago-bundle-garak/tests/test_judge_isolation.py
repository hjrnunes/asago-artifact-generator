"""The judge imports no Garak or MiDojo harness, even through its shared adapter rules."""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGES = Path(__file__).resolve().parents[2]
_SOURCES = {
    name: _PACKAGES / name.replace("_", "-") / "src" / name
    for name in ("asago_bundle_garak", "asago_injection_spec")
}


def _module_path(module: str) -> Path | None:
    root, _, rest = module.partition(".")
    if root not in _SOURCES:
        return None
    base = _SOURCES[root]
    child = base.joinpath(*rest.split(".")) if rest else base
    path = child.with_suffix(".py")
    if path.is_file():
        return path
    package = child / "__init__.py"
    return package if package.is_file() else None


def _imports(path: Path, module: str) -> set[str]:
    dependencies: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            dependencies.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = module.split(".")
                base = parts[: len(parts) - node.level]
                name = ".".join([*base, *([node.module] if node.module else [])])
            else:
                name = node.module or ""
            dependencies.add(name)
            dependencies.update(
                child
                for alias in node.names
                if (child := f"{name}.{alias.name}") and _module_path(child) is not None
            )
    return dependencies


def test_the_judge_and_every_reachable_shared_module_avoid_garak_and_midojo() -> None:
    pending = ["asago_bundle_garak.judge_service"]
    visited: set[str] = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        path = _module_path(module)
        assert path is not None
        for dependency in _imports(path, module):
            assert dependency.split(".")[0] not in {"garak", "midojo"}, (path, dependency)
            # asago_injection_spec still uses midojo_sdk for its schema and snapshot.
            # Only direct imports by the judge or its own package are forbidden.
            if module.startswith("asago_bundle_garak"):
                assert dependency.split(".")[0] != "midojo_sdk", (path, dependency)
            if _module_path(dependency) is not None:
                pending.append(dependency)


def test_the_garak_adapter_has_no_direct_sdk_or_harness_import() -> None:
    for path in _SOURCES["asago_bundle_garak"].rglob("*.py"):
        for dependency in _imports(path, "asago_bundle_garak." + path.stem):
            assert dependency.split(".")[0] not in {"garak", "midojo", "midojo_sdk"}, (
                path,
                dependency,
            )
