#!/usr/bin/env python3
# SCHEDULE: manual (operator runs the audit/drain; --check is the sweep a cron row in hermes-home calls)
"""Half-built ``git worktree add`` leftovers in a fleet checkout: attribute, sweep, drain (t_f609a5a0).

A killed ``git worktree add`` leaves its tree registered and locked with git's own reason ``initializing``
(no index, a partial checkout). On 2026-11-11 the Studio home tree held 183 of them.

Modes (all read-only except ``--drain --execute``):

  audit   (default) one row per ``locked initializing`` registration: path, path shape, branch, the card that
          path names and its status, lock age, whether the branch has commits no remote-tracking ref has, and
          the drain verdict. ``--json`` for machine output.
  --check the sweep: exit 1 and one ``finding:`` line per ``locked initializing`` registration older than
          ``--min-age-h`` (default 1 h); silent, exit 0, when clean.
  --drain prints what the drain WOULD remove (dry run). ``--execute`` removes them.

The drain removes a tree only when every one of these holds (anything else is listed, never touched):
  * the lock reason is exactly ``initializing`` and the admin dir has no index;
  * the path is a dispatcher spawn: ``<repo>/.worktrees/t_<8 hex>``, and that card exists on the board;
  * the card is ``done`` or ``archived``, or it never spawned a worker (no ``spawned`` event);
  * the tree's branch is the card's (``wt/<id>`` or its ``branch_name``) or HEAD is detached;
  * the lock is older than ``--min-age-h``.
The branch is deleted only when it has no commit that a remote-tracking ref lacks; else it is kept and named.
Removal is ``git worktree remove -f -f <path>``, else the ONE admin dir whose ``gitdir`` names that path. Never a
repo-wide ``worktree prune``.

Usage:
  kanban_worktree_residue_audit.py --repo ~/.hermes [--db ~/.hermes/kanban.db] [--json]
  kanban_worktree_residue_audit.py --repo ~/.hermes --check [--min-age-h 1]
  kanban_worktree_residue_audit.py --repo ~/.hermes --db ~/.hermes/kanban.db --drain [--execute] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_cli import kanban_worktree_residue as kwr  # noqa: E402

CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")
DRAINABLE_STATUS = ("done", "archived")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=True, timeout=600).stdout


def initializing(repo: Path) -> list:
    """``[{path, branch, head}]`` of every registration ``worktree list`` reports ``locked initializing``."""
    out, cur = [], {}
    for line in git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur.get("locked") == kwr.INIT_LOCK:
                out.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        if key == "worktree":
            cur["path"] = val
        elif key == "branch":
            cur["branch"] = val[len("refs/heads/"):] if val.startswith("refs/heads/") else val
        elif key == "HEAD":
            cur["head"] = val
        elif key == "locked":
            cur["locked"] = val
    return out


def card_row(conn, tid: str):
    if conn is None:
        return None
    row = conn.execute("SELECT status, branch_name FROM tasks WHERE id = ?", (tid,)).fetchone()
    if row is None:
        return {"exists": False}
    spawned = conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'spawned' LIMIT 1",
                           (tid,)).fetchone() is not None
    return {"exists": True, "status": row[0], "branch_name": row[1], "spawned": spawned}


def attribute(repo: Path, conn, min_age_s: int, now: float) -> list:
    repo = repo.resolve()
    common = kwr.common_dir(repo)
    rows = []
    for wt in initializing(repo):
        path = Path(wt["path"])
        admins = kwr.admin_dirs_for(common, path) if common is not None else []
        admin = admins[0] if len(admins) == 1 else None
        try:
            age = int(now - (admin / "locked").stat().st_mtime) if admin is not None else None
        except OSError:
            age = None
        name = path.name
        dispatcher = path.parent.name == ".worktrees" and path.parent.parent.resolve() == repo \
            and bool(CARD_RE.match(name))
        shape = "dispatcher:.worktrees/<card>" if dispatcher else (
            f".worktrees/{name}" if path.parent.name == ".worktrees" else str(path.parent))
        branch = wt.get("branch") or ""
        own = kwr.branch_has_own_commits(repo, branch) if branch else False
        card = card_row(conn, name) if dispatcher else None
        why = []
        if admin is None:
            why.append(f"{len(admins)} admin dirs name the path")
        elif (admin / "index").exists():
            why.append("has an index (checkout finished)")
        if not dispatcher:
            why.append("not a dispatcher path")
        elif card is None:
            why.append("no board given")
        elif not card["exists"]:
            why.append("no card with that id")
        elif card["status"] not in DRAINABLE_STATUS and card["spawned"]:
            why.append(f"card {card['status']} and it ran")
        if dispatcher and branch and branch not in (f"wt/{name}", (card or {}).get("branch_name") or ""):
            why.append(f"branch {branch} is not the card's")
        late = kwr.written_after_add(path, admin) if admin is not None and path.is_dir() else None
        if late is not None:
            why.append(f"written after the add died: {late}")
        if age is None or age < min_age_s:
            why.append(f"lock age {age}s < {min_age_s}s")
        rows.append({"path": str(path), "shape": shape, "branch": branch, "head": wt.get("head", ""),
                     "card": name if dispatcher else None,
                     "card_status": (card or {}).get("status") if card and card.get("exists") else
                     ("missing" if card else None),
                     "card_spawned": (card or {}).get("spawned"), "lock_age_h": round(age / 3600, 1) if age else age,
                     "branch_own_commits": own, "admin": str(admin) if admin else None,
                     "drain": not why, "keep_reason": "; ".join(why)})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--db", type=Path, help="kanban.db (opened read-only); needed for card attribution / drain")
    ap.add_argument("--min-age-h", type=float, default=1.0)
    ap.add_argument("--json", action="store_true")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--drain", action="store_true")
    ap.add_argument("--execute", action="store_true", help="with --drain: actually remove")
    ap.add_argument("--limit", type=int, default=0, help="with --drain: at most N trees")
    a = ap.parse_args(argv)
    if a.execute and not a.drain:
        ap.error("--execute needs --drain")
    repo = a.repo.expanduser()
    now = time.time()
    min_age = int(a.min_age_h * 3600)
    conn = None
    if a.db:
        conn = sqlite3.connect(f"file:{a.db.expanduser()}?mode=ro", uri=True, timeout=10)
    if a.check:
        hits = []
        for r in attribute(repo, None, min_age, now):
            if r["lock_age_h"] is not None and r["lock_age_h"] * 3600 >= min_age:
                hits.append(r)
        for r in hits:
            print(f"finding: locked-initializing worktree {r['path']} (branch {r['branch'] or '-'}, "
                  f"lock {r['lock_age_h']} h old) in {repo}")
        if hits:
            print(f"finding: {len(hits)} half-built worktrees older than {a.min_age_h:g} h in {repo}; "
                  f"attribute + drain: {Path(__file__).name} --repo {repo} --db <kanban.db> --drain")
        return 1 if hits else 0
    rows = attribute(repo, conn, min_age, now)
    if a.drain:
        todo = [r for r in rows if r["drain"]]
        if a.limit:
            todo = todo[:a.limit]
        before = git(repo, "worktree", "list", "--porcelain").count("\nworktree ") + 1
        for r in todo:
            p, b = Path(r["path"]), r["branch"]
            if not a.execute:
                print(f"would remove {p} (card {r['card']} {r['card_status']}, lock {r['lock_age_h']} h)"
                      + (f"; would delete branch {b}" if b and r["branch_own_commits"] is False else
                         f"; keep branch {b} (own commits: {r['branch_own_commits']})" if b else ""))
                continue
            tip = kwr.rev(repo, f"refs/heads/{b}") if b else ""
            rep = kwr.remove_failed_add(repo, p, branch=b, created_branch=r["branch_own_commits"] is False and
                                        bool(tip), base_sha=tip)
            print(f"removed={rep['removed']} admin_dropped={len(rep['admin_dropped'])} "
                  f"branch_deleted={rep['branch_deleted']} {p}" + (f" kept={rep['kept']}" if rep["kept"] else ""))
        after = git(repo, "worktree", "list", "--porcelain").count("\nworktree ") + 1
        kept = len(rows) - len(todo)
        print(f"drain {'EXECUTED' if a.execute else 'DRY RUN'}: {len(todo)} to remove, {kept} kept, "
              f"registrations {before} -> {after}")
        return 0
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    cols = ("path", "shape", "branch", "card", "card_status", "card_spawned", "lock_age_h", "branch_own_commits",
            "drain", "keep_reason")
    print("\t".join(cols))
    for r in rows:
        print("\t".join("" if r[c] is None else str(r[c]) for c in cols))
    n = len(rows)
    print(f"# {n} locked-initializing registrations; drainable {sum(r['drain'] for r in rows)}; "
          f"dispatcher-shaped {sum(1 for r in rows if r['card'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
