#!/usr/bin/env python3
# SCHEDULE: manual (operator runs the audit; --check is the sweep a cron row in hermes-home calls)
"""``git worktree add`` leftovers locked ``initializing`` in a fleet checkout: a READ-ONLY report (t_f609a5a0).

A killed ``git worktree add`` leaves its tree registered and locked with git's own reason ``initializing``. Two
states: ``half-built`` (no index, a partial checkout) and ``finished-still-locked`` (the checkout child outlived
its killed parent and wrote the index; spawn reuse and card-close cleanup release that stale lock).

Modes (both read-only: this script never removes, unlocks or writes anything in the repo; the board is opened
read-only; ruling:t_f609a5a0-no-automatic-deletion):

  table   (default) one row per ``locked initializing`` registration: path, state, path shape, branch, HEAD, the
          card that path names and its status, whether it ever spawned, lock age, whether the branch has commits
          no remote-tracking ref has. ``--json`` for machine output.
  --check the sweep: exit 1 and one ``finding:`` line per HALF-BUILT registration (locked ``initializing`` AND no
          index) older than ``--min-age-h`` (default 1 h); silent, exit 0, when clean. No content hashing, no board.

Removal is an operator's call after inspecting the tree; this script prescribes no command.

Usage:
  kanban_worktree_residue_audit.py --repo ~/.hermes [--db ~/.hermes/kanban.db] [--json]
  kanban_worktree_residue_audit.py --repo ~/.hermes --check [--min-age-h 1]
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


def attribute(repo: Path, conn, now: float, *, cheap: bool = False) -> list:
    """One row per ``locked initializing`` registration. ``cheap`` (the ``--check`` sweep) skips every per-row git
    call: only the admin dir's files are read."""
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
        state = ("unknown" if admin is None else
                 "finished-still-locked" if (admin / "index").exists() else "half-built")
        own = None if cheap else (kwr.branch_has_own_commits(repo, branch) if branch else False)
        card = card_row(conn, name) if dispatcher else None
        rows.append({"path": str(path), "state": state, "shape": shape, "branch": branch or "(detached)",
                     "head": wt.get("head", ""), "card": name if dispatcher else None,
                     "card_status": (card or {}).get("status") if card and card.get("exists") else
                     ("missing" if card else None),
                     "card_spawned": (card or {}).get("spawned"),
                     "lock_age_h": round(age / 3600, 1) if age is not None else None,
                     "branch_own_commits": own})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--db", type=Path, help="kanban.db (opened read-only); needed for card attribution")
    ap.add_argument("--min-age-h", type=float, default=1.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    repo = a.repo.expanduser()
    now = time.time()
    if a.check:
        hits = [r for r in attribute(repo, None, now, cheap=True)
                # Keyed on "no index", never on the lock alone: a finished tree a killed add left locked is a
                # healthy checkout (Studio 2026-11-11: 150 of 189 locked trees), not a finding.
                if r["state"] == "half-built" and r["lock_age_h"] is not None and r["lock_age_h"] >= a.min_age_h]
        for r in hits:
            print(f"finding: half-built worktree (locked initializing, no index) {r['path']} (branch {r['branch']}, "
                  f"lock {r['lock_age_h']} h old) in {repo}")
        if hits:
            print(f"finding: {len(hits)} half-built worktrees older than {a.min_age_h:g} h in {repo}; "
                  f"attribute: {Path(__file__).name} --repo {repo} --db <kanban.db>; operator: inspect before removing")
        return 1 if hits else 0
    conn = sqlite3.connect(f"file:{a.db.expanduser()}?mode=ro", uri=True, timeout=10) if a.db else None
    rows = attribute(repo, conn, now)
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0
    cols = ("path", "state", "shape", "branch", "head", "card", "card_status", "card_spawned", "lock_age_h",
            "branch_own_commits")
    print("\t".join(cols))
    for r in rows:
        print("\t".join("" if r[c] is None else str(r[c]) for c in cols))
    print(f"# {len(rows)} locked-initializing registrations; half-built {sum(r['state'] == 'half-built' for r in rows)}; "
          f"dispatcher-shaped {sum(1 for r in rows if r['card'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
