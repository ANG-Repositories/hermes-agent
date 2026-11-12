#!/usr/bin/env python3
# SCHEDULE: manual (one-shot bench for t_6314cc2f; run on the Mac Studio)
"""Measure LCM append_batch commit and checkpoint latency with checkpoint_fullfsync OFF vs ON.

Usage (on the Studio, from a hermes-agent checkout of the PR branch):
    python3 t6314_lcm_fsync_bench.py --src ~/.hermes/lcm.db --work ~/.hermes/var/t6314-bench \
        [--rounds 6 --commits 40 --batch 8 --ckpt-every 10] [--also-fullfsync]

It APFS-clones (cp -c) src, -wal, -shm into --work, never opens src, opens the CLONE through
MessageStore (the production path), and interleaves OFF/ON rounds so host-load drift hits both.
Commits run with wal_autocheckpoint=0; an explicit PASSIVE checkpoint every --ckpt-every commits
is timed separately. Prints one JSON object; deletes the clone unless --keep.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, int(round(q / 100.0 * (len(s) - 1)))))] * 1000.0, 3)


def summarize(values):
    return {"n": len(values), "p50_ms": pct(values, 50), "p99_ms": pct(values, 99),
            "max_ms": round(max(values) * 1000.0, 3) if values else None,
            "mean_ms": round(statistics.fmean(values) * 1000.0, 3) if values else None}


def clone(src: Path, work: Path) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    dst = work / "lcm.db"
    for suffix in ("", "-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.exists():
            flag = ["-c"] if platform.system() == "Darwin" else []
            subprocess.run(["cp", *flag, str(s), str(dst) + suffix], check=True)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--commits", type=int, default=40)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--chars", type=int, default=1500)
    ap.add_argument("--ckpt-every", type=int, default=10)
    ap.add_argument("--also-fullfsync", action="store_true")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    sys.path.insert(0, a.repo)
    from plugins.context_engine.lcm.store import MessageStore  # production open path

    src, work = Path(a.src).expanduser(), Path(a.work).expanduser()
    t0 = time.monotonic()
    db = clone(src, work)
    clone_s = time.monotonic() - t0
    home = work / "home"
    home.mkdir(exist_ok=True)
    t0 = time.monotonic()
    store = MessageStore(db_path=str(db), hermes_home=str(home))
    open_s = time.monotonic() - t0
    conn = store._conn
    conn.execute("PRAGMA wal_autocheckpoint=0")
    readback = {p: conn.execute(f"PRAGMA {p}").fetchone()[0]
                for p in ("checkpoint_fullfsync", "fullfsync", "synchronous", "journal_mode")}

    modes = [("off", 0, 0), ("ckpt_fullfsync", 1, 0)]
    if a.also_fullfsync:
        modes.append(("fullfsync_all", 1, 1))
    res = {m: {"commit": [], "checkpoint": [], "ckpt_frames": []} for m, _, _ in modes}
    body = "bench " + "y" * a.chars
    sid = f"t6314-bench-{os.getpid()}"
    for r in range(a.rounds):
        for name, ckpt, full in (modes if r % 2 == 0 else list(reversed(modes))):
            conn.execute(f"PRAGMA checkpoint_fullfsync={ckpt}")
            conn.execute(f"PRAGMA fullfsync={full}")
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            for i in range(a.commits):
                msgs = [{"role": "user", "content": f"{body} r{r} {name} {i} {j}"} for j in range(a.batch)]
                t = time.monotonic()
                store.append_batch(sid, msgs)
                res[name]["commit"].append(time.monotonic() - t)
                if (i + 1) % a.ckpt_every == 0:
                    t = time.monotonic()
                    row = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                    res[name]["checkpoint"].append(time.monotonic() - t)
                    res[name]["ckpt_frames"].append(int(row[2]) if row else -1)
    store.close()
    try:
        load = os.getloadavg()
    except OSError:
        load = None
    out = {
        "host": platform.node(), "system": platform.system(), "python": sys.version.split()[0],
        "sqlite": __import__("sqlite3").sqlite_version, "src_bytes": src.stat().st_size,
        "clone_s": round(clone_s, 3), "open_s": round(open_s, 3), "loadavg_end": load,
        "params": vars(a), "readback_after_open": readback,
        "results": {m: {"commit": summarize(v["commit"]), "checkpoint": summarize(v["checkpoint"]),
                        "ckpt_frames_mean": (round(statistics.fmean(v["ckpt_frames"]), 1)
                                             if v["ckpt_frames"] else None)}
                    for m, v in res.items()},
    }
    print(json.dumps(out, indent=2, default=str))
    if not a.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
