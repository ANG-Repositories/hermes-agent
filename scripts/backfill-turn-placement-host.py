#!/usr/bin/env python3
# SCHEDULE: manual (operator runs once per profile store after the placement_host column deploys)
"""Backfill ``turns.placement_host`` for one profile's Blackbox store (t_c9fc6c9f).

A historical turn needs positive provenance: the session prefix of its turn_id
must match ``worker_session_id`` in the covering task_runs.metadata. Time overlap alone
does not prove worker ownership. Exactly one run of this profile may overlap
the turn, including runs whose host is unknown; that run must cover the whole
turn and have a known host (``studio`` -> ``studio-local``). Missing provenance,
overlapping runs, gateway turns and already stamped rows are left unchanged.

Dry-run is the default; ``--apply`` takes a WAL-safe VACUUM INTO backup first.

    python3 scripts/backfill-turn-placement-host.py --home ~/.hermes/profiles/daedalus \\
        --board ~/.hermes/kanban.db --profile daedalus            # inspect
    ... --apply                                                    # write
"""
from __future__ import annotations

import argparse
import json
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
        run_cols = {r[1] for r in board.execute("PRAGMA table_info(task_runs)")}
        if "metadata" not in run_cols:
            return []  # No positive worker-session provenance in this board.
        runs = board.execute(
            "SELECT started_at, COALESCE(ended_at, 9e18), host, metadata FROM task_runs "
            "WHERE profile = ?",
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
        # agent.turn_facade: <session_id>:<effective_task_id>:<nonce>.
        parts = turn_id.rsplit(":", 2)
        if len(parts) != 3 or not all(parts):
            continue
        session_id = parts[0]
        if plat not in WORKER_PLATFORMS or not ts_start or not ts_end:
            continue
        end = ts_end
        if end < ts_start:
            continue
        overlapping = [(s, e, h, m) for s, e, h, m in runs if s <= end and ts_start <= e]
        if len(overlapping) != 1:
            continue
        start, stop, host, metadata = overlapping[0]
        if not (start <= ts_start and end <= stop) or not host or not host.strip():
            continue
        try:
            provenance = json.loads(metadata or "null")
        except (TypeError, ValueError):
            continue
        if not isinstance(provenance, dict) or provenance.get("worker_session_id") != session_id:
            continue
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
