#!/usr/bin/env python3
# SCHEDULE: manual (operator runs once per profile store after the placement_host column deploys)
"""Backfill ``turns.placement_host`` for one profile's Blackbox store (t_c9fc6c9f).

Old rows predate the column. A kanban worker turn ran inside exactly one
``task_runs`` row of the board, and that row records where it executed
(``host``: a pool host id, or ``studio`` for a local run). A NULL-host turn of
this profile whose whole window [ts_start, ts_end] sits inside exactly ONE such
run gets that run's host (``studio`` -> ``studio-local``). Turns covered by no
run, by more than one run (concurrent workers of one profile: ambiguous), or
written by a gateway platform are left NULL. Rows that already carry a host are
never touched.

Dry-run is the default; ``--apply`` takes a WAL-safe VACUUM INTO backup first.

    python3 scripts/backfill-turn-placement-host.py --home ~/.hermes/profiles/daedalus \\
        --board ~/.hermes/kanban.db --profile daedalus            # inspect
    ... --apply                                                    # write
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

LOCAL_RUN_HOST = "studio"
LOCAL_HOST_NAME = "studio-local"  # hermes_cli.kanban_worker_hosts.LOCAL_HOST_NAME
# A worker turn is recorded by the CLI; a gateway turn on these platforms never ran in a worker.
WORKER_PLATFORMS = ("", "cli")


def _plan(turns_db: Path, board_db: Path, profile: str) -> list[tuple[str, str]]:
    with sqlite3.connect(f"file:{board_db}?mode=ro", uri=True, timeout=30) as board:
        runs = board.execute(
            "SELECT started_at, COALESCE(ended_at, 9e18), host FROM task_runs "
            "WHERE profile = ? AND host IS NOT NULL AND host != ''",
            (profile,),
        ).fetchall()
    with sqlite3.connect(f"file:{turns_db}?mode=ro", uri=True, timeout=30) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
        if "placement_host" not in cols:
            sys.exit(f"{turns_db}: no placement_host column yet (deploy the schema first)")
        platform = "COALESCE(platform, '')" if "platform" in cols else "''"
        rows = conn.execute(
            f"SELECT turn_id, ts_start, ts_end, {platform} FROM turns "
            "WHERE placement_host IS NULL"
        ).fetchall()
    plan = []
    for turn_id, ts_start, ts_end, plat in rows:
        if plat not in WORKER_PLATFORMS or not ts_start:
            continue
        end = ts_end or ts_start
        hosts = {h for s, e, h in runs if s <= ts_start and end <= e}
        if len(hosts) != 1:
            continue
        host = hosts.pop()
        plan.append((turn_id, LOCAL_HOST_NAME if host == LOCAL_RUN_HOST else host))
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, required=True, help="the profile's HERMES_HOME")
    parser.add_argument("--board", type=Path, required=True, help="the board's kanban.db")
    parser.add_argument("--profile", required=True, help="task_runs.profile of this store")
    parser.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    args = parser.parse_args()
    turns_db = args.home / "blackbox" / "turns.db"
    for path in (turns_db, args.board):
        if not path.is_file():
            parser.error(f"does not exist: {path}")

    plan = _plan(turns_db, args.board, args.profile)
    by_host = Counter(h for _, h in plan)
    print(f"{turns_db}: mode={'apply' if args.apply else 'dry-run'} would_stamp={len(plan)} "
          f"by_host={dict(sorted(by_host.items()))}")
    if not args.apply or not plan:
        return 0
    backup = turns_db.with_name("turns-before-placement-backfill-"
                                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".db")
    with sqlite3.connect(str(turns_db), timeout=30) as conn:
        conn.execute("VACUUM INTO ?", (str(backup),))
    print(f"backup={backup}")
    with sqlite3.connect(str(turns_db), timeout=30) as conn:
        cur = conn.executemany(
            "UPDATE turns SET placement_host = ? WHERE turn_id = ? AND placement_host IS NULL",
            [(h, t) for t, h in plan],
        )
        print(f"stamped={cur.rowcount}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
