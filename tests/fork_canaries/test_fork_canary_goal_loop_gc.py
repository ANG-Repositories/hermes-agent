"""Fork canary: goal/loop orphan GC (t_92fc3ff0).

A ``/goal`` or ``/loop`` row is keyed by session id. On 2026-11-08 the Studio's default profile held
205 active goals; 190 sat on session ids no gateway routing key pointed at (oldest 2026-05-12). They
never woke anyone, yet every 15 s idle-wake scan reloaded them. Cause: the gateway's reset
rotations (``/new``, ``/reset``, a suspended auto-reset, the compression-exhausted reset) all end the
old session through ``SessionStore._finish_route_transition`` and nothing retired its goal/loop.

Contracts guarded here (behavior, not snapshots):
1. A reset rotation retires the old session's ACTIVE goal and loop as ``superseded`` (the transcript
   is dropped, so the goal never carried; ``/goal resume`` restores) and never touches a non-active row.
2. The hourly reaper retires (``orphaned``) an active goal/loop only when its session is unrouted AND
   idle >= ``goals.orphan_gc_days``; recent session-store activity (a CLI session) and routing protect it;
   ``0`` disables it.
3. Routing is read gateway-wide, so a goal routed only under a secondary profile is kept, while that
   profile's own store is still reaped.
4. Retire, never delete: an orphaned goal/loop round-trips to active through the resume path.
5. The reaper rides the goal idle-wake watcher (no new thread or job), throttled to once per hour.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_cli import goals, loops
from hermes_state import SessionDB

FEATURE = "goal-loop-orphan-gc"
DAY = 86400.0


def _db(path) -> SessionDB:
    return SessionDB(db_path=path)


def _put_goal(db, sid, *, status="active", age_days=0.0, now=None):
    now = time.time() if now is None else now
    state = goals.GoalState(goal=f"goal for {sid}", status=status, created_at=now - age_days * DAY)
    db.set_meta(f"goal:{sid}", state.to_json())


def _put_loop(db, sid, *, status="active", age_days=0.0, now=None):
    now = time.time() if now is None else now
    state = loops.LoopState(prompt=f"tick {sid}", status=status, interval_seconds=300.0,
                            created_at=now - age_days * DAY)
    db.set_meta(f"loop:{sid}", state.to_json())


def _goal(db, sid):
    raw = db.get_meta_committed(f"goal:{sid}")
    return goals.GoalState.from_json(raw) if raw else None


def _loop(db, sid):
    raw = db.get_meta_committed(f"loop:{sid}")
    return loops.LoopState.from_json(raw) if raw else None


def _session_row(db, sid, *, last_active):
    """A sessions row whose start, last message and effective_last_active all read *last_active*."""
    db.create_session(session_id=sid, source="cli")

    def _txn(conn):
        conn.execute("UPDATE sessions SET started_at = ?, effective_last_active = ? WHERE id = ?",
                     (last_active, last_active, sid))
        conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'user', 'hi', ?)",
                     (sid, last_active))

    db._execute_write(_txn)


@pytest.fixture
def store_rig(tmp_path, monkeypatch):
    """Real SessionStore over a real SessionDB; the goals/loops module store is the same file."""
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    if store._db is not None:
        store._db.close()
    db = _db(tmp_path / "state.db")
    store._db = db
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="4242", chat_type="dm", user_id="4242")
    yield store, db, source
    db.close()


# ── 1. Rotation leak ──────────────────────────────────────────────────

def test_new_reset_retires_the_replaced_goal_and_loop(store_rig):
    store, db, source = store_rig
    a = store.get_or_create_session(source)
    _put_goal(db, a.session_id)
    _put_loop(db, a.session_id)

    b = store.reset_session(a.session_key)  # the /new and /reset store call
    assert b is not None and b.session_id != a.session_id

    # RED-proof (M1): delete the `self._retire_replaced_goal_and_loop(...)` call in
    # gateway/session_recovery.py::_finish_route_transition (~line 414) -> both rows stay "active".
    g, lp = _goal(db, a.session_id), _loop(db, a.session_id)
    assert g.status == "superseded" and "superseded" in (g.paused_reason or "")
    assert lp.status == "superseded"
    assert a.session_id not in goals.list_active_goal_session_ids()
    assert a.session_id not in {sid for sid, _ in loops.list_active_loops()}
    # A fresh start never carried the goal (existing /new semantics): the successor has none.
    assert _goal(db, b.session_id) is None and _loop(db, b.session_id) is None


def test_suspended_auto_reset_retires_the_replaced_goal(store_rig):
    """The automatic rotation (fork has no time-based expiry; ``suspended`` is the auto-reset)."""
    store, db, source = store_rig
    a = store.get_or_create_session(source)
    _put_goal(db, a.session_id)
    assert store.suspend_session(a.session_key)

    b = store.get_or_create_session(source)
    assert b.session_id != a.session_id and b.auto_reset_reason == "suspended"
    # RED-proof (M1): same edit as above; the auto-reset reaches the same funnel.
    assert _goal(db, a.session_id).status == "superseded"
    assert a.session_id not in goals.list_active_goal_session_ids()


@pytest.mark.parametrize("status", ["done", "paused", "cleared"])
def test_reset_never_touches_a_non_active_goal(store_rig, status):
    """Ace 2026-10-19: a done or /stop-latched goal never wakes; retirement only ever reads ACTIVE."""
    store, db, source = store_rig
    a = store.get_or_create_session(source)
    _put_goal(db, a.session_id, status=status)
    store.reset_session(a.session_key)
    # RED-proof (M6): in hermes_cli/goals.py::retire_goal (~line 877) drop
    # `or state.status != "active"` -> done/paused/cleared rows are re-stamped "superseded".
    assert _goal(db, a.session_id).status == status


# ── 2. Reaper ─────────────────────────────────────────────────────────

def test_orphaned_ids_pure_contract():
    now = 1_800_000_000.0
    activity = {"old-unrouted": now - 8 * DAY, "fresh-unrouted": now - 1 * DAY, "old-routed": now - 30 * DAY,
                "exactly-7d": now - 7 * DAY}
    # RED-proof (M2): drop `and (last or 0.0) <= cutoff` in
    # hermes_cli/goals.py::orphaned_goal_and_loop_ids (~line 899) -> "fresh-unrouted" is returned.
    assert goals.orphaned_goal_and_loop_ids(now, {"old-routed"}, activity, gc_days=7) == ["exactly-7d", "old-unrouted"]
    assert goals.orphaned_goal_and_loop_ids(now, set(), activity, gc_days=0) == []


def test_reaper_retires_only_unrouted_and_idle(tmp_path):
    db = _db(tmp_path / "state.db")
    now = time.time()
    try:
        _put_goal(db, "old-unrouted", age_days=8, now=now)
        _put_loop(db, "old-unrouted", age_days=8, now=now)
        _session_row(db, "old-unrouted", last_active=now - 8 * DAY)
        # CLI-like: the goal row is old, but the session store saw a message yesterday.
        _put_goal(db, "cli-recent", age_days=30, now=now)
        _session_row(db, "cli-recent", last_active=now - 1 * DAY)
        _put_goal(db, "routed-idle", age_days=30, now=now)
        _put_loop(db, "routed-idle", age_days=30, now=now)
        _put_goal(db, "old-done", status="done", age_days=60, now=now)

        counts = goals.reap_orphaned_goals_and_loops({"routed-idle"}, gc_days=7, now=now, db=db)

        assert counts == {"scanned": 3, "orphaned": 1, "loops": 1}
        g = _goal(db, "old-unrouted")
        assert g.status == "orphaned" and "orphan-gc" in g.paused_reason
        assert time.strftime("%Y-%m-%d", time.localtime(now)) in g.paused_reason
        assert _loop(db, "old-unrouted").status == "orphaned"
        # RED-proof (M3): in hermes_cli/goals.py::_goal_loop_session_activity (~line 934) replace
        # `activity[sid] = max(activity[sid], ...)` with `pass` -> "cli-recent" is reaped.
        assert _goal(db, "cli-recent").status == "active"
        assert _goal(db, "routed-idle").status == "active" and _loop(db, "routed-idle").status == "active"
        assert _goal(db, "old-done").status == "done"   # never revived, never re-stamped
    finally:
        db.close()


def test_reaper_disabled_at_zero_days(tmp_path):
    db = _db(tmp_path / "state.db")
    try:
        _put_goal(db, "ancient", age_days=365)
        assert goals.reap_orphaned_goals_and_loops(set(), gc_days=0, db=db) == {"scanned": 0, "orphaned": 0, "loops": 0}
        assert _goal(db, "ancient").status == "active"
    finally:
        db.close()


# ── 4. Retire, never delete ───────────────────────────────────────────

def test_orphaned_goal_and_loop_round_trip_through_resume(tmp_path, monkeypatch):
    db = _db(tmp_path / "state.db")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    try:
        _put_goal(db, "s", age_days=9)
        _put_loop(db, "s", age_days=9)
        assert goals.reap_orphaned_goals_and_loops(set(), gc_days=7, db=db)["orphaned"] == 1

        # RED-proof (M9): in hermes_cli/goals.py::retire_goal (~line 882) replace the
        # compare_and_set_meta write with a DELETE of the row -> resume() has nothing to restore.
        mgr = goals.GoalManager("s")
        assert mgr.resume() is not None
        assert _goal(db, "s").status == "active" and _goal(db, "s").paused_reason is None
        assert "s" in goals.list_active_goal_session_ids()

        lm = loops.LoopManager("s")
        assert lm.resume() is not None
        assert _loop(db, "s").status == "active"
    finally:
        db.close()


# ── 3 + 5. Gateway: multiplex scopes, throttle, watcher wiring, log line ──

_ACTIVE_STORE: contextvars.ContextVar = contextvars.ContextVar("t92_active_store", default=None)


@pytest.fixture
def gw_rig(tmp_path, monkeypatch):
    dbs = {None: _db(tmp_path / "root.db"), "sec": _db(tmp_path / "sec.db")}
    monkeypatch.setattr(goals, "_get_session_db", lambda: dbs[_ACTIVE_STORE.get()])

    @contextlib.asynccontextmanager
    async def scope_cm(profile_home):
        token = _ACTIVE_STORE.set("sec" if profile_home is not None else None)
        try:
            yield
        finally:
            _ACTIVE_STORE.reset(token)

    routed = SimpleNamespace(session_key="agent:sec:discord:channel:9", session_id="sec-routed")
    runner = object.__new__(GatewayRunner)
    runner.config = {"goals": {"orphan_gc_days": 7}}
    runner.session_store = SimpleNamespace(list_sessions=lambda: [routed])
    runner._run_in_executor_with_context = asyncio.to_thread

    async def _warm(_label):
        return None

    runner._warm_goals_session_db = _warm
    yield runner, dbs, scope_cm
    for db in dbs.values():
        db.close()


@pytest.mark.asyncio
async def test_goal_routed_only_under_a_secondary_profile_is_kept(gw_rig, caplog):
    runner, dbs, scope_cm = gw_rig
    _put_goal(dbs[None], "root-orphan", age_days=10)
    _put_goal(dbs["sec"], "sec-routed", age_days=30)
    _put_goal(dbs["sec"], "sec-orphan", age_days=10)

    with caplog.at_level(logging.INFO, logger="gateway.run"):
        totals = await runner._goal_orphan_gc([(None, None), ("sec", "/profiles/sec")], scope_cm)

    assert _goal(dbs["sec"], "sec-routed").status == "active"
    # The secondary store IS scanned (else the line above would pass vacuously). RED-proof (M5): in
    # gateway/run_goals.py::_goal_orphan_gc (~line 832) iterate `scopes[:1]` -> "sec-orphan" stays active.
    assert _goal(dbs["sec"], "sec-orphan").status == "orphaned"
    assert _goal(dbs[None], "root-orphan").status == "orphaned"
    assert totals == {"scanned": 3, "orphaned": 2, "loops": 0}
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("goal-gc:")]
    assert lines == ["goal-gc: scanned=3 orphaned=2 loops=0"]


@pytest.mark.asyncio
async def test_config_zero_disables_the_gateway_pass(gw_rig):
    runner, dbs, scope_cm = gw_rig
    runner.config = {"goals": {"orphan_gc_days": 0}}
    _put_goal(dbs[None], "root-orphan", age_days=100)
    await runner._goal_orphan_gc([(None, None)], scope_cm)
    # RED-proof (M7): in gateway/run_goals.py::_goal_orphan_gc (~line 828) default a 0 to 7 days.
    assert _goal(dbs[None], "root-orphan").status == "active"


def test_gc_runs_at_most_once_per_hour(monkeypatch):
    import gateway.run_goals as rg

    runner = object.__new__(GatewayRunner)
    clock = [1000.0]
    monkeypatch.setattr(rg.time, "monotonic", lambda: clock[0])
    assert runner._goal_orphan_gc_due() is True
    # RED-proof (M8): delete the `if last is not None and now - last < ...: return False` throttle in
    # gateway/run_goals.py::_goal_orphan_gc_due (~line 813) -> due on every 15 s tick.
    clock[0] += 3599
    assert runner._goal_orphan_gc_due() is False
    clock[0] += 2
    assert runner._goal_orphan_gc_due() is True


@pytest.mark.asyncio
async def test_idle_wake_watcher_runs_the_gc(gw_rig, monkeypatch):
    """The reaper rides the existing watcher. RED-proof (M4): delete the
    `if self._goal_orphan_gc_due(): await self._goal_orphan_gc(scopes, _scope)` lines in
    gateway/run_goals.py::_goal_idle_wake_watcher (~line 800) -> the goal stays active."""
    import gateway.run_goals as rg

    runner, dbs, _scope_cm = gw_rig
    runner.session_store = SimpleNamespace(list_sessions=lambda: [])
    _put_goal(dbs[None], "root-orphan", age_days=10)
    runner._running = True
    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:  # the startup delay, then one full tick
            runner._running = False

    monkeypatch.setattr(rg.asyncio, "sleep", _sleep)
    await runner._goal_idle_wake_watcher(interval=15.0)
    assert _goal(dbs[None], "root-orphan").status == "orphaned"
