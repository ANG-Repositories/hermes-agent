"""GIL hold per spawn in a large-RSS process (t_afc525a2). Manual benchmark, not collected.

    python tests/tools/spawn_gil_bench.py --rss-gb 2 --spawns 40 --threads 200

A ticker thread records the longest gap between two of its loop iterations while each
spawn call runs. A gap that long means some thread held the GIL (or the whole process was
stopped) for that time. Modes:

* ``legacy``: the pre-fix ``LocalEnvironment._run_bash`` call (cwd=, start_new_session=True,
  default close_fds=True), so CPython takes ``_posixsubprocess.fork_exec``. On Linux that
  path can use vfork.
* ``legacy-fork``: the same call with ``subprocess._USE_VFORK = False``. That is the macOS
  shape, where fork_exec always does a real fork() of the whole process.
* ``nofork``: ``tools.spawn_nofork.spawn_popen`` (posix_spawn + setsid).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from tools.spawn_nofork import spawn_popen  # noqa: E402

BASH = "/bin/bash"


class Ticker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop = False
        self.window_max = 0.0

    def run(self):
        last = time.perf_counter()
        while not self.stop:
            now = time.perf_counter()
            gap = now - last
            if gap > self.window_max:
                self.window_max = gap
            last = now


def _spawn(mode: str, cwd: str) -> subprocess.Popen:
    kwargs = dict(text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if mode == "nofork":
        return spawn_popen(["/bin/sh", "-c", 'cd -- "$1" || exit 126; shift; exec "$@"', "sh", cwd,
                            BASH, "-c", "true"], new_session=True, **kwargs)
    return subprocess.Popen([BASH, "-c", "true"], start_new_session=True, cwd=cwd, **kwargs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rss-gb", type=float, default=2.0)
    ap.add_argument("--spawns", type=int, default=40)
    ap.add_argument("--threads", type=int, default=200)
    ap.add_argument("--modes", default="legacy,legacy-fork,nofork")
    ap.add_argument("--fds", type=int, default=1500)
    opts = ap.parse_args()
    descriptors = [os.open(os.devnull, os.O_RDONLY) for _ in range(opts.fds)]
    counters = {"fork_exec": 0, "posix_spawn": 0}
    real_spawn = os.posix_spawn

    def counted_spawn(*args, **kwargs):
        counters["posix_spawn"] += 1
        return real_spawn(*args, **kwargs)

    os.posix_spawn = counted_spawn
    real_fork = subprocess._fork_exec

    def counted_fork(*args, **kwargs):
        counters["fork_exec"] += 1
        return real_fork(*args, **kwargs)

    subprocess._fork_exec = counted_fork

    # Many separately allocated, page-touched chunks reproduce a fragmented heap,
    # unlike one contiguous bytearray (especially important for macOS fork cost).
    chunks = [bytearray(64 * 1024) for _ in range(int(opts.rss_gb * (1 << 30)) // (64 * 1024))]
    for chunk in chunks:
        chunk[::4096] = b"\x01" * 16
    idle = threading.Event()
    for _ in range(opts.threads):
        threading.Thread(target=idle.wait, daemon=True).start()
    sys.setswitchinterval(0.0005)
    ticker = Ticker()
    ticker.start()
    cwd = os.getcwd()
    rss_kb = int(open("/proc/self/status").read().split("VmRSS:")[1].split()[0]) if os.path.exists("/proc/self/status") else -1

    results = {"python": sys.version.split()[0], "platform": sys.platform, "rss_mb": rss_kb // 1024,
               "threads": threading.active_count(), "pid": os.getpid(), "modes": {}}
    for mode in opts.modes.split(","):
        counters.update(fork_exec=0, posix_spawn=0)
        subprocess._USE_VFORK = mode != "legacy-fork"
        stalls, walls = [], []
        for _ in range(opts.spawns):
            time.sleep(0.01)
            ticker.window_max = 0.0
            t0 = time.perf_counter()
            proc = _spawn(mode, cwd)
            walls.append(time.perf_counter() - t0)
            stalls.append(ticker.window_max)
            proc.communicate()
        subprocess._USE_VFORK = True
        ms = lambda xs, q: round(1000 * statistics.quantiles(xs, n=20)[q], 3)  # noqa: E731
        results["modes"][mode] = {
            "calls": dict(counters),
            "gil_stall_ms_p50": round(1000 * statistics.median(stalls), 3), "gil_stall_ms_p95": ms(stalls, 18),
            "gil_stall_ms_max": round(1000 * max(stalls), 3),
            "spawn_wall_ms_p50": round(1000 * statistics.median(walls), 3), "spawn_wall_ms_p95": ms(walls, 18)}
    ticker.stop = True
    idle.set()
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
