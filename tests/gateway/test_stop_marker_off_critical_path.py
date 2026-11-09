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
import json
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
    # A skipped write proves nothing durable about the replacement route, so the intent stays
    # (Prism r3 c-fef208c96fa65cce); the next boot drops it once the new route is on disk.
    assert list((tmp_path / "sessions" / "user_stop_intents").glob("*.json"))
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


# --- Prism r2 (round 2 on #165) repros ------------------------------------------------------


def test_older_clear_keeps_a_newer_stops_intent_across_a_crash(tmp_path, monkeypatch):
    """Prism r2 P1 2839625d4dbd: a clear at generation 10 runs after the generation-11 /stop
    wrote its intent but before that stop's marker raised the fence. The clear must not delete
    the newer stop's only durable record; a crash then must still find the session stopped."""
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    key = entry.session_key
    assert store.mark_resume_pending(key, "shutdown_timeout")
    path = store.write_user_stop_intent(key, entry.session_id, boot_id="b1", generation=11)
    store.clear_user_stopped(key, generation=10)  # the queued older resume lands now
    assert path.exists(), "an older clear deleted a newer /stop's intent"
    # SIGKILL before the generation-11 marker commits: the next boot reads only the disk.
    reloaded = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    revived = reloaded.get_or_create_session(_source())
    assert revived.user_stopped_at is not None
    assert revived.resume_pending is False
    # A clear at the stop's generation or newer does retire it (negative control).
    store.clear_user_stopped(key, generation=11)
    assert not path.exists()
    db.close()


def test_clear_retires_an_intent_left_by_a_previous_process(tmp_path, monkeypatch):
    """Run generations restart with the process: an intent written by a previous process is
    superseded by any clear in this one, whatever its generation number."""
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    old = runner.session_store
    entry = old.get_or_create_session(_source())
    path = old.write_user_stop_intent(entry.session_key, entry.session_id, generation=500)
    fresh = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    assert fresh.get_or_create_session(_source()).user_stopped_at is not None  # applied at load
    fresh.clear_user_stopped(entry.session_key, generation=3)
    assert not path.exists()
    db.close()


def _fail_dir_fsync(monkeypatch, directory):
    import errno
    import os as _os
    real_fsync = _os.fsync
    target = None

    def _fsync(fd):
        nonlocal target
        if target is None and directory.exists():
            target = _os.stat(directory).st_ino
        st = _os.fstat(fd)
        import stat as _stat
        if _stat.S_ISDIR(st.st_mode) and st.st_ino == target:
            raise OSError(errno.EIO, "injected EIO on intent dir fsync")
        return real_fsync(fd)

    monkeypatch.setattr(_os, "fsync", _fsync)


@pytest.mark.asyncio
async def test_unconfirmed_intent_dir_fsync_falls_back_to_waiting_for_the_marker(
    tmp_path, monkeypatch, caplog,
):
    """Prism r2 P1 0cb938c88f46: a directory fsync failure means the intent's rename is not
    durable. The writer must raise (and leave no intent), and /stop must wait for the marker
    write as before #165 instead of acking on an unconfirmed intent."""
    monkeypatch.delenv("HERMES_RESUME_INTERRUPTED_TURNS", raising=False)
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    key = entry.session_key
    _seed(db, entry, _MID_TOOL_TAIL)
    intents = tmp_path / "sessions" / "user_stop_intents"
    intents.mkdir(parents=True, exist_ok=True)
    _fail_dir_fsync(monkeypatch, intents)
    with pytest.raises(OSError):
        store.write_user_stop_intent(key, entry.session_id)
    assert not list(intents.glob("*.json")), "an unconfirmed intent was left behind"

    runner._running_agents[key] = _HungAgent()
    _warm_up()
    writer = _OtherWriter(store, "store_lock", 1.5)
    writer.start()
    assert writer.held.wait(2.0)
    caplog.set_level(logging.INFO)
    t0 = time.monotonic()
    await runner._busy_stop_command(MessageEvent(text="/stop", source=_source(), user_id="u1"),
                                    key, _source())
    ack_s = time.monotonic() - t0
    print(f"[t_41843f70] dir-fsync EIO: /stop ack {ack_s:.3f}s (waited for the marker)")
    stages = [m for m in caplog.messages if "PHASE=stop_stages" in m]
    assert stages and "intent=0" in stages[-1] and "marker_deferred=0" in stages[-1], stages
    assert ack_s >= 1.0, "acked before the marker write landed with no durable intent"
    # The marker is on disk at ack time.
    reloaded = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    assert reloaded.get_or_create_session(_source()).user_stopped_at is not None
    writer.join(5.0)
    db.close()


def test_pending_stop_does_not_block_a_new_session_or_a_resumed_turn(tmp_path, monkeypatch):
    """Prism r2 P1 ed0ce609a190: a pending /stop receipt belongs to the session and generation
    it was taken for. /new (another session on the key) and a turn the user resumed at a newer
    generation must still be resume-markable while the old write is in flight."""
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    key, sid_a = entry.session_key, entry.session_id
    store.note_user_stop_pending(key, sid_a, 5)
    assert store.mark_resume_pending(key, "restart_interrupted") is False  # same session: refused

    # /new: the key now routes to session B; A's write is still pending.
    store._entries[key] = dataclasses.replace(store._entries[key], session_id="sess-b")
    assert store.user_stop_pending(key) is True
    assert store.mark_resume_pending(key, "restart_interrupted") is True
    store._entries[key].resume_pending = False
    assert store.suspend_recently_active(max_age_seconds=3600) == 1

    # Back on session A: the user spoke again at generation 6, fencing the stop out.
    store._entries[key] = dataclasses.replace(store._entries[key], session_id=sid_a,
                                              resume_pending=False)
    assert store.mark_resume_pending(key, "restart_interrupted") is False
    store.clear_user_stopped(key, generation=6)
    assert store.mark_resume_pending(key, "restart_interrupted") is True
    store.release_user_stop_pending(key, sid_a, 5)
    assert not store.user_stop_pending(key)
    db.close()


def test_fallback_routing_load_keeps_the_intent_and_applies_it_after_reconcile(
    tmp_path, monkeypatch,
):
    """Prism r2 P1 f8d11269d797: state.db routes K -> B (resume_pending) while sessions.json
    still says K -> A. A boot whose state.db routing read fails loads the fallback; the stop
    intent for B must survive that load and be applied once the authoritative rows merge."""
    import hermes_state as _hs

    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    key, sid_b = entry.session_key, entry.session_id
    assert store.mark_resume_pending(key, "shutdown_timeout")
    store.drain_sessions_json_writes(timeout=5.0)
    stale = dict(store._entries[key].to_dict(), session_id="sess-a-predecessor",
                 resume_pending=False)
    (tmp_path / "sessions" / "sessions.json").write_text(json.dumps({key: stale}), encoding="utf-8")
    path = store.write_user_stop_intent(key, sid_b, boot_id="b1")

    real_load = _hs.SessionDB.load_gateway_routing_entries
    state = {"fail": True}

    def _load(self, *a, **k):
        if state["fail"]:
            raise OSError("injected state.db routing read failure")
        return real_load(self, *a, **k)

    monkeypatch.setattr(_hs.SessionDB, "load_gateway_routing_entries", _load)
    reloaded = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    with reloaded._lock:
        reloaded._ensure_loaded_locked()
        assert reloaded._routing_db_loaded is False  # fallback-only load
        assert reloaded._entries[key].session_id == "sess-a-predecessor"
    assert path.exists(), "the fallback load deleted the only durable stop evidence"

    state["fail"] = False
    with reloaded._lock:
        reloaded._ensure_loaded_locked()  # reconciles with the authoritative rows
        revived = reloaded._entries[key]
        assert reloaded._routing_db_loaded is True
        assert revived.session_id == sid_b
        assert revived.user_stopped_at is not None, "the acked /stop was lost after reconcile"
        assert revived.resume_pending is False
    db.close()


# --- Prism r3 (round 3 on #165, Apollo send-back @ 6dfdeb5227) repros -----------------------


def _record_dir_fsyncs(monkeypatch) -> list:
    """Patch os.fsync to record the inode of every directory fsync'd (and still fsync it)."""
    import os as _os
    import stat as _stat

    real_fsync = _os.fsync
    seen: list = []

    def _fsync(fd):
        st = _os.fstat(fd)
        if _stat.S_ISDIR(st.st_mode):
            seen.append(st.st_ino)
        return real_fsync(fd)

    monkeypatch.setattr(_os, "fsync", _fsync)
    return seen


def test_intent_write_completes_a_short_write_and_fails_on_no_progress(tmp_path, monkeypatch):
    """Prism r3 P1 c-2dc0ab551bad124b: one ``os.write`` may write part of the payload. The intent
    must hold the whole payload, and a write that makes no progress must raise (and leave no
    intent), so /stop falls back to waiting for the marker."""
    import os as _os

    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entry = store.get_or_create_session(_source())
    real_write = _os.write
    monkeypatch.setattr(_os, "write", lambda fd, data: real_write(fd, bytes(data[:16])))
    path = store.write_user_stop_intent(entry.session_key, entry.session_id, generation=7)
    data = json.loads(path.read_text(encoding="utf-8-sig"))  # truncated JSON before the fix
    assert data["session_id"] == entry.session_id and data["generation"] == 7

    monkeypatch.setattr(_os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        store.write_user_stop_intent(entry.session_key, entry.session_id)
    left = [p.name for p in path.parent.iterdir() if p != path]
    assert left == [], f"a failed intent write left {left}"


def test_intent_write_fsyncs_the_parent_of_a_directory_it_creates(tmp_path, monkeypatch):
    """Prism r3 P1 c-8ff907760b0b3857: the first /stop creates ``user_stop_intents``. Its entry in
    ``sessions_dir`` is durable only once ``sessions_dir`` itself is fsync'd."""
    import os as _os

    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entry = store.get_or_create_session(_source())
    sessions_dir = tmp_path / "sessions"
    assert not (sessions_dir / "user_stop_intents").exists()
    seen = _record_dir_fsyncs(monkeypatch)
    store.write_user_stop_intent(entry.session_key, entry.session_id)
    assert _os.stat(sessions_dir / "user_stop_intents").st_ino in seen
    assert _os.stat(sessions_dir).st_ino in seen, "the new intent directory's parent was not fsync'd"


def test_intent_retirement_fsyncs_the_directory(tmp_path, monkeypatch):
    """Prism r3 P1 c-1efe0292ac7e504a: an unlink a crash can undo brings a retired intent back,
    and the next boot re-stops a session the user resumed. Both retire paths fsync the dir."""
    import os as _os

    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entry = store.get_or_create_session(_source())
    intents = tmp_path / "sessions" / "user_stop_intents"
    first = store.write_user_stop_intent(entry.session_key, entry.session_id)
    seen = _record_dir_fsyncs(monkeypatch)
    store.retire_user_stop_intent(first)
    assert not first.exists()
    assert _os.stat(intents).st_ino in seen, "retire_user_stop_intent: no directory fsync"

    second = store.write_user_stop_intent(entry.session_key, entry.session_id)
    seen.clear()
    store.clear_user_stopped(entry.session_key)  # -> _retire_user_stop_intents_for_key
    assert not second.exists()
    assert _os.stat(intents).st_ino in seen, "_retire_user_stop_intents_for_key: no directory fsync"


def test_stop_landing_after_compression_marks_the_continuation(tmp_path, monkeypatch):
    """Prism r3 P1 c-d9f504b2a0758b66: compression rotates the key's session id while the
    deferred marker write is queued. The compression tip of the stopped id is the same
    conversation, so the stop must land on it (fail-closed rowid), and a boot that applies a
    surviving intent must honour it too. /new (an unrelated id) still does not inherit it."""
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    entry = store.get_or_create_session(_source())
    key, tip = entry.session_key, entry.session_id
    pre = "sess-before-compression"
    tips = {pre: tip}
    monkeypatch.setattr(SessionStore, "_compression_tip_for_session_id",
                        lambda self, sid: tips.get(sid, sid))
    assert store.mark_resume_pending(key, "shutdown_timeout")

    timings: dict = {}
    assert store.mark_user_stopped(key, expected_session_id=pre, last_message_id=41,
                                   generation=3, timings=timings) is True
    assert store._entries[key].user_stopped_at is not None, "a stop during compression was dropped"
    assert store._entries[key].user_stopped_message_id is None  # pre-compression rowid: fail closed
    assert store._entries[key].resume_pending is False
    assert timings["outcome"] == "written"

    # Boot path: an intent written for the pre-compression id survives a crash.
    store.clear_user_stopped(key)
    store.write_user_stop_intent(key, pre, generation=9)
    reloaded = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    assert reloaded.get_or_create_session(_source()).user_stopped_at is not None

    # Negative control: an unrelated id (what /new routes to) is still skipped.
    other: dict = {}
    assert store.mark_user_stopped(key, expected_session_id="sess-unrelated", generation=10,
                                   timings=other) is False
    assert other["outcome"] == "skipped_route"


@pytest.mark.asyncio
async def test_skipped_marker_write_keeps_the_intent_for_the_next_boot(tmp_path, monkeypatch):
    """Prism r3 P1 c-fef208c96fa65cce: a write skipped because the key routes elsewhere is not
    proof the replacement is durable (``_replace_route_locked`` publishes before its deferred
    save). The intent must survive, so a crash before the replacement lands still boots the
    stopped session as stopped. A written marker does retire it (positive control)."""
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    entry = store.get_or_create_session(_source())
    key = entry.session_key
    intents = tmp_path / "sessions" / "user_stop_intents"

    real_mark = runner._mark_user_stopped

    async def _mark_after_new(session_key, **kwargs):
        # /new published in memory only (its deferred save has not landed) before the write runs.
        store._entries[key] = dataclasses.replace(store._entries[key], session_id="sess-after-new")
        return await real_mark(session_key, **kwargs)

    monkeypatch.setattr(runner, "_mark_user_stopped", _mark_after_new)
    await runner._mark_user_stopped_bounded(key)
    await _settle_pending_writes(runner)
    assert list(intents.glob("*.json")), "a skipped marker write retired the only durable stop"
    # Crash now: disk still routes the key to the stopped session; the boot must refuse it.
    reloaded = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    assert reloaded.get_or_create_session(_source()).user_stopped_at is not None

    # Positive control: a written marker retires its intent.
    monkeypatch.setattr(runner, "_mark_user_stopped", real_mark)
    store._entries[key] = dataclasses.replace(store._entries[key], session_id=entry.session_id)
    store.clear_user_stopped(key)
    assert not list(intents.glob("*.json"))
    await runner._mark_user_stopped_bounded(key)
    await _settle_pending_writes(runner)
    assert store._entries[key].user_stopped_at is not None
    assert not list(intents.glob("*.json")), "a written marker left its intent behind"
    db.close()


@pytest.mark.asyncio
async def test_intent_write_runs_off_the_event_loop(tmp_path, monkeypatch, caplog):
    """Prism r3 P2 c-8f4218c9f5df3ee0: the intent's mkdir/write/fsync/rename are blocking I/O and
    must not run on the event loop thread; their time is still reported as intent_s."""
    runner, _adapter, db = _runner(tmp_path, monkeypatch)
    store = runner.session_store
    key = store.get_or_create_session(_source()).session_key
    real_writer = store.write_user_stop_intent
    threads: list = []

    def _writer(*args, **kwargs):
        threads.append(threading.current_thread())
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(store, "write_user_stop_intent", _writer)
    frag = await runner._mark_user_stopped_bounded(key)
    await _settle_pending_writes(runner)
    assert threads, "the intent writer was not called"
    assert threads[0] is not threading.main_thread(), "intent I/O ran on the event loop thread"
    assert "intent=1" in frag and "intent_s=" in frag, frag
    db.close()
