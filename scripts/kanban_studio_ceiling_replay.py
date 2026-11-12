#!/usr/bin/env python3
# SCHEDULE: manual (operator runs it to size hosts.mac-studio.consumers.kanban.ceiling, t_103a4648)
"""t_103a4648: replay the last 14 days of Studio worker runs under a running-worker ceiling N.

Usage: kanban_studio_ceiling_replay.py <backup dir of *.kanban.db> <end epoch> <N,N,...> [--216] [--since <epoch>]

Input: every board DB in one kanban backup (read-only, immutable). A Studio run = task_runs.host='studio'.
Model: runs arrive at their real started_at with their real duration; at most N run at once; a run that arrives
at the ceiling waits FIFO (by arrival) for the first slot. Delay pushes the run's end later by the same wait.
Operator-only (asap) cards are exempt: they start on arrival and still count against N for later arrivals.
"""
from __future__ import annotations

import glob
import heapq
import json
import sqlite3
import sys

SQL = ("SELECT r.task_id, r.started_at, COALESCE(r.ended_at, ?), COALESCE(t.asap, 0), "
       "(SELECT json_extract(e.payload, '$.rule') FROM task_events e WHERE e.run_id = r.id AND e.kind = 'placement' "
       " LIMIT 1) "
       "FROM task_runs r LEFT JOIN tasks t ON t.id = r.task_id "
       "WHERE r.host = 'studio' AND r.started_at >= ? AND r.started_at < ?")
# Rules #216 (studio_capability_only) no longer accepts as a reason to run on the Studio: the card is carried,
# ported or waits for a pool host instead. "--216" drops runs placed by these rules from the replay.
MOVED_BY_216 = {"workspace_kind", "local_files", "default", "unplanned"}


def load(backup_dir: str, t0: float, t1: float):
    runs = []
    for db in sorted(glob.glob(f"{backup_dir}/*.kanban.db")):
        try:
            c = sqlite3.connect(f"file:{db}?immutable=1&mode=ro", uri=True)
            cols = {r[1] for r in c.execute("PRAGMA table_info(task_runs)")}
            tcols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
            if "host" not in cols:
                continue
            sql = SQL if "asap" in tcols else SQL.replace("COALESCE(t.asap, 0)", "0")
            for tid, s, e, asap, outcome in c.execute(sql, (t1, t0, t1)):
                runs.append((float(s), max(0.0, float(e) - float(s)), bool(asap), tid, outcome))
            c.close()
        except sqlite3.DatabaseError as exc:
            print(f"skip {db}: {exc}", file=sys.stderr)
    runs.sort()
    return runs


def concurrency(intervals, t0, t1):
    ev = []
    for s, e in intervals:
        ev += [(s, 1), (e, -1)]
    ev.sort(key=lambda x: (x[0], x[1]))
    cur = peak = 0
    area = 0.0
    last = t0
    for t, d in ev:
        t = min(max(t, t0), t1)
        area += cur * (t - last)
        last = t
        cur += d
        peak = max(peak, cur)
    area += cur * (t1 - last)
    return area / (t1 - t0), peak


def replay(runs, n, t0, t1):
    running = []            # heap of end times
    queue = []              # FIFO of (arrival, dur)
    events = sorted(runs)   # (start, dur, asap, tid, outcome)
    waits = []
    at_ceiling = 0.0        # seconds with running >= n
    intervals = []
    i = 0
    now = t0
    full_since = None
    ep = {"id": 0, "open": False, "paged": set()}

    def in_window(a, b):
        # Only the part of [a, b] inside the replay window counts (Prism 399f775412b6).
        return max(0.0, min(b, t1) - max(a, t0))

    def mark(t):
        nonlocal at_ceiling, full_since
        if len(running) >= n and full_since is None:
            full_since = t
        elif len(running) < n and full_since is not None:
            at_ceiling += in_window(full_since, t)
            full_since = None

    while i < len(events) or queue or running:
        next_arr = events[i][0] if i < len(events) else float("inf")
        next_end = running[0] if running else float("inf")
        if next_end <= next_arr:
            now = heapq.heappop(running)
            while queue and len(running) < n:
                arr, dur = queue.pop(0)
                waits.append(now - arr)
                if now - arr >= 3600:
                    ep["paged"].add(ep["id"])
                heapq.heappush(running, now + dur)
                intervals.append((now, now + dur))
            if not queue and ep["open"]:
                ep["open"] = False
            mark(now)
            continue
        now = next_arr
        s, dur, asap, _tid, _o = events[i]
        i += 1
        if asap or (len(running) < n and not queue):
            heapq.heappush(running, now + dur)
            intervals.append((now, now + dur))
            waits.append(0.0)
        else:
            if not queue and not ep["open"]:
                ep["id"] += 1
                ep["open"] = True
            queue.append((now, dur))
        mark(now)
    if full_since is not None:
        at_ceiling += in_window(full_since, now)
    delayed = [w for w in waits if w > 0]
    mean, peak = concurrency(intervals, t0, t1)
    return {
        "ceiling": n, "runs": len(waits), "delayed": len(delayed),
        "delayed_pct": round(100.0 * len(delayed) / max(1, len(waits)), 1),
        "hours_at_ceiling": round(at_ceiling / 3600.0, 1),
        "wait_p50_min": round(sorted(delayed)[len(delayed) // 2] / 60.0, 1) if delayed else 0.0,
        "wait_p95_min": round(sorted(delayed)[int(len(delayed) * 0.95)] / 60.0, 1) if delayed else 0.0,
        "longest_wait_min": round(max(delayed) / 60.0, 1) if delayed else 0.0,
        "waits_over_60min": sum(1 for w in delayed if w > 3600),
        "wait_episodes": ep["id"], "paging_episodes": len(ep["paged"]),
        "mean_running": round(mean, 2), "peak_running": peak,
    }


def main():
    backup, t1 = sys.argv[1], float(sys.argv[2])
    t0 = t1 - 14 * 86400
    if "--since" in sys.argv:
        t0 = float(sys.argv[sys.argv.index("--since") + 1])
    runs = load(backup, t0, t1)
    if "--216" in sys.argv:
        runs = [r for r in runs if (r[4] or "") not in MOVED_BY_216]
    base_mean, base_peak = concurrency([(s, s + d) for s, d, *_ in runs], t0, t1)
    long_runs = sum(1 for r in runs if r[1] > 6 * 3600)
    print(json.dumps({"window": [t0, t1], "studio_runs": len(runs), "asap_runs": sum(r[2] for r in runs),
                      "runs_over_6h": long_runs, "baseline_mean_running": round(base_mean, 2),
                      "baseline_peak_running": base_peak}))
    for n in [int(x) for x in sys.argv[3].split(",")]:
        print(json.dumps(replay(runs, n, t0, t1)))


if __name__ == "__main__":
    main()
