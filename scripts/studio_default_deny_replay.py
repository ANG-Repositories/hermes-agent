#!/usr/bin/env python3
# SCHEDULE: manual (operator / one replay card runs it against the live board, read-only)
"""Replay the last N days of kanban placements under Studio default-deny (t_062a5948). Read-only, stdlib only.

For every run that STARTED on the Mac Studio (``placement`` event whose host is the Studio), it asks what the rule
(``kanban.placement.studio_default_deny``) would have done with a given allowlist:

  allowlisted   the capability is on the allowlist: runs on the Studio as before
  exempt        operator-only card (operator assignee, or no_worker): runs on the Studio as before
  moves         nothing Studio-only held it (portable last resort, pool-served capability, carried files):
                under t_9965b8d5 + this rule it goes to a pool host; the wait is estimated from the next pool
                placement after it (0 when a pool host had a free slot that tick)
  override      a Studio-only capability (or structural fact) NOT on the allowlist: the card has no host at all
                and waits for an operator ``hermes kanban studio-override`` (one per run with --once)

Output: a markdown report (per-capability table with proposed keep/deny, the per-day override count, the moved
cards and their estimated waits, and every card that would have been stuck).

  studio_default_deny_replay.py --db ~/.hermes/kanban.db [--db <other board>] \
      --caps ~/.hermes/fleet/host-capabilities.json [--days 14] [--allow a,b,c] [--now EPOCH]

``--allow`` defaults to the proposed keep list (:data:`PROPOSED`). ``--allow ''`` replays an empty allowlist.
"""
from __future__ import annotations

import argparse
import collections
import datetime as _dt
import json
import sqlite3
import sys
import time
from pathlib import Path

OPERATOR_PROFILES = frozenset({"default", "apollo", "aegis"})
STRUCTURAL = ("profile", "native_command", "host_reach", "linked", "workspace_of", "workspace_shared",
              "workspace_kind", "local_files", "workspace_bind")
# Proposed keep (truly Studio-only) / deny, with the reason. The replay prints counts next to each; the operator
# decides. A capability absent here is shown as "deny (unreviewed)".
PROPOSED = {
    "imessage": ("keep", "Ace's Messages account is signed in on the Studio only"),
    "keychain": ("keep", "the login keychain is the Studio user's"),
    "op-desktop": ("keep", "1Password desktop app integration is on the Studio session"),
    "apple-apps": ("keep", "Notes/Reminders/Calendar app stores signed into Ace's Apple ID"),
    "browser-session": ("keep", "Ace's signed-in browser profiles live on the Studio"),
    "obsidian-vault": ("keep", "the vault + Obsidian sync run on the Studio only (t_9c99978a: no pool host can serve it)"),
    "acoustic-bench": ("keep", "the physical voice bench is attached to the Studio"),
    "profile": ("keep", "the assignee profile is installed only on the Studio (pool hosts do not run it)"),
    "native_command": ("keep", "the assignee is a native-command lane that only runs on the Studio"),
    "studio-state": ("deny", "reads the live board/state: a pool host can be given a read replica (gap to fix)"),
    "control-plane-read": ("deny", "same as studio-state: a read replica or API closes it"),
    "control-plane": ("deny", "launchd / skills tree / home tree edits: most cards only open a PR; surface each"),
    "android-sdk": ("deny", "the SDK installs on a pool host"),
    "linked": ("deny", "a link to a live card is lineage, not a need (relink derived-from)"),
    "workspace_of": ("deny", "shares a parent's Studio dir: carry/port it instead"),
    "workspace_shared": ("deny", "a shared Studio dir: carry/port it instead"),
    "workspace_kind": ("deny", "a worktree that could not port: fix the port, not the host"),
    "host_reach": ("deny", "a target no pool host reaches yet: give a pool host the reach"),
    "workspace_bind": ("deny", "bound workspace: carry/port it"),
}


def is_studio(host) -> bool:
    h = str(host or "").lower()
    return h.startswith("mac-studio") or h == "studio" or h.startswith("studio")


def load_caps(path: Path):
    doc = json.loads(path.read_text())
    hosts = {h: set(v if isinstance(v, list) else (v or {}).get("capabilities") or [])
             for h, v in (doc.get("hosts") or {}).items()}
    studio = hosts.pop("studio", set())
    pool = set().union(*hosts.values()) if hosts else set()
    return (studio - pool) | {"control-plane-read"}, pool


def capability_of(p: dict):
    """(capability, source) of a Studio placement payload."""
    sc = p.get("studio_capability")
    if isinstance(sc, dict) and sc.get("capability"):
        return str(sc["capability"]), str(sc.get("source") or "?")
    rule = str(p.get("rule") or "")
    if rule.startswith("access_bound:"):
        return rule.split(":", 1)[1], "access-rule"
    if rule.startswith("requires:"):
        return rule[len("requires:"):], "rule"
    if rule in ("studio_bound_skill", "studio_manifest"):
        return "control-plane", "skill"
    if rule == "studio_body":
        return "browser-session", "body"
    if rule == "studio_needs":
        return "studio-needs", "body"
    if rule.startswith(STRUCTURAL):
        return rule, "structural"
    return None, rule or "default"


def head(cap: str) -> str:
    return cap.split(":", 1)[0]


def covered(cap, allow) -> bool:
    ps = [x.strip() for x in str(cap or "").split(",") if x.strip()]
    return bool(ps) and all(x in allow or head(x) in allow for x in ps)


def studio_only_cap(cap, studio_only) -> bool:
    if cap is None:
        return False
    ps = [x.strip() for x in cap.split(",") if x.strip()]
    if any(head(x) in STRUCTURAL and head(x) not in ("local_files",) for x in ps):
        return True
    if cap == "studio-needs":
        return True  # body words meant a Studio resource; t_c82eded0 removes this source
    return any(x in studio_only for x in ps)


def replay(dbs, studio_only, allow, since, now):
    rows, moved, stuck = [], [], []
    for db in dbs:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        pool_times = [r[0] for r in con.execute(
            "SELECT created_at FROM task_events WHERE kind='worker_placed' AND created_at >= ? ORDER BY created_at",
            (since,))]
        cols = {r[1] for r in con.execute("PRAGMA table_info(tasks)")}
        nw = "no_worker" if "no_worker" in cols else "0"
        tasks = {r["id"]: r for r in con.execute(f"SELECT id, title, assignee, {nw} AS no_worker FROM tasks")}
        for ev in con.execute("SELECT task_id, run_id, created_at, payload FROM task_events WHERE kind='placement' "
                              "AND created_at >= ? AND created_at < ? ORDER BY created_at", (since, now)):
            try:
                p = json.loads(ev["payload"] or "{}")
                if isinstance(p, str):
                    p = json.loads(p)
            except (TypeError, ValueError):
                continue
            if not isinstance(p, dict) or not is_studio(p.get("host")):
                continue
            t = tasks.get(ev["task_id"])
            cap, src = capability_of(p)
            assignee = str((t["assignee"] if t else "") or "").lower()
            if t is not None and (t["no_worker"] or assignee in OPERATOR_PROFILES):
                verdict = "exempt"
            elif studio_only_cap(cap, studio_only):
                verdict = "allowlisted" if covered(cap, allow) else "override"
            else:
                verdict = "moves"
            row = {"task_id": ev["task_id"], "run_id": ev["run_id"], "at": int(ev["created_at"]), "cap": cap,
                   "source": src, "verdict": verdict, "title": (t["title"] if t else "")[:70], "assignee": assignee}
            if verdict == "moves":
                free = any((c or {}).get("free_slots", 0) > 0 and (c or {}).get("band") in ("ok", "admitting", None)
                           and (c or {}).get("reachable", True) is not False for c in p.get("considered") or [])
                if free:
                    row["wait_s"] = 0
                else:
                    nxt = next((x for x in pool_times if x > row["at"]), None)
                    row["wait_s"] = (nxt - row["at"]) if nxt is not None else None
                moved.append(row)
            if verdict == "override":
                stuck.append(row)
            rows.append(row)
        con.close()
    return rows, moved, stuck


def _day(ts):
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def report(rows, moved, stuck, allow, days, out=sys.stdout):
    w = out.write
    total = len(rows)
    by = collections.Counter(r["verdict"] for r in rows)
    w(f"# Studio default-deny replay: last {days} days\n\n")
    w(f"Allowlist replayed: {', '.join(sorted(allow)) or '(empty)'}\n\n")
    w(f"Studio-started runs: {total}. allowlisted {by['allowlisted']}, exempt {by['exempt']}, "
      f"moves to a pool host {by['moves']}, needs an operator override (no host at all) {by['override']}.\n\n")
    w("## Capabilities that started a run on the Studio\n\n| capability | runs | cards | sources | proposal | why |\n"
      "|---|---|---|---|---|---|\n")
    caps = collections.defaultdict(list)
    for r in rows:
        if r["verdict"] in ("allowlisted", "override"):
            caps[r["cap"]].append(r)
    for cap, rs in sorted(caps.items(), key=lambda kv: -len(kv[1])):
        parts = [x.strip() for x in (cap or "").split(",")]
        verdicts = [PROPOSED.get(head(x), ("deny", "unreviewed")) for x in parts]
        keep = all(v[0] == "keep" for v in verdicts)
        why = "; ".join(f"{x}: {v[1]}" for x, v in zip(parts, verdicts))
        srcs = ",".join(sorted({r["source"] for r in rs}))
        w(f"| {cap} | {len(rs)} | {len({r['task_id'] for r in rs})} | {srcs} | {'keep' if keep else 'deny'} | {why} |\n")
    w("\n## Overrides per day (runs the operator would have had to override)\n\n| day | overrides | distinct cards |"
      " capabilities |\n|---|---|---|---|\n")
    per = collections.defaultdict(list)
    for r in stuck:
        per[_day(r["at"])].append(r)
    for d in sorted(per):
        rs = per[d]
        cc = collections.Counter(r["cap"] for r in rs)
        w(f"| {d} | {len(rs)} | {len({r['task_id'] for r in rs})} | "
          f"{', '.join(f'{c} {n}' for c, n in cc.most_common())} |\n")
    n_days = max(1, days)
    avg = len(stuck) / n_days
    w(f"\nMean {avg:.1f} overrides/day over {days} days (max {max((len(v) for v in per.values()), default=0)}).")
    w(" Over ~10/day the rule is a queue on the operator: widen the allowlist first.\n" if avg > 10 else
      " Under the ~10/day line.\n")
    w("\n## Runs that would have moved to a pool host (estimated wait)\n\n")
    waits = [r["wait_s"] for r in moved if r.get("wait_s") is not None]
    if moved:
        waits_sorted = sorted(waits)
        p50 = waits_sorted[len(waits_sorted) // 2] if waits_sorted else None
        w(f"{len(moved)} runs, {len({r['task_id'] for r in moved})} cards. Wait 0 (a pool host had a free slot that "
          f"tick): {sum(1 for x in waits if x == 0)}. Median wait {p50 if p50 is not None else '-'} s, longest "
          f"{max(waits) if waits else '-'} s, unknown {sum(1 for r in moved if r.get('wait_s') is None)}.\n\n")
        w("| why it was on the Studio | runs |\n|---|---|\n")
        for k, n in collections.Counter((r["cap"] or r["source"]) for r in moved).most_common(15):
            w(f"| {k} | {n} |\n")
    else:
        w("none\n")
    w("\n## Cards that would have been stuck with no host (each needs an override)\n\n"
      "| card | runs | capability | source | assignee | title |\n|---|---|---|---|---|---|\n")
    cards = collections.OrderedDict()
    for r in stuck:
        cards.setdefault(r["task_id"], []).append(r)
    for tid, rs in cards.items():
        r = rs[0]
        w(f"| {tid} | {len(rs)} | {r['cap']} | {r['source']} | {r['assignee']} | {r['title'].replace('|', '/')} |\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", action="append", required=True)
    ap.add_argument("--caps", required=True)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--allow", default=None)
    ap.add_argument("--now", type=int, default=None)
    a = ap.parse_args(argv)
    now = a.now or int(time.time())
    studio_only, _pool = load_caps(Path(a.caps).expanduser())
    if a.allow is None:
        allow = {k for k, (v, _w) in PROPOSED.items() if v == "keep"}
    else:
        allow = {x.strip() for x in a.allow.split(",") if x.strip()}
    rows, moved, stuck = replay([str(Path(d).expanduser()) for d in a.db], studio_only, allow,
                                now - a.days * 86400, now)
    report(rows, moved, stuck, allow, a.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
