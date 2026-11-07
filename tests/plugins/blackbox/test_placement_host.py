"""Blackbox ``placement_host`` column: which box ran a kanban worker's tools (t_c9fc6c9f).

- the column is a guarded additive migration (old DB gains it, re-open is a no-op);
- a dispatcher-owned worker turn carries its pool host, ``studio-local`` when unplaced;
- a non-worker turn, and a child process that merely inherited a worker's env, stay NULL;
- orphan repair (another process writing the row) never stamps its own host;
- the backfill script is dry-run by default and never writes without ``--apply``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import plugins.blackbox as blackbox
from plugins.blackbox import store
from plugins.blackbox.record import TurnRecord
from hermes_cli import kanban_worker_hosts as kwh

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "blackbox" / "turns.db"
    with patch("plugins.blackbox.store._db_path", return_value=path):
        yield path


@pytest.fixture
def no_worker_env(monkeypatch):
    for name in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_OWNER_PID", kwh.PLACEMENT_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)


def _columns(path):
    conn = sqlite3.connect(str(path))
    try:
        return [(r[1], r[2]) for r in conn.execute("PRAGMA table_info(turns)")]
    finally:
        conn.close()


def _host(path, turn_id):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(
            "SELECT placement_host FROM turns WHERE turn_id = ?", (turn_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _build(**kwargs):
    return blackbox._build_record(
        session_id="s1", interrupted=False, model="m", platform="cli", provider="p",
        user_message="u", final_response="f", turn_usage={}, cfg={}, kwargs=kwargs,
    )


def _worker(monkeypatch, *, placement_host=None, owner_pid=None):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_deadbeef")
    monkeypatch.setenv("HERMES_KANBAN_OWNER_PID", str(owner_pid or os.getpid()))
    if placement_host:
        monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps(
            {"host": placement_host, "env": {"TERMINAL_ENV": "ssh"}}))


# --------------------------------------------------------------------------- #
# migration
# --------------------------------------------------------------------------- #
def test_new_db_has_nullable_text_column(db):
    store._connect().close()
    assert ("placement_host", "TEXT") in _columns(db)


def test_old_db_gains_column_and_reopen_is_idempotent(db, no_worker_env):
    # A store written by the previous release: today's schema minus the column.
    store.insert_turn(TurnRecord(turn_id="old", profile="default"))
    conn = sqlite3.connect(str(db))
    conn.execute("ALTER TABLE turns DROP COLUMN placement_host")
    conn.commit()
    conn.close()
    assert "placement_host" not in [c for c, _ in _columns(db)]

    store._connect().close()
    store._connect().close()

    names = [c for c, _ in _columns(db)]
    assert names.count("placement_host") == 1
    assert _host(db, "old") is None


# --------------------------------------------------------------------------- #
# population
# --------------------------------------------------------------------------- #
def test_spilled_worker_turn_carries_pool_host(db, no_worker_env, monkeypatch):
    _worker(monkeypatch, placement_host="ci-box")
    rec = _build(turn_id="turn_w")
    assert rec.placement_host == "ci-box"
    store.insert_turn(rec)
    assert _host(db, "turn_w") == "ci-box"


def test_boot_record_wins_over_mutated_env(no_worker_env, monkeypatch):
    _worker(monkeypatch, placement_host="ace-media")
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", "ci-box")
    assert _build().placement_host == "ci-box"


def test_unplaced_worker_turn_is_studio_local(db, no_worker_env, monkeypatch):
    _worker(monkeypatch)
    store.insert_turn(_build(turn_id="turn_l"))
    assert _host(db, "turn_l") == kwh.LOCAL_HOST_NAME == "studio-local"


def test_non_worker_turn_is_null(db, no_worker_env):
    store.insert_turn(_build(turn_id="turn_n"))
    assert _host(db, "turn_n") is None


def test_child_that_inherited_worker_env_is_null(no_worker_env, monkeypatch):
    # The grant names another pid: this process is not the worker.
    _worker(monkeypatch, placement_host="ci-box", owner_pid=os.getpid() + 100000)
    assert _build().placement_host is None


def test_explicit_none_kwarg_suppresses_own_host(no_worker_env, monkeypatch):
    _worker(monkeypatch, placement_host="ci-box")
    assert _build(placement_host=None).placement_host is None


def test_orphan_repair_does_not_stamp_its_own_host():
    src = (REPO / "plugins" / "blackbox" / "orphans.py").read_text()
    assert '"placement_host": None' in src


def test_blank_host_round_trips_as_null(db, no_worker_env):
    store.insert_turn(TurnRecord(turn_id="turn_b", placement_host="  "))
    assert _host(db, "turn_b") is None


# --------------------------------------------------------------------------- #
# backfill script
# --------------------------------------------------------------------------- #
SCRIPT = REPO / "scripts" / "backfill-turn-placement-host.py"


def _seed_backfill(tmp_path):
    home = tmp_path / "home"
    turns = home / "blackbox" / "turns.db"
    turns.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(turns))
    conn.execute("CREATE TABLE turns (turn_id TEXT PRIMARY KEY, platform TEXT, ts_start REAL,"
                 " ts_end REAL, placement_host TEXT)")
    conn.executemany("INSERT INTO turns VALUES (?, ?, ?, ?, ?)", [
        ("tw", "cli", 1000.0, 1010.0, None),      # inside run 7 window on ci-box
        ("tl", "cli", 2000.0, 2010.0, None),      # inside local run 8
        ("tn", "cli", 5000.0, 5010.0, None),      # no run covers it
        ("tx", "cli", 1001.0, 1002.0, "keep"),    # already stamped: untouched
        ("tg", "discord", 1000.0, 1010.0, None),  # gateway turn: never a worker
        ("ta", "cli", 3000.0, 3010.0, None),      # two runs on two hosts: ambiguous
    ])
    conn.commit()
    conn.close()
    board = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(board))
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,"
                 " started_at INTEGER, ended_at INTEGER, host TEXT)")
    conn.executemany("INSERT INTO task_runs VALUES (?, ?, ?, ?, ?, ?)", [
        (7, "t_a", "daedalus", 990, 1100, "ci-box"),
        (8, "t_b", "daedalus", 1990, 2100, "studio"),
        (9, "t_c", "daedalus", 2990, 3100, "ci-box"),
        (10, "t_d", "daedalus", 2995, 3100, "ace-media"),
        (11, "t_e", "other", 4990, 5100, "ci-box"),   # another profile's run
    ])
    conn.commit()
    conn.close()
    return home, turns, board


def _run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, timeout=60)


def test_backfill_dry_run_is_default_and_writes_nothing(tmp_path):
    home, turns, board = _seed_backfill(tmp_path)
    before = turns.read_bytes()
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus")
    assert out.returncode == 0, out.stderr
    assert "mode=dry-run" in out.stdout
    assert "would_stamp=2" in out.stdout
    assert turns.read_bytes() == before
    assert _host(turns, "tw") is None


def test_backfill_apply_stamps_only_covered_null_rows(tmp_path):
    home, turns, board = _seed_backfill(tmp_path)
    out = _run("--home", str(home), "--board", str(board), "--profile", "daedalus", "--apply")
    assert out.returncode == 0, out.stderr
    assert _host(turns, "tw") == "ci-box"
    assert _host(turns, "tl") == "studio-local"
    assert _host(turns, "tn") is None
    assert _host(turns, "tx") == "keep"
    assert _host(turns, "tg") is None
    assert _host(turns, "ta") is None
