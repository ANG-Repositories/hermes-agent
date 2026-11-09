"""/stop acks and frees the slot while the user_stopped write is still queued (t_41843f70).

Live 2026-11-08 (Studio default gateway): PHASE=stop_stages marker=12.75 s, store_s=12.748 on one
real Discord /stop. The marker's one-row UPSERT waits for the store's writers (``_lock``, then
``_save_lock``, which a whole-index rewrite holds across its state.db replace and the synchronous
sessions.json mirror), and /stop awaited it before acking. Ace waited ~18 s.

The boot-gate ruling stands: the marker is durable before a restart can resume the session.
These tests drive the real ``_busy_stop_command`` over a real SessionStore + state.db with
another writer holding the store, and prove (a) the ack and slot release come back in < 1 s,
(b) no resume mark lands while the write is in flight, (c) graceful shutdown lands the write
before the next boot reads the store, and the boot gate refuses the session.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
import time

import pytest

import gateway.run_shutdown as run_shutdown
from agent.i18n import t
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionStore
from hermes_state import AsyncSessionDB, SessionDB
from hermes_cli.plugins import invoke_hook
from tests.gateway.restart_test_helpers import make_restart_runner
from tests.gateway.test_user_stopped_never_resumes import (
    _MID_TOOL_TAIL,
    _HungAgent,
    _noop_resume_event,
    _runner,
    _seed,
    _source,
)


class _OtherWriter(threading.Thread):
    """Holds one of the store's writer locks, as a long full-index rewrite would."""

    def __init__(self, store: SessionStore, which: str, hold_s: float) -> None:
        super().__init__(daemon=True)
        self._lock = store._lock if which == "store_lock" else store._save_lock
        self._hold_s = hold_s
        self.held = threading.Event()
        self.release = threading.Event()

    def run(self) -> None:
        self._lock.acquire()
        try:
            self.held.set()
            self.release.wait(self._hold_s)
        finally:
            self._lock.release()


@pytest.mark.parametrize("which,hold_s", [("store_lock", 10.0), ("save_lock", 10.0)])
@pytest.mark.asyncio
async def test_stop_acks_under_a_held_store_writer_and_marker_lands_before_restart(
    tmp_path, monkeypatch, caplog, which, hold_s,
):
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    key = entry.session_key
    _seed(db, entry, _MID_TOOL_TAIL)
    last_id = db.get_messages(entry.session_id)[-1]["id"]
    # A pre-existing restart hedge: exactly what the marker write must retire before any boot.
    assert store.mark_resume_pending(key, "shutdown_timeout")
    runner._running_agents[key] = _HungAgent()
    # A running gateway has its plugins and catalog loaded at boot; the first invoke_hook /
    # t() in a fresh test process discovers every bundled plugin (0.2-1 s, measured), which
    # is not the seam under test.
    invoke_hook("agent_loop_stopped", session_key="warm-up", platform="", reason="",
                invalidation_reason="")
    t("gateway.stop.stopped")

    writer = _OtherWriter(store, which, hold_s)
    writer.start()
    assert writer.held.wait(2.0)
    event = MessageEvent(text="/stop", source=_source(), user_id="u1")
    event.invoke_timing = {"at": time.monotonic() - 0.25, "auth": 0.01, "defer": 0.20}
    try:
        caplog.set_level(logging.INFO, logger="gateway.run")  # whole test: the marker logs late
        t0 = time.monotonic()
        reply = await runner._busy_stop_command(event, key, _source())
        ack_s = time.monotonic() - t0
        print(f"[t_41843f70] {which} held {hold_s:.0f}s: /stop ack+release {ack_s:.3f}s")

        # (a) ack and slot release while the other writer still holds the store.
        assert ack_s < 1.0, f"/stop took {ack_s:.2f}s behind a held {which}"
        assert writer.is_alive()
        assert key not in runner._running_agents
        assert "stop" in str(getattr(reply, "text", reply)).lower()
        stages = [m for m in caplog.messages if "PHASE=stop_stages" in m]
        assert stages and "marker_deferred=1" in stages[-1], stages
        assert "intent=1" in stages[-1], stages[-1]
        assert "inv_lag=0.2" in stages[-1] and "inv_defer=0.20" in stages[-1], stages[-1]

        # (b) while the write is in flight, no in-process resume mark can land.
        assert store.user_stop_pending(key)
        resume_mark = asyncio.ensure_future(asyncio.to_thread(
            store.mark_resume_pending, key, "restart_interrupted"))

        # (c) a graceful restart now: shutdown flushes in-flight marker writes first.
        flush = asyncio.ensure_future(GatewayRunner._stop_flush_user_stop_writes(runner, "pre-drain"))
        await asyncio.sleep(0.2)
        assert not flush.done(), "the flush must wait for the in-flight marker write"
    finally:
        writer.release.set()  # the other writer finishes
    await asyncio.wait_for(flush, timeout=15.0)
    assert await asyncio.wait_for(resume_mark, timeout=15.0) is False
    assert not store.user_stop_pending(key)
    # The landed write retired its durable intent.
    assert not list((tmp_path / "sessions" / "user_stop_intents").glob("*.json"))
    stopped = [m for m in caplog.messages if "PHASE=user_stopped " in m]
    assert stopped and "marked=True" in stopped[-1], stopped
    print(f"[t_41843f70] {stopped[-1]}")
    waited = "lock_s" if which == "store_lock" else "save_lock_s"
    assert f"{waited}=" in stopped[-1] and f"{waited}=-" not in stopped[-1]

    # The next boot reads the marker from disk and refuses to resume the stopped turn.
    reloaded = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    revived = reloaded.get_or_create_session(_source())
    assert revived.user_stopped_at is not None
    assert revived.user_stopped_message_id == last_id
    assert revived.resume_pending is False
    runner2, adapter2 = make_restart_runner()
    runner2.config = runner.config
    runner2.session_store = reloaded
    reloaded_db = reloaded._db
    assert isinstance(reloaded_db, SessionDB)
    runner2._session_db = AsyncSessionDB(reloaded_db)
    runner2.adapters = {Platform.TELEGRAM: adapter2}
    monkeypatch.setattr(runner2, "_run_startup_resume_event", _noop_resume_event)
    forced = reloaded._entries[revived.session_key]
    forced.resume_pending = True
    forced.resume_reason = "shutdown_timeout"
    forced.last_resume_marked_at = None
    assert await runner2._prepare_boot_resume_work_check() == 1
    assert runner2._boot_resume_has_work[revived.session_key] is False
    assert runner2._schedule_resume_pending_sessions() == 0
    db.close()


def test_pending_stop_refuses_every_resume_mark_before_the_marker_is_published(tmp_path, monkeypatch):
    """The window the integration test cannot pin: /stop has acked, but the marker thread has not
    yet taken ``_lock`` (executor queue, held lock), so ``user_stopped_at`` is still unset."""
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    key = store.get_or_create_session(_source()).session_key
    store.note_user_stop_pending(key)
    assert store._entries[key].user_stopped_at is None
    assert store.mark_resume_pending(key, "restart_interrupted") is False
    assert store.suspend_recently_active(max_age_seconds=3600) == 0
    assert store._entries[key].resume_pending is False
    store.release_user_stop_pending(key)
    assert store.mark_resume_pending(key, "restart_interrupted") is True  # negative control
    db.close()


@pytest.mark.asyncio
async def test_shutdown_flushes_marker_writes_before_marking_and_before_closing_dbs(monkeypatch):
    """Wiring: the flush runs before the drain's resume marks and before the DBs close."""
    runner, _adapter = make_restart_runner()
    runner._restart_drain_timeout = 1.0
    calls: list[str] = []

    async def _flush(_timeout):
        calls.append("flush")
        return 0

    runner._flush_pending_user_stop_writes = _flush

    def _phase(name, sync=False):
        if sync:
            return lambda *_a, **_k: calls.append(name)

        async def _async(*_a, **_k):
            calls.append(name)
        return _async

    for name in ("_stop_begin_teardown", "_stop_drain_active_work", "_stop_interrupt_remaining_work",
                 "_stop_finalize_agents_and_adapters", "_stop_release_runtime_state",
                 "_stop_persist_exit_state"):
        monkeypatch.setattr(GatewayRunner, name, _phase(name))
    monkeypatch.setattr(GatewayRunner, "_stop_quiesce_and_close_session_dbs",
                        _phase("_stop_quiesce_and_close_session_dbs", sync=True))
    monkeypatch.setattr(run_shutdown, "effective_stop_drain_timeout", lambda _self: 1.0)

    await GatewayRunner._stop_impl(runner)

    assert calls.index("flush") < calls.index("_stop_drain_active_work")
    assert calls.index("_stop_release_runtime_state") < len(calls) - 1 - calls[::-1].index("flush")
    assert len(calls) - 1 - calls[::-1].index("flush") < calls.index("_stop_quiesce_and_close_session_dbs")


def _warm_up() -> None:
    invoke_hook("agent_loop_stopped", session_key="warm-up", platform="", reason="",
                invalidation_reason="")
    t("gateway.stop.stopped")


async def _settle_pending_writes(runner) -> None:
    pending = [t_ for t_ in getattr(runner, "_pending_user_stop_writes", ()) if not t_.done()]
    if pending:
        await asyncio.wait(pending, timeout=15.0)


@pytest.mark.asyncio
async def test_process_killed_after_ack_before_marker_lands_never_resumes(tmp_path, monkeypatch, caplog):
    """Prism r1 P1 3409598809cf repro: hedge on disk, marker write blocked past the 0.5 s bound,
    /stop acks, then the process dies before the write lands. The next boot reads only the disk,
    and must still refuse the stopped turn."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    key = entry.session_key
    _seed(db, entry, _MID_TOOL_TAIL)
    assert store.mark_resume_pending(key, "shutdown_timeout")
    on_disk = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    assert on_disk.get_or_create_session(_source()).resume_pending is True  # precondition
    runner._running_agents[key] = _HungAgent()
    _warm_up()

    writer = _OtherWriter(store, "store_lock", 10.0)
    writer.start()
    assert writer.held.wait(2.0)
    caplog.set_level(logging.INFO)
    try:
        t0 = time.monotonic()
        await runner._busy_stop_command(MessageEvent(text="/stop", source=_source(), user_id="u1"),
                                        key, _source())
        ack_s = time.monotonic() - t0
        print(f"[t_41843f70] kill-before-landing: /stop ack {ack_s:.3f}s")
        assert ack_s < 1.0
        assert any("marker_deferred=1" in m for m in caplog.messages if "PHASE=stop_stages" in m)

        # SIGKILL here: the marker write never lands. The next boot is a fresh store on the disk.
        reloaded = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
        revived = reloaded.get_or_create_session(_source())
        assert revived.user_stopped_at is not None, "a killed-before-landing /stop lost its stop"
        assert revived.user_stopped_message_id is None  # fail-closed in user_stop_blocks_resume
        assert revived.resume_pending is False
        assert any("PHASE=user_stop_intent_applied" in m for m in caplog.messages)

        runner2, adapter2 = make_restart_runner()
        runner2.config = runner.config
        runner2.session_store = reloaded
        runner2._session_db = AsyncSessionDB(reloaded._db)
        runner2.adapters = {Platform.TELEGRAM: adapter2}
        monkeypatch.setattr(runner2, "_run_startup_resume_event", _noop_resume_event)
        forced = reloaded._entries[revived.session_key]  # even a hedge that survives elsewhere
        forced.resume_pending = True
        forced.resume_reason = "shutdown_timeout"
        forced.last_resume_marked_at = None
        assert await runner2._prepare_boot_resume_work_check() == 1
        assert runner2._boot_resume_has_work[revived.session_key] is False
        assert runner2._schedule_resume_pending_sessions() == 0
    finally:
        writer.release.set()
    await _settle_pending_writes(runner)
    db.close()


@pytest.mark.asyncio
async def test_deferred_marker_write_is_bound_to_the_session_stop_saw(tmp_path, monkeypatch, caplog):
    """Prism r1 P1 79e1ef91bc79: a /new that re-routes the key before the deferred write lands
    must not inherit the stop."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    key = entry.session_key
    _seed(db, entry, _MID_TOOL_TAIL)
    runner._running_agents[key] = _HungAgent()
    _warm_up()

    writer = _OtherWriter(store, "store_lock", 10.0)
    writer.start()
    assert writer.held.wait(2.0)
    caplog.set_level(logging.INFO)
    try:
        await runner._busy_stop_command(MessageEvent(text="/stop", source=_source(), user_id="u1"),
                                        key, _source())
        assert any("marker_deferred=1" in m for m in caplog.messages if "PHASE=stop_stages" in m)
        # /new lands first: the key now routes to a fresh session.
        store._entries[key] = dataclasses.replace(store._entries[key], session_id="sess-after-new",
                                                  user_stopped_at=None)
    finally:
        writer.release.set()
    await _settle_pending_writes(runner)
    assert store._entries[key].session_id == "sess-after-new"
    assert store._entries[key].user_stopped_at is None, "the new session inherited the old /stop"
    assert any("Skipping user_stopped write" in m for m in caplog.messages)
    assert not list((tmp_path / "sessions" / "user_stop_intents").glob("*.json"))
    db.close()


def test_next_user_message_retires_the_stop_intent(tmp_path):
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entry = store.get_or_create_session(_source())
    path = store.write_user_stop_intent(entry.session_key, entry.session_id, boot_id="b1")
    assert path.exists()
    other = store.write_user_stop_intent("other-key", "other-sid")
    store.clear_user_stopped(entry.session_key)
    assert not path.exists()
    assert other.exists()  # only this key's intents


@pytest.mark.parametrize("live", [True, False])
def test_db_close_is_skipped_while_a_marker_write_is_live(monkeypatch, live):
    """Prism r1 P1 3f05587acc86: a pre-close flush that timed out leaves a marker write running
    on the default executor; the SessionDB close must not run under it."""
    runner, _adapter = make_restart_runner()
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        if not live:
            fut.set_result(True)
        runner._pending_user_stop_writes = {fut}
        monkeypatch.setattr(GatewayRunner, "_shutdown_executor", lambda _self, drain_timeout: 0)
        monkeypatch.setattr(runner, "_active_cron_job_count", lambda: 0, raising=False)
        monkeypatch.setattr(runner, "_active_api_worker_count", lambda: 0, raising=False)
        steps: list[str] = []
        monkeypatch.setattr(run_shutdown.GatewayShutdownMixin, "_quiet_step",
                            staticmethod(lambda label, _fn: steps.append(label)))
        ctx = run_shutdown.GatewayShutdownMixin._StopContext(deferred_count=lambda: 0,
                                                             started_at=time.monotonic())
        GatewayRunner._stop_quiesce_and_close_session_dbs(runner, 30.0, ctx)
        closed = any("close" in s for s in steps)
        assert closed is (not live), steps
    finally:
        loop.close()
