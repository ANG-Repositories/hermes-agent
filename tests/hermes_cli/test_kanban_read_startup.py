"""Read CLI paths do not initialize SQLite, spawn git, or load dispatch machinery."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

ROOT = Path(__file__).resolve().parents[2]


def seed(path):
    with kbc.connect_closing(db_path=path) as conn:
        task = kb.create_task(conn, title="startup fixture")
    kbc._INITIALIZED_PATHS.discard(str(path.resolve()))
    return task


def test_read_connection_never_initializes_and_is_sqlite_readonly(tmp_path, monkeypatch):
    db = tmp_path / "board.db"
    task_id = seed(db)
    def forbidden(*args, **kwargs):
        pytest.fail("read connection entered initialization")
    monkeypatch.setattr(kbc, "_cross_process_init_lock", forbidden)
    with kbc.connect_closing(db_path=db, readonly=True) as conn:
        assert kb.get_task(conn, task_id).title == "startup fixture"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
        conn.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE tasks SET title='no'")
    with pytest.raises(sqlite3.OperationalError):
        kbc.connect(db_path=tmp_path / "missing.db", readonly=True)
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize("action", ["show", "list", "placement", "context", "runs", "attachments"])
def test_cli_reads_under_held_writer_and_init_lock(tmp_path, action):
    if sys.platform == "win32":
        pytest.skip("POSIX init-lock contention")
    import fcntl
    db = tmp_path / "kanban.db"
    task_id = seed(db)
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_KANBAN_DB": str(db),
           "HERMES_KANBAN_BUSY_TIMEOUT_MS": "100", "PYTHONPATH": str(ROOT)}
    argv = [action, "--json"]
    if action != "list":
        argv.insert(1, task_id)
    writer = sqlite3.connect(db)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with db.with_suffix(".db.init.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(
                [sys.executable, "-m", "hermes_cli.main", "kanban", *argv],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
                stdin=subprocess.DEVNULL,
            )
        assert result.returncode == 0, result.stderr
        assert "database is locked" not in result.stderr
    finally:
        writer.rollback()
        writer.close()


def test_show_imports_no_dispatch_pool_gateway_or_git(tmp_path):
    db = tmp_path / "kanban.db"
    task_id = seed(db)
    code = '''
import json, runpy, subprocess, sys
real = subprocess.Popen
launches = []
def counted(argv, *a, **kw):
    launches.append(str(argv))
    return real(argv, *a, **kw)
subprocess.Popen = counted
sys.argv = ['hermes', 'kanban', 'show', sys.argv[1], '--json']
runpy.run_module('hermes_cli.main', run_name='__main__')
for name in sys.modules:
    assert name not in ('hermes_cli.kanban_db_dispatch', 'hermes_cli.kanban_worker_pool'), name
    assert not name.startswith('gateway'), name
assert not launches, launches
'''
    result = subprocess.run(
        [sys.executable, "-c", code, task_id], cwd=ROOT,
        env={**os.environ, "HERMES_HOME": str(tmp_path), "HERMES_KANBAN_DB": str(db),
             "PYTHONPATH": str(ROOT)}, capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["id"] == task_id
