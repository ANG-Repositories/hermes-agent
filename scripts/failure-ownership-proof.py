#!/usr/bin/env python3
"""Disposable S2 authority acceptance; never opens a production board."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arm', choices=('atomic', 'crash'), required=True)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--producers', type=int, default=4)
    parser.add_argument('--processes', type=int, default=32)
    parser.add_argument('--all-boundaries', action='store_true')
    args = parser.parse_args()
    root = args.scratch.expanduser().resolve()
    repo = Path(__file__).resolve().parents[1]
    # An explicit empty/new scratch root is required; no DB path override exists.
    if root == Path.home() or root == repo or not root.is_absolute():
        parser.error('dedicated scratch directory required')
    if root.exists() and any(root.iterdir()):
        parser.error('scratch root must be empty')
    if (args.producers, args.processes) != (4, 32):
        parser.error('this pinned acceptance requires 4 producers and 32 processes')
    root.mkdir(parents=True, exist_ok=True)
    selected = ('test_real_32_process_four_producer_race' if args.arm == 'atomic'
                else 'test_sigkill_transaction_boundary_retry')
    command = [sys.executable, '-m', 'pytest', '-o', 'addopts=', '-q', '-s',
               f'tests/hermes_cli/test_kanban_failure_ownership.py::{selected}',
               '--basetemp', str(root / 'pytest')]
    env = dict(os.environ, HERMES_HOME=str(root / 'home'), TMPDIR=str(root))
    env.pop('HERMES_KANBAN_DB', None)
    started = time.monotonic()
    with (root / 'output.log').open('wb') as output:
        child = subprocess.Popen(command, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        try:
            rc = child.wait(timeout=180)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            rc = 124
        finally:
            # Reap multiprocessing grandchildren even after interrupted pytest.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    summary = dict(arm=args.arm, pid=child.pid, exit_code=rc,
                   elapsed_seconds=time.monotonic()-started, command=command,
                   log=str(root / 'output.log'))
    (root / 'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
