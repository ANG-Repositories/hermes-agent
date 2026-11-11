#!/usr/bin/env python3
"""Fail when a GitHub worktree card can be kept on the Studio for a reason nobody enumerated (t_20fa16c7).

Ace 2026-11-10 12:24: "All machines can make any checkout always ... harden and lint it so this can never
happen again." Every reason ``hermes_cli/kanban_worktree_port.py`` may give for keeping a worktree card
local is a ``why`` it appends (``_why(why, X)``) and the dispatcher reports as
``not_portable:workspace_kind:<X>``. This lint fails when:

* a ``_why(...)`` in the port module names a reason outside :data:`ENUMERATED` (a string literal, or a
  ``WHY_*`` constant whose value is not listed), or any module-level ``WHY_*`` constant is not listed;
* the module's ``LOCAL_ONLY_REASONS`` keys differ from :data:`ENUMERATED` (each needs a written
  justification there);
* ``hermes_cli/kanban_db_dispatch.py`` builds a ``not_portable:workspace_kind:`` reason from anything but
  the port's ``wt_why`` (a fixed string naming a repo list or a checkout location).

Adding a reason means editing BOTH the module and :data:`ENUMERATED` below, so it shows in review.

Usage: python3 scripts/check_worktree_local_reasons.py [port_module.py [dispatch_module.py]]
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = ROOT / "hermes_cli" / "kanban_worktree_port.py"
DISPATCH = ROOT / "hermes_cli" / "kanban_db_dispatch.py"

# The enumerated set. Each value's justification lives in LOCAL_ONLY_REASONS in the port module.
ENUMERATED = frozenset({
    "no_pool_host", "origin_not_ours", "origin_not_github", "not_a_repo", "diverged_from_origin",
    "operator_pin", "concrete_target_path", "own_tree_not_a_checkout", "own_tree_off_branch",
    # the dirty-tree carry's own refusals (t_894be273)
    "dirty_submodule_or_nested_repo", "dirty_tree_over_cap", "origin_tip_not_studio_head",
    "dirty_path_through_symlink", "dirty_path_not_plain_file",
    # kill-switch / hand-list only (derived: false, KANBAN_DIRTY_CARRY=0)
    "unpushed_dirty_tree", "unpushed_commits", "derived_mode_off",
})
# The one sanctioned fixed string (a host: pin the port does not serve); it names no repo list.
DISPATCH_FIXED_OK = re.compile(r"^not_portable:workspace_kind:worktree card not portable to the pinned host")


def _constants(tree: ast.Module) -> dict:
    out = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            out[node.targets[0].id] = node.value.value
    return out


def _resolve(node: ast.AST, consts: dict):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def check_port(path: Path) -> list:
    src = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(src, str(path))
    consts = _constants(tree)
    errors = []
    # Every WHY_* constant is a reason some path can hand to _why (directly, or returned by a helper such as
    # dirty_census and passed on through a variable), so each one must be enumerated.
    for name, val in sorted(consts.items()):
        if name.startswith("WHY_") and val not in ENUMERATED:
            errors.append(f"{path.name}: {name} = {val!r} is not enumerated (add it to LOCAL_ONLY_REASONS with a "
                          "justification AND to scripts/check_worktree_local_reasons.py ENUMERATED)")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_why":
            if len(node.args) < 2:
                continue
            arg = node.args[1]
            names = [arg]
            if isinstance(arg, ast.IfExp):
                names = [arg.body, arg.orelse]
            for n in names:
                val = _resolve(n, consts)
                if val is None and isinstance(n, ast.Name):
                    continue  # a loop variable over checked tuples (resolved below via the tuple literals)
                if val not in ENUMERATED:
                    errors.append(f"{path.name}:{node.lineno}: local-only reason {val or ast.unparse(n)!r} is not "
                                  "enumerated (add it to LOCAL_ONLY_REASONS with a justification AND to "
                                  "scripts/check_worktree_local_reasons.py ENUMERATED)")
        if isinstance(node, ast.Tuple):
            # (base, WHY_X) pairs fed to _why through a loop variable
            for elt in node.elts:
                if isinstance(elt, ast.Name) and elt.id.startswith("WHY_"):
                    val = consts.get(elt.id)
                    if val is not None and val not in ENUMERATED:
                        errors.append(f"{path.name}:{node.lineno}: reason {val!r} ({elt.id}) is not enumerated")
    table = None
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
        if isinstance(target, ast.Name) and target.id == "LOCAL_ONLY_REASONS" and isinstance(node.value, ast.Dict):
            table = {}
            for k, v in zip(node.value.keys, node.value.values):
                key = _resolve(k, consts)
                just = v.value if isinstance(v, ast.Constant) else ast.unparse(v)
                table[key] = just
    if table is None:
        errors.append(f"{path.name}: LOCAL_ONLY_REASONS table missing")
    else:
        for key, just in table.items():
            if not (isinstance(just, str) and len(just.strip()) >= 20):
                errors.append(f"{path.name}: LOCAL_ONLY_REASONS[{key!r}] needs a written justification")
        if set(table) != set(ENUMERATED):
            errors.append(f"{path.name}: LOCAL_ONLY_REASONS keys differ from the lint's ENUMERATED: "
                          f"only in module {sorted(set(table) - ENUMERATED)}, "
                          f"only in lint {sorted(ENUMERATED - set(table))}")
    return errors


def check_dispatch(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), str(path))
    errors = []
    in_fstring = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in in_fstring \
                and node.value.startswith("not_portable:workspace_kind:"):
            if not DISPATCH_FIXED_OK.match(node.value):
                errors.append(f"{path.name}:{node.lineno}: fixed workspace_kind reason {node.value[:80]!r}: "
                              "a worktree card's local reason must come from the port's wt_why")
        if isinstance(node, ast.JoinedStr):
            head = node.values[0] if node.values else None
            if isinstance(head, ast.Constant) and str(head.value).startswith("not_portable:workspace_kind:"):
                rest = node.values[1:]
                ok = (len(rest) == 1 and isinstance(rest[0], ast.FormattedValue)
                      and ast.unparse(rest[0].value) == "wt_why[0]")
                if not ok:
                    errors.append(f"{path.name}:{node.lineno}: workspace_kind reason built from "
                                  f"{ast.unparse(node)[:80]}, not wt_why[0]")
    return errors


def main(argv: list) -> int:
    port = Path(argv[0]) if argv else PORT
    dispatch = Path(argv[1]) if len(argv) > 1 else DISPATCH
    errors = check_port(port) + (check_dispatch(dispatch) if dispatch.exists() else [])
    for e in errors:
        print(f"worktree-local-reasons: {e}", file=sys.stderr)
    if not errors:
        print(f"worktree-local-reasons: ok ({len(ENUMERATED)} enumerated reasons)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
