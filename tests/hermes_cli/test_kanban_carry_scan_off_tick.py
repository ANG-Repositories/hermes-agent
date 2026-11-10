"""t_3489445d: the local_files carry scan never walks a workspace inside the dispatcher's routing pass.

Measured before the change (nas-fleet, 85 real workspaces under /srv/fleet/workspaces/default, cold cache):
12.2 s and 1.53M lstat for one full pass; 72 of 85 dirs were repo-sized (>20k entries, ~0.2-0.7 s each). The
dispatcher re-walked every candidate once per 300 s TTL, inside ``_route_row``.

Now ``_route_row`` reads :func:`kanban_worker_hosts.carry_size_known` (cache only; unknown keeps the old rule) and
:func:`kanban_worker_hosts.carry_scan_refresh` walks queued paths after the routing pass, within a per-tick entry
budget, each walk stopping at the cap.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_worker_hosts as kwh

from tests.hermes_cli.test_kanban_worker_pool import _plan, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_studio_last_resort import _write_policy


@pytest.fixture(autouse=True)
def _fresh(kanban_home, monkeypatch):
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    _write_policy(fleet)
    kwh._carry_scan_cache.clear()
    kwh._carry_sync_cache.clear()
    getattr(kwh, "_carry_pending", {}).clear()
    getattr(kwh, "_carry_stats", {}).clear()
    monkeypatch.setattr(kwh, "carry_workspace", lambda host, ws, runner=None: None)


def _boom(*a, **k):
    raise AssertionError("workspace walked inside the routing pass")


def _card(conn, files=1, body=None):
    tid = kb.create_task(conn, title="lf", assignee="alpha", body=body)
    path = kbw.resolve_workspace(kb.get_task(conn, tid))
    Path(path).mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (Path(path) / f"f{i}.txt").write_text("x")
    kbw.set_workspace_path(conn, tid, str(path))
    return tid, str(path)


def _route(conn, tid):
    rules: dict = {}
    carry: dict = {}
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    route = kbd._route_row(conn, row, _plan(free=2), rules_out=rules, carry_out=carry)
    return route, rules.get(tid), carry.get(tid, False)


def test_route_row_never_walks_a_workspace(kanban_home, monkeypatch):
    with kb.connect_closing() as conn:
        tid, _ = _card(conn)
        monkeypatch.setattr(os, "walk", _boom)
        monkeypatch.setattr(kwh, "_carry_walk", _boom, raising=False)
        route, rule, carry = _route(conn, tid)
    # Cold cache: unknown keeps the old rule this tick (Studio continuity, not carried).
    assert (route[0], rule, carry) == ("local", "local_files", False)


def test_cold_cache_answers_unknown_without_walking(tmp_path, monkeypatch):
    d = tmp_path / "w"
    d.mkdir()
    monkeypatch.setattr(kwh, "_carry_walk", _boom)
    assert kwh.carry_size_known(str(d)) is None
    assert len(kwh._carry_pending) == 1
    assert kwh.carry_size_known("/tmp/a b") is False  # never carryable: answered, not queued


def test_refresh_after_routing_then_carries(kanban_home):
    with kb.connect_closing() as conn:
        tid, _ = _card(conn)
        assert _route(conn, tid)[2] is False
        assert kwh.carry_scan_refresh() >= 1
        route, rule, carry = _route(conn, tid)
    assert carry is True and route[0] != "local", (route, rule)


def test_walk_stops_at_the_entry_cap(tmp_path):
    d = tmp_path / "big"
    d.mkdir()
    for i in range(200):
        (d / f"f{i}").write_text("x")
    ok, seen = kwh._carry_walk(str(d), max_bytes=10 ** 9, max_entries=5)
    assert ok is False and seen == 6
    ok, seen = kwh._carry_walk(str(d), max_bytes=3, max_entries=10 ** 6)
    assert ok is False and seen == 4
    assert kwh._carry_walk(str(d), 10 ** 9, 10 ** 6) == (True, 200)


def test_refresh_respects_the_per_tick_budget(tmp_path):
    paths = []
    for n in range(3):
        d = tmp_path / f"w{n}"
        d.mkdir()
        for i in range(10):
            (d / f"f{i}").write_text("x")
        paths.append(str(d))
        assert kwh.carry_size_known(str(d)) is None
    assert kwh.carry_scan_refresh(budget_entries=5) == 5  # no workspace may overrun the remaining budget
    assert len(kwh._carry_pending) == 3
    kwh.carry_scan_refresh(budget_entries=10 ** 6)
    assert not kwh._carry_pending and all(kwh.carry_size_known(p) is True for p in paths)
    s = kwh.carry_scan_stats()
    assert s["walks"] == 4 and s["walk_entries"] == 35 and s["paths"] == 3


def test_stale_positive_is_unknown_and_requeued(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    assert kwh.carry_size_known(str(d), now=0.0) is None
    kwh.carry_scan_refresh(now=0.0)
    assert kwh.carry_size_known(str(d), now=kwh.CARRY_SCAN_TTL_SECONDS + 1.0) is None
    assert kwh._carry_pending


def test_too_big_verdict_is_not_rewalked_every_ttl(tmp_path):
    d = tmp_path / "big"
    d.mkdir()
    for i in range(5):
        (d / f"f{i}").write_text("x")
    assert kwh.carry_size_known(str(d), max_entries=2, now=0.0) is None
    kwh.carry_scan_refresh(now=0.0)
    assert kwh.carry_size_known(str(d), max_entries=2, now=kwh.CARRY_SCAN_TTL_SECONDS + 1.0) is False
    assert not kwh._carry_pending  # still fresh: not re-queued at 300 s
    assert kwh.carry_size_known(str(d), max_entries=2, now=kwh.CARRY_SCAN_NEGATIVE_TTL_SECONDS + 1.0) is False
    assert kwh._carry_pending  # answered False, re-queued after the negative TTL


def test_warm_placement_parity_with_main_synchronous_scanner(kanban_home, monkeypatch):
    """Warm-routing parity only: main synchronous scanner versus cached scanner on fixture rows."""
    with kb.connect_closing() as conn:
        small, _ = _card(conn, files=2)
        big, _ = _card(conn, files=30)
        browser, _ = _card(conn, body="add to cart in the signed-in browser")
        plain = kb.create_task(conn, title="p", assignee="alpha")
        ids = [small, big, browser, plain]
        real_known = kwh.carry_size_known
        kwh.carry_size_known = lambda p, **k: kwh.carry_size_ok(p, max_entries=10)  # the old in-tick scan
        try:
            before = {t: _route(conn, t) for t in ids}
        finally:
            kwh.carry_size_known = real_known
        kwh._carry_scan_cache.clear()
        kwh._carry_pending.clear()
        for t in ids:
            _route(conn, t)  # cold pass: queues
        for key in list(kwh._carry_pending):
            item = kwh._carry_pending.pop(key)
            kwh._carry_pending[(key[0], kwh.CARRY_MAX_BYTES, 10)] = (kwh.CARRY_MAX_BYTES, 10, True)
        kwh.carry_scan_refresh(budget_entries=10 ** 6)
        monkeypatch.setattr(kwh, "carry_size_known", lambda p: real_known(p, max_entries=10))
        after = {t: _route(conn, t) for t in ids}
    assert after == before
    assert before[small][2] is True and before[big][2] is False


def test_kanban_home_nested_check_is_memoized(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(kb, "_kanban_isolation_context", lambda: None)
    monkeypatch.setattr(kb, "kanban_sandbox_enabled", lambda: False)
    monkeypatch.setattr(kb, "_refuse_nested_non_profile_home", lambda root: calls.append(root))
    kb._NESTED_HOME_OK.clear()
    kb._refuse_nested_non_profile_home_memo(tmp_path)
    kb._refuse_nested_non_profile_home_memo(tmp_path)
    assert len(calls) == 1
    kb._refuse_nested_non_profile_home_memo(tmp_path / "other")
    assert len(calls) == 2


def test_kanban_home_refusal_is_never_memoized(monkeypatch, tmp_path):
    calls = []

    def refuse(root):
        calls.append(root)
        raise kb.KanbanLiveBoardRefusedError("nested")
    monkeypatch.setattr(kb, "_refuse_nested_non_profile_home", refuse)
    kb._NESTED_HOME_OK.clear()
    for _ in range(2):
        with pytest.raises(kb.KanbanLiveBoardRefusedError):
            kb._refuse_nested_non_profile_home_memo(tmp_path)
    assert len(calls) == 2


def test_refresh_deadline_is_checked_inside_walk(tmp_path):
    d = tmp_path / "deadline"
    d.mkdir()
    for i in range(100):
        (d / str(i)).touch()
    ticks = iter(i / 1000 for i in range(1000))
    clock = lambda: next(ticks)
    kwh.carry_size_known(str(d))
    used = kwh.carry_scan_refresh(max_seconds=0.010, clock=clock)
    assert used < 10
    assert kwh.carry_size_known(str(d)) is None


def test_expanded_home_change_invalidates_memo(monkeypatch, tmp_path):
    monkeypatch.setattr(kb, "_matching_live_root", lambda root: tmp_path)
    monkeypatch.setenv("HERMES_HOME", "$CARRY_TEST_HOME")
    monkeypatch.setenv("CARRY_TEST_HOME", str(tmp_path / "profiles" / "ok"))
    kb._NESTED_HOME_OK.clear()
    kb._refuse_nested_non_profile_home_memo(tmp_path)
    monkeypatch.setenv("CARRY_TEST_HOME", str(tmp_path / "profiles" / "ok" / "scratch"))
    with pytest.raises(kb.KanbanLiveBoardRefusedError):
        kb._refuse_nested_non_profile_home_memo(tmp_path)


def test_memo_expires(monkeypatch, tmp_path):
    import time
    now = [0.0]
    calls = []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(kb, "_refuse_nested_non_profile_home", lambda root: calls.append(root))
    kb._NESTED_HOME_OK.clear()
    kb._refuse_nested_non_profile_home_memo(tmp_path)
    now[0] = 86400
    kb._refuse_nested_non_profile_home_memo(tmp_path)
    assert len(calls) == 2
