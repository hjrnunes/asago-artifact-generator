"""Collect every terminal reason the authoring code can emit, by reading its source.

A terminal reason is the code of the last terminal finding or, without one, the
run status (``AuthoringOrchestrator._finish``). This module walks the package's
syntax trees and resolves the code argument of every ``Finding(...)`` and the
status argument of every stage stop and result to string literals. A site it
cannot resolve is returned as unresolved, so a new code path fails the
coverage test instead of slipping past it.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "asago_artifact_generator"
SKIPPED_PARTS = frozenset({"report", "report_kit"})

# (callee name, positional index, keyword) of each argument that becomes a reason.
REASON_ARGUMENTS: tuple[tuple[str, int, str], ...] = (
    ("Finding", 0, "code"),
    ("_StageStop", 0, "status"),
    ("_result", 0, "status"),
)
REASON_KEYWORDS = frozenset({"review_blocked_status"})
# Attributes that carry a reason produced at another resolved site.
PASSTHROUGH_ATTRIBUTES = frozenset({"status", "code", "review_blocked_status"})


def _error_pattern_codes() -> list[str]:
    from asago_artifact_generator.authoring import core

    return [code for _pattern, code in core._ERROR_CODE_PATTERNS]


# Sites that pick a code from a runtime table with ``next(..., default)``:
# (module, enclosing function, callee) -> the table's codes. The default argument
# is resolved from source.
RUNTIME_SITES = {
    ("authoring/core.py", "_findings_from_error", "next"): _error_pattern_codes,
}


@dataclass
class Collected:
    reasons: dict[str, set[str]] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)

    def add(self, value: str, where: str) -> None:
        self.reasons.setdefault(value, set()).add(where)


class _Module:
    def __init__(self, name: str, tree: ast.Module) -> None:
        self.name = name
        self.tree = tree
        self.parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        self.constants = {
            node.targets[0].id: node.value
            for node in self.tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        }
        self.functions = {
            node.name: node
            for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.imports: dict[str, tuple[str, str]] = {}
        for node in self.tree.body:
            if isinstance(node, ast.ImportFrom) and node.level:
                base = Path(self.name).parent
                for _ in range(node.level - 1):
                    base = base.parent
                target = (base / (node.module or "").replace(".", "/")).as_posix() + ".py"
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (target.lstrip("./"), alias.name)

    def enclosing(self, node: ast.AST, kinds: tuple[type, ...]) -> ast.AST | None:
        current = self.parents.get(node)
        while current is not None and not isinstance(current, kinds):
            current = self.parents.get(current)
        return current


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _argument(call: ast.Call, index: int, keyword: str) -> ast.expr | None:
    for item in call.keywords:
        if item.arg == keyword:
            return item.value
    if len(call.args) > index and not isinstance(call.args[index], ast.Starred):
        return call.args[index]
    return None


class _Resolver:
    def __init__(self, modules: dict[str, _Module]) -> None:
        self.modules = modules

    def values(self, module: _Module, node: ast.expr, depth: int = 0) -> list[str] | None:
        """Return the literals ``node`` can take, or None when it cannot tell."""

        if depth > 6:
            return None
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) else None
        if isinstance(node, ast.IfExp):
            return self._all(module, [node.body, node.orelse], depth)
        if isinstance(node, ast.BoolOp):
            return self._all(module, node.values, depth)
        if isinstance(node, ast.Attribute) and node.attr in PASSTHROUGH_ATTRIBUTES:
            return []
        if isinstance(node, ast.Name):
            return self._name(module, node, depth)
        if isinstance(node, ast.Call):
            return self._call(module, node, depth)
        return None

    def _call(self, module: _Module, call: ast.Call, depth: int) -> list[str] | None:
        function = module.enclosing(call, (ast.FunctionDef,))
        known = RUNTIME_SITES.get(
            (module.name, getattr(function, "name", ""), ast.unparse(call.func))
        )
        if known is not None:
            default = self.values(module, call.args[-1], depth + 1)
            return None if default is None else [*known(), *default]
        return self._returns(module, call, depth)

    def _all(self, module: _Module, nodes: list[ast.expr], depth: int) -> list[str] | None:
        found: list[str] = []
        for item in nodes:
            values = self.values(module, item, depth + 1)
            if values is None:
                return None
            found += values
        return found

    def _name(self, module: _Module, node: ast.Name, depth: int) -> list[str] | None:
        function = module.enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if function is not None:
            loop = self._loop_values(module, node, depth)
            if loop is not None:
                return loop
            parameters = [arg.arg for arg in function.args.args + function.args.kwonlyargs]
            if node.id in parameters:
                return self._parameter(module, function, node.id, depth)
            local = self._local(module, function, node.id, depth)
            if local is not None:
                return local
        if node.id in module.constants:
            return self.values(module, module.constants[node.id], depth + 1)
        if node.id in module.imports:
            target, name = module.imports[node.id]
            other = self.modules.get(target)
            if other is not None and name in other.constants:
                return self.values(other, other.constants[name], depth + 1)
        return None

    def _loop_values(self, module: _Module, node: ast.Name, depth: int) -> list[str] | None:
        loop = module.enclosing(node, (ast.For,))
        if (
            isinstance(loop, ast.For)
            and isinstance(loop.target, ast.Name)
            and loop.target.id == node.id
            and isinstance(loop.iter, (ast.Tuple, ast.List))
        ):
            return self._all(module, list(loop.iter.elts), depth)
        return None

    def _local(
        self, module: _Module, function: ast.AST, name: str, depth: int
    ) -> list[str] | None:
        assigned = [
            item.value
            for item in ast.walk(function)
            if isinstance(item, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in item.targets)
        ]
        return self._all(module, assigned, depth) if assigned else None

    def _parameter(
        self, module: _Module, function: ast.AST, name: str, depth: int
    ) -> list[str] | None:
        assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        positional = [arg.arg for arg in function.args.args]
        offset = 1 if positional and positional[0] in {"self", "cls"} else 0
        index = positional.index(name) - offset if name in positional else -1
        found: list[str] = []
        with_defaults = positional[len(positional) - len(function.args.defaults) :]
        defaults: dict[str, ast.expr] = dict(
            zip(with_defaults, function.args.defaults, strict=True)
        )
        defaults.update(
            {
                arg.arg: default
                for arg, default in zip(
                    function.args.kwonlyargs, function.args.kw_defaults, strict=True
                )
                if default is not None
            }
        )
        if name in defaults:
            values = self.values(module, defaults[name], depth + 1)
            if values is None:
                return None
            found += values
        calls = [
            call
            for other in self.modules.values()
            for call in ast.walk(other.tree)
            if isinstance(call, ast.Call) and _callee(call) == function.name
        ]
        for call in calls:
            owner = next(m for m in self.modules.values() if call in m.parents)
            argument = _argument(call, index if index >= 0 else len(call.args), name)
            if argument is None:
                if name in defaults:
                    continue
                return None
            values = self.values(owner, argument, depth + 1)
            if values is None:
                return None
            found += values
        return found

    def _returns(self, module: _Module, call: ast.Call, depth: int) -> list[str] | None:
        name = _callee(call)
        function = module.functions.get(name or "")
        if function is None:
            return None
        returned = [
            item.value
            for item in ast.walk(function)
            if isinstance(item, ast.Return) and item.value is not None
        ]
        return self._all(module, returned, depth) if returned else None


def _sites(module: _Module) -> Iterator[tuple[ast.expr, int]]:
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node)
        for name, index, keyword in REASON_ARGUMENTS:
            if callee == name:
                argument = _argument(node, index, keyword)
                if argument is not None:
                    yield argument, node.lineno
        for item in node.keywords:
            if item.arg in REASON_KEYWORDS:
                yield item.value, node.lineno


def _status_assignments(module: _Module) -> Iterator[tuple[ast.expr, int]]:
    """Yield literal assignments to a variable named ``status`` (status overrides)."""

    for node in ast.walk(module.tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "status"
            and isinstance(node.value, ast.Constant)
        ):
            yield node.value, node.lineno


@functools.cache
def collect_reasons(root: Path = PACKAGE) -> Collected:
    """Return every reason literal the package emits and each site left unresolved.

    Callers share one result per root, so they must not modify it.
    """

    paths = [
        path
        for path in sorted(root.rglob("*.py"))
        if not SKIPPED_PARTS & set(path.relative_to(root).parts)
    ]
    trees = {path.relative_to(root).as_posix(): ast.parse(path.read_text()) for path in paths}
    modules = {name: _Module(name, tree) for name, tree in trees.items()}
    resolver = _Resolver(modules)
    collected = Collected()
    for module in modules.values():
        for argument, line in [*_sites(module), *_status_assignments(module)]:
            where = f"{module.name}:{line}"
            values = resolver.values(module, argument)
            if values is None:
                collected.unresolved.append(where)
                continue
            for value in values:
                collected.add(value, where)
    return collected
