"""No shipped module may call ``get_version_info()`` at import (t_a1bd70f8).

In an unstamped git checkout ``get_version_info()`` runs ~8 git subprocesses (``describe``,
``status``, ``tag --merged`` ...). The result is cached per process, so whichever module calls it
first at import makes every fresh process that imports it pay: on the 4-vCPU CI guest ~1.8 s per
child, which kept the clean-graph import smoke (e2e-upgrade) red. Fixing one call site only moved
the cost to the next one, so this pins all of them. Resolve on use instead: a function, or
``version_info.LazyHeaders`` for a header dict.

This reads source, so it holds in trees with no ``.git`` (where the git calls never run and a
spawn-counting probe would pass vacuously).
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _shipped_files() -> list[Path]:
    include = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"]["setuptools"]["packages"]["find"]["include"]
    tops = sorted({name.split(".")[0] for name in include})
    files = [p for p in REPO.glob("*.py") if p.name != "setup.py"]
    for top in tops:
        files += (REPO / top).rglob("*.py")
    return sorted(p for p in files
                  if "tests" not in p.relative_to(REPO).parts
                  and not p.name.startswith("test_") and p.name != "conftest.py")


def _calls_name(node: ast.AST, names: set[str]) -> bool:
    func = node.func if isinstance(node, ast.Call) else None
    return (isinstance(func, ast.Name) and func.id in names) or (
        isinstance(func, ast.Attribute) and func.attr in names)


def import_time_version_calls(source: str) -> list[int]:
    """Line numbers of ``get_version_info()`` calls that run when the module is imported:
    module/class bodies, decorators and default arguments, plus module-level calls to a
    module-level function whose own body calls ``get_version_info()`` directly."""
    tree = ast.parse(source)
    resolvers = {"get_version_info"}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                _calls_name(n, {"get_version_info"}) for n in ast.walk(node)):
            resolvers.add(node.name)
    hits: list[int] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Only what runs at definition time: decorators and default values.
            for dec in getattr(node, "decorator_list", ()):
                visit(dec)
            for default in [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]:
                visit(default)
            return
        if _calls_name(node, resolvers):
            hits.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return hits


def test_detector_flags_import_time_calls_and_ignores_resolve_on_use():
    flagged = '''
from hermes_cli.version_info import get_version_info
UA = f"x/{get_version_info().base_version}"
class C:
    V = get_version_info()
def helper():
    return get_version_info().base_version
HELPER_UA = helper()
def f(v=get_version_info()):
    return v
'''
    assert import_time_version_calls(flagged) == [3, 5, 8, 9]
    lazy = '''
from hermes_cli.version_info import LazyHeaders, get_version_info
def ua():
    return f"x/{get_version_info().base_version}"
HEADERS = LazyHeaders(lambda: {"User-Agent": ua()})
class C:
    def m(self):
        return get_version_info()
'''
    assert import_time_version_calls(lazy) == []


def test_no_shipped_module_resolves_the_version_at_import():
    files = _shipped_files()
    assert len(files) > 500, f"enumerated only {len(files)} shipped files; the scan is not looking at the tree"
    offenders = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        if "get_version_info" in source:
            offenders += [f"{path.relative_to(REPO)}:{line}" for line in import_time_version_calls(source)]
    assert not offenders, (
        "get_version_info() runs ~8 git subprocesses in a checkout, so calling it at import makes every "
        f"fresh process pay them; resolve on use (a function, or version_info.LazyHeaders): {offenders}")


def test_lazy_headers_resolve_once_on_first_read():
    from hermes_cli.version_info import LazyHeaders

    built = []
    headers = LazyHeaders(lambda: built.append(1) or {"User-Agent": "x/1", "X-Title": "T"})
    assert built == []
    assert dict(headers) == {"User-Agent": "x/1", "X-Title": "T"}
    assert headers["User-Agent"] == "x/1" and len(headers) == 2 and {**headers}["X-Title"] == "T"
    assert headers == {"User-Agent": "x/1", "X-Title": "T"}
    assert built == [1]
