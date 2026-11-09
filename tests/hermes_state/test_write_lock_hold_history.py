"""Write-lock hold history, callback CPU vs wall, and waiter-side attribution (t_7fe48f0b).

2026-11-09: the per-pid ledger kept only each process's LATEST hold, so the holder of a 06:05-06:07
stall had been overwritten before anyone read it, and a 172 s hold of a one-row SELECT+UPDATE could
not say whether the thread was running. These tests pin:

- every recorded hold is appended to ``<ledger>/history.jsonl`` (op, pid, who, since, held_s,
  commit_s, callback_s, callback_cpu_s) and a past window is rebuilt by ``ledger_history``;
- the history is bounded (one rotation, one backup);
- ``callback_cpu_s`` is thread CPU: a sleeping callback reads wall >> cpu, a spinning one cpu ~ wall;
- a writer that waits WAITER_ATTRIBUTION_S logs the holder on its own side, for the in-process
  writer lock and for the SQLite lock, once per write;
- all of it fails open: a broken history or attribution never fails the write.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

import hermes_state_lockowners as lockowners
import hermes_state as sessiondb_mod
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def _record_everything(monkeypatch):
    monkeypatch.setattr(lockowners, "_last_timeout", {})
    monkeypatch.setattr(lockowners, "_HOLD_RECORD_MIN_S", 0.0)
    monkeypatch.setattr(lockowners, "state_db_write_lock_holders", lambda _p: [])  # ledger-only, any OS


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    d.close()


def _history(db_path):
    path = Path(lockowners._ledger_dir(db_path)) / lockowners._HISTORY_NAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _sleeping_write(db, seconds):
    def fixture_sleep_hold():  # the ledger names the method that opened the transaction
        db._execute_write(lambda conn: time.sleep(seconds))
    fixture_sleep_hold()


def test_every_recorded_hold_lands_in_history_and_a_window_is_rebuilt(db):
    t_start = time.time()
    _sleeping_write(db, 0.3)
    _sleeping_write(db, 0.1)  # overwrites <pid>.json; the history keeps both
    t_end = time.time()
    rows = [r for r in _history(db.db_path) if r["op"] == "fixture_sleep_hold"]
    assert len(rows) == 2, rows
    first = rows[0]
    for key in ("op", "pid", "who", "since", "held_s", "commit_s", "callback_s", "callback_cpu_s", "released_at"):
        assert key in first, (key, first)
    assert first["pid"] == os.getpid() and first["state"] == "released"
    assert first["callback_s"] >= 0.2 and first["held_s"] >= first["callback_s"], first
    latest = json.loads((Path(lockowners._ledger_dir(db.db_path)) / f"{os.getpid()}.json").read_text())
    assert latest["held_s"] < 0.3, "per-pid file holds only the latest hold"
    window = [r for r in lockowners.ledger_history(db.db_path, t_start, t_end) if r["op"] == "fixture_sleep_hold"]
    assert [r["seq"] for r in window] == [r["seq"] for r in rows]
    assert lockowners.ledger_history(db.db_path, t_end + 60, t_end + 120) == []
    print("history line:", json.dumps(first, sort_keys=True))


def test_callback_cpu_separates_a_parked_thread_from_a_running_one(db):
    _sleeping_write(db, 0.4)  # holds the lock without running: wall >> cpu

    def fixture_spin_hold():
        def _spin(conn):
            end = time.thread_time() + 0.3
            while time.thread_time() < end:
                pass
        db._execute_write(_spin)
    fixture_spin_hold()
    rows = {r["op"]: r for r in _history(db.db_path)}
    parked, running = rows["fixture_sleep_hold"], rows["fixture_spin_hold"]
    assert parked["callback_s"] >= 0.3 and parked["callback_cpu_s"] < 0.1, parked
    assert running["callback_cpu_s"] >= 0.25, running


def test_rolled_back_hold_counts_the_whole_hold_as_callback(db):
    def fixture_raising_hold():
        def _boom(conn):
            time.sleep(0.2)
            raise ValueError("fixture")
        db._execute_write(_boom)
    with pytest.raises(ValueError):
        fixture_raising_hold()
    row = [r for r in _history(db.db_path) if r["op"] == "fixture_raising_hold"][-1]
    assert "commit_s" not in row and row["callback_s"] == row["held_s"] and row["callback_cpu_s"] is not None, row


def test_history_is_bounded_to_one_rotation(db, monkeypatch):
    monkeypatch.setattr(lockowners, "_HISTORY_MAX_BYTES", 600)
    for _ in range(12):
        _sleeping_write(db, 0.0)
    ledger = Path(lockowners._ledger_dir(db.db_path))
    current, backup = ledger / "history.jsonl", ledger / "history.jsonl.1"
    assert backup.exists() and current.stat().st_size <= 600 and backup.stat().st_size <= 600
    assert sorted(p.name for p in ledger.iterdir() if p.name.startswith("history")) == ["history.jsonl",
                                                                                          "history.jsonl.1"]
    assert lockowners.ledger_history(db.db_path, 0, time.time() + 1), "both generations are readable"


def test_broken_history_never_fails_the_write(db, monkeypatch, tmp_path):
    ledger = Path(lockowners._ledger_dir(db.db_path))
    _sleeping_write(db, 0.0)  # creates the ledger dir
    (ledger / "blocked-history").mkdir()
    monkeypatch.setattr(lockowners, "_HISTORY_NAME", "blocked-history")  # open() -> EISDIR
    db.set_meta("k", "v")
    assert db.get_meta("k") == "v"


def _waiter_lines(caplog):
    return [r.getMessage() for r in caplog.records if "state.db write waiting" in r.getMessage()]


def test_writer_lock_waiter_names_the_in_process_holder(db, monkeypatch, caplog):
    """The gateway shape: two threads of one process share one SessionDB writer; the second queues on
    the in-process lock, never sees SQLITE_BUSY, and used to log nothing."""
    monkeypatch.setattr(sessiondb_mod, "WAITER_ATTRIBUTION_S", 0.9)
    started = threading.Event()

    def fixture_long_holder():
        def _hold(conn):
            started.set()
            time.sleep(2.0)
        db._execute_write(_hold)

    holder = threading.Thread(target=fixture_long_holder)
    holder.start()
    try:
        assert started.wait(5.0)
        with caplog.at_level(logging.WARNING, logger=lockowners.logger.name):
            db.set_meta("k", "after the holder")
    finally:
        holder.join(timeout=10.0)
    assert db.get_meta("k") == "after the holder"
    lines = _waiter_lines(caplog)
    assert len(lines) == 1, caplog.text
    assert "writer lock" in lines[0] and "fixture_long_holder" in lines[0] and "still holding" in lines[0], lines
    print("waiter line:", lines[0])


def test_sqlite_lock_waiter_logs_once_and_the_write_lands(db, monkeypatch, caplog):
    monkeypatch.setattr(sessiondb_mod, "WAITER_ATTRIBUTION_S", 0.4)
    db._conn.execute("PRAGMA busy_timeout=100")
    started = threading.Event()

    def _foreign_holder():
        conn = sqlite3.connect(str(db.db_path), timeout=1.0, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            started.set()
            time.sleep(1.5)
            conn.execute("COMMIT")
        finally:
            conn.close()

    holder = threading.Thread(target=_foreign_holder)
    holder.start()
    try:
        assert started.wait(5.0)
        with caplog.at_level(logging.WARNING, logger=lockowners.logger.name):
            db.set_meta("k2", "v2")
    finally:
        holder.join(timeout=10.0)
    assert db.get_meta("k2") == "v2"
    lines = _waiter_lines(caplog)
    assert len(lines) == 1 and "sqlite lock" in lines[0], caplog.text


def test_waiter_attribution_fails_open(db, monkeypatch):
    def _broken(*_a, **_k):
        raise RuntimeError("ledger unreadable")
    monkeypatch.setattr(lockowners, "describe_write_lock_holders", _broken)
    assert lockowners.log_write_lock_waiter(db.db_path, 12.0, lock="writer") == ""
