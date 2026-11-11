"""Upstream small-VM memory guards stand down under fleet admission (t_2158f095).

Apollo ruling t_2158f095-round4-integration 2 (Ace msg 1569308234441949345, "only fwa is the guard/gate"): the
derived ``max_in_progress`` default (MemTotal / 512 MB clamped 2..8) and ``_memory_pressure_level`` (critical = spawn
nothing, elevated = at most one) stay in the code for non-fleet installs, and are inert whenever
``kanban.admission: utilization`` (the fleet admission path) is set in config.yaml. With it unset/legacy, upstream
behaviour is unchanged. The config is read through the real loader from a temp HERMES_HOME.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

GIB = 1024 * 1024  # KiB per GiB
# A 1 GiB Linux VM at critical pressure: the derived cap is 2, the pressure guard spawns nothing.
LOW = {"mem_available_kib": 32 * 1024, "mem_total_kib": 1 * GIB}


def _home(tmp_path, monkeypatch, admission):
    home = tmp_path / ".hermes"
    home.mkdir()
    if admission is not None:
        (home / "config.yaml").write_text(f"kanban:\n  admission: {admission}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: dict(LOW))
    kb.init_db()
    return home


def _dispatch_three():
    spawns = []

    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42

    with kbc.connect() as conn:
        for title in ("a", "b", "c"):
            kb.create_task(conn, title=title, assignee="alice")
        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn,
                                max_in_progress=kbd.resolve_max_in_progress(kbd.configured_max_in_progress()))
    return spawns, res


@pytest.mark.real_memory_guard
def test_fleet_admission_neither_caps_nor_holds_on_a_low_memory_linux_sample(
    tmp_path, monkeypatch, all_assignees_spawnable,
):
    _home(tmp_path, monkeypatch, "utilization")
    assert kbd.resolve_max_in_progress(None) is None
    assert kbd._memory_pressure_level() == "unknown"
    spawns, res = _dispatch_three()
    assert len(spawns) == 3, res
    assert res.memory_pressure is None


@pytest.mark.real_memory_guard
@pytest.mark.parametrize("admission", [None, "legacy"])
def test_without_fleet_admission_upstream_guards_unchanged(
    tmp_path, monkeypatch, all_assignees_spawnable, admission,
):
    _home(tmp_path, monkeypatch, admission)
    assert kbd.resolve_max_in_progress(None) == 2
    assert kbd._memory_pressure_level() == "critical"
    spawns, res = _dispatch_three()
    assert spawns == []
    assert res.memory_pressure == "critical"


@pytest.mark.real_memory_guard
def test_explicit_max_in_progress_still_wins_under_fleet_admission(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch, "utilization")
    assert kbd.resolve_max_in_progress(5) == 5
