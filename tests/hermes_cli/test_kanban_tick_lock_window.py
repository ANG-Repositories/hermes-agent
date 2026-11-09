"""The board tick lock is held only while the tick decides (t_f60f502f, ``kanban.dispatch.narrow_lock``).

Measured 2026-11-08 on the Studio: one tick held ``kanban.db.dispatch.lock`` 60-300 s, almost all of it in
workspace resolution and spawn after the claim, and ``kanban admit`` was refused for minutes. The tick now
hands the lock back between a claim and the next decision (:class:`kanban_db_connect.TickLockWindow`),
records how long it held it (``DispatchResult.lock_held_s``) and where the time went (``tick_phases_s``).
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _kill_switch(home: Path) -> None:
    (home / "config.yaml").write_text("kanban:\n  dispatch:\n    narrow_lock: false\n", encoding="utf-8")


def _lock_free(db_path: Path) -> bool:
    """Can ANOTHER holder take the board's tick lock right now (a separate open file, as a second process)."""
    with kbc._dispatch_tick_lock(db_path) as held:
        return held


def test_window_hands_the_board_lock_back_and_takes_it_again(kanban_home):
    db_path = kb.kanban_db_path(board="default")
    with kbc._dispatch_tick_lock(db_path) as held:
        assert held
        window = kbc.TickLockWindow(db_path)
        assert not _lock_free(db_path)
        window.release()
        assert _lock_free(db_path)
        window.hold()
        assert not _lock_free(db_path)
    assert _lock_free(db_path)


def test_a_window_left_released_never_clears_the_next_holders_lock(kanban_home):
    """An exception between release and hold exits the tick with the lock not ours: the tick's exit must not
    unlock or un-stamp whoever holds it now."""
    db_path = kb.kanban_db_path(board="default")
    lock_path = db_path.with_name(db_path.name + ".dispatch.lock")
    with kbc._dispatch_tick_lock(db_path):
        kbc.TickLockWindow(db_path).release()
        other = lock_path.open("a+b")
        assert kbc._try_lock_nb(other)
        kbc._stamp_tick_lock(other)
    try:
        assert not _lock_free(db_path), "the tick's exit released the other holder's lock"
        assert kb._read_dispatch_lock_holder(db_path).get("pid"), "the tick's exit cleared the holder stamp"
    finally:
        kbc._unlock(other)
        other.close()


def _tick_with_slow_spawn(conn, n_cards: int, spawn_s: float):
    """One real tick over ``n_cards`` ready cards whose spawn takes ``spawn_s``; also reports whether another
    holder could take the board lock from inside each spawn."""
    free_during_spawn = []
    db_path = kb.kanban_db_path(board="default")

    def spawn(task, workspace):
        free_during_spawn.append(_lock_free(db_path))
        time.sleep(spawn_s)
        return None

    for i in range(n_cards):
        kb.create_task(conn, title=f"card {i}", assignee="alpha")
    return kbd.dispatch_once(conn, spawn_fn=spawn), free_during_spawn


def test_spawn_runs_with_the_board_lock_released(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        res, free = _tick_with_slow_spawn(conn, n_cards=2, spawn_s=0.0)
    assert len(res.spawned) == 2
    assert free == [True, True], "a second dispatcher / admit was locked out while the tick spawned"
    assert res.lock_held_s is not None
    assert "spawn.unlocked" in res.tick_phases_s and "workspace.unlocked" in res.tick_phases_s
    assert "decide" in res.tick_phases_s


def test_kill_switch_holds_the_lock_through_the_spawn(kanban_home, all_assignees_spawnable):
    _kill_switch(kanban_home)
    with kbc.connect() as conn:
        res, free = _tick_with_slow_spawn(conn, n_cards=2, spawn_s=0.0)
    assert len(res.spawned) == 2
    assert free == [False, False]
    assert not any(k.endswith(".unlocked") and k != "pr_gate_prefetch.unlocked" for k in res.tick_phases_s)


def _p95(values):
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


@pytest.mark.parametrize("narrow", [True, False])
def test_held_time_p95_stays_under_two_seconds_when_spawns_are_slow(kanban_home, all_assignees_spawnable, narrow):
    """Fake board: 5 ticks x 2 cards, each spawn 0.6 s (1.2 s of spawn per tick). Narrow: p95 held time
    under 2 s and under the tick's own spawn time. Kill switch (the control): every tick holds the lock
    for at least its spawns."""
    if not narrow:
        _kill_switch(kanban_home)
    held = []
    with kbc.connect() as conn:
        kbd.dispatch_once(conn, spawn_fn=lambda t, w: None)   # warm-up: the first tick pays one-time imports/sweeps
        for _ in range(5):
            res, _free = _tick_with_slow_spawn(conn, n_cards=2, spawn_s=0.6)
            assert len(res.spawned) == 2
            held.append(res.lock_held_s)
    if narrow:
        assert _p95(held) < 2.0, held
        assert _p95(held) < 1.2, f"lock held through the spawns: {held}"
    else:
        assert min(held) >= 1.2, held


def test_narrow_lock_config_reader():
    assert kbd.narrow_lock_enabled({}) is True
    assert kbd.narrow_lock_enabled({"dispatch": {"narrow_lock": True}}) is True
    assert kbd.narrow_lock_enabled({"dispatch": {"narrow_lock": False}}) is False
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert kbd.narrow_lock_enabled(DEFAULT_CONFIG["kanban"]) is True
