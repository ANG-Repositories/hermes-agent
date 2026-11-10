"""A slow /stop marker write names the state.db writer it waited behind (t_41843f70).

Live 2026-11-08 21:41: ``PHASE=user_stopped store_s=12.748``. Measured locally, the marker UPSERT
itself costs milliseconds; a multi-second wait is the time another writer held state.db's write lock
(same SessionDB: its ``_lock`` serialises the whole transaction; another process: BEGIN IMMEDIATE).
The marker stays on disk before /stop acks (boot-gate ruling); these tests lock that the wait is
attributed (``lock_s`` / ``save_lock_s`` / ``upsert_s``), that the holder is named, that the platform
invoke lag reaches the stage line, and that the write is bound to the session /stop saw.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
import hermes_state
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore, build_session_key
from hermes_state import AsyncSessionDB, SessionDB
from tests.gateway.restart_test_helpers import make_restart_runner


def _source() -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, chat_id="123", chat_type="dm", user_id="u1")


def _runner(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    runner, _adapter = make_restart_runner()
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    runner.session_store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    db = runner.session_store._db
    assert isinstance(db, SessionDB)
    runner._session_db = AsyncSessionDB(db)
    return runner, db


def _hold_write_lock(db: SessionDB, seconds: float) -> threading.Thread:
    """Another writer on the SAME SessionDB holds the write transaction for ``seconds``."""
    started = threading.Event()

    def _holder():
        def fn(_conn):
            started.set()
            time.sleep(seconds)
        db._execute_write(fn)

    th = threading.Thread(target=_holder, daemon=True)
    th.start()
    assert started.wait(5.0)
    return th


def _field(line: str, name: str) -> float:
    m = re.search(rf"\b{name}=([0-9.]+)", line)
    assert m, (name, line)
    return float(m.group(1))


async def _wait_for_log(caplog, needle: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for r in caplog.records:
            if needle in r.getMessage():
                return r.getMessage()
        await asyncio.sleep(0.05)
    raise AssertionError(f"no {needle!r} log line within {timeout}s")


@pytest.mark.asyncio
async def test_slow_marker_write_is_attributed_and_still_durable_before_return(tmp_path, monkeypatch, caplog):
    runner, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    db.create_session(entry.session_id, "gateway", session_key=entry.session_key)

    holder = _hold_write_lock(db, 2.5)
    with caplog.at_level(logging.INFO, logger="gateway.run"):
        t0 = time.monotonic()
        assert await runner._mark_user_stopped(entry.session_key) is True
        took = time.monotonic() - t0
        # Durable before return: a fresh store on the same files sees the marker.
        reloaded = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
        assert reloaded.get_or_create_session(_source()).user_stopped_at is not None
        slow = await _wait_for_log(caplog, "PHASE=user_stopped_slow")
    holder.join(5.0)

    line = next(r.getMessage() for r in caplog.records if "PHASE=user_stopped " in r.getMessage())
    print(f"\nmeasured: took={took:.3f}s | {line} | {slow}")
    assert took >= 2.0, took
    # The wait is the state.db write lock (SessionDB._lock + BEGIN IMMEDIATE), which upsert_s
    # spans; the store's own locks were free.
    assert _field(line, "upsert_s") >= 2.0, line
    assert _field(line, "lock_s") < 0.5 and _field(line, "save_lock_s") < 0.5, line
    assert "held the write lock" in slow, slow


@pytest.mark.asyncio
async def test_fast_marker_write_names_no_holder(tmp_path, monkeypatch, caplog):
    runner, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())
    db.create_session(entry.session_id, "gateway", session_key=entry.session_key)

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        assert await runner._mark_user_stopped(entry.session_key) is True
        await asyncio.sleep(0.3)

    line = next(r.getMessage() for r in caplog.records if "PHASE=user_stopped " in r.getMessage())
    print(f"\nmeasured: {line}")
    assert _field(line, "store_s") < GatewayRunner._USER_STOP_SLOW_S, line
    assert not [r for r in caplog.records if "PHASE=user_stopped_slow" in r.getMessage()]


@pytest.mark.asyncio
async def test_marker_write_is_bound_to_the_session_stop_saw(tmp_path, monkeypatch):
    runner, db = _runner(tmp_path, monkeypatch)
    entry = runner.session_store.get_or_create_session(_source())

    # /new landed between the stop and its write: the key routes to another session now.
    assert await runner._mark_user_stopped(
        entry.session_key, expected_session_id="some-older-session") is False
    assert runner.session_store._entries[entry.session_key].user_stopped_at is None

    assert await runner._mark_user_stopped(entry.session_key, expected_session_id=entry.session_id) is True
    assert runner.session_store._entries[entry.session_key].user_stopped_at is not None


class _FakeAdapter:
    def __init__(self):
        self._pending_messages = {}
        self._active_sessions = {}

    async def send(self, chat_id, text, **kwargs):
        pass

    async def interrupt_session_activity(self, session_key, chat_id):
        pass


@pytest.mark.asyncio
async def test_stop_stages_carry_store_substages_and_invoke_lag(caplog):
    source = _source()
    key = build_session_key(source)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    runner.adapters = {Platform.TELEGRAM: _FakeAdapter()}
    runner._running_agents, runner._running_agents_ts, runner._running_agent_tasks = {}, {}, {}
    runner._pending_messages, runner._background_tasks = {}, set()
    runner._session_db = None
    seen = []

    def _mark(session_key, **kwargs):
        seen.append(kwargs)
        kwargs["timings"].update(lock_s=0.001, save_lock_s=0.002, upsert_s=0.003)
        return True

    runner.session_store = SimpleNamespace(
        _entries={key: SimpleNamespace(session_id="sess-a")}, mark_user_stopped=_mark)
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    agent = MagicMock()
    agent.session_id = "sess-a"
    runner._running_agents[key] = agent
    runner._session_state(key).turn.started_ts = time.time()

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        await runner._interrupt_and_clear_session(
            key, source, interrupt_reason="stop", invalidation_reason="stop_command",
            invoke_timing={"at": time.monotonic() - 5.3, "auth": 0.2, "defer": 5.0})

    lines = [r.getMessage() for r in caplog.records if "PHASE=stop_stages" in r.getMessage()]
    assert len(lines) == 1, lines
    for field in ("lock_s=0.001", "save_lock_s=0.002", "upsert_s=0.003", "inv_auth=0.20", "inv_defer=5.00"):
        assert field in lines[0], (field, lines[0])
    assert 5.2 <= _field(lines[0], "inv_lag") <= 7.0, lines[0]
    assert seen and seen[0].get("expected_session_id") == "sess-a", seen
