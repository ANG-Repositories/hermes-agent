"""Studio kanban ceiling backstop (t_103a4648).

The LoadGate already caps LOCAL spawns at ``hosts.<studio>.consumers.kanban.ceiling`` (fleet host table, read on
every admit). This file pins what t_103a4648 adds on top:

1. the ceiling is read from the table file on EVERY tick (an edit applies without a restart);
2. operator-set ``asap`` cards are exempt from the ceiling only: the gate publishes how many spawns the ceiling cut
   (``ceiling_exempt``) and the dispatcher lets ``asap`` Studio-bound rows use exactly that room; a plain card waits
   with the ceiling named; the exemption is never wider than utilization admission;
3. a Studio card held at the ceiling for ``ceiling_wait_page_minutes`` (default 60) pages ONCE per episode through
   episode_pager; the episode clears when nothing is held at the ceiling.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_load_gate as klg
from hermes_cli import kanban_studio_ceiling as ksc
from hermes_cli.fleet_placement import admission as ad


# -- the gate -------------------------------------------------------------------------------------------------------

@pytest.fixture
def ctx(monkeypatch):
    c = {"host": "mac-studio", "ceiling": 3, "ceiling_reason": "backstop",
         "need": ad.Estimate(cpus=1.0, ram_gb=3.0, source="prior"),
         "limits": ad.Limits(mem_reserve_gb=128.0), "mem_available_gb": 360.0, "mem_total_gb": 512.0}
    monkeypatch.setattr(klg, "utilization_context", lambda: dict(c))
    monkeypatch.setattr(klg, "sample_psi", lambda: {"psi_cpu_some10": 0.0, "psi_mem_some10": 0.0,
                                                    "psi_io_some10": 0.0})
    monkeypatch.setattr(klg, "shared_leases", lambda host: [])
    return c


def _gate():
    return klg.LoadGate({"max_spawn_per_tick": 4}, 32)


def test_gate_publishes_the_room_the_ceiling_cut(ctx):
    g = _gate()
    a, why = g.admit(10.0, now=1000.0, running=3, cpu_busy=0.10)
    assert a == 0 and why.startswith("ceiling: 3 running of kanban ceiling 3 (backstop)")
    assert g.ceiling_bound is True and g.ceiling_exempt == 4      # utilization alone admits max_spawn_per_tick
    assert g.snapshot()["util"]["ceiling_exempt"] == 4


def test_gate_partial_room_cuts_the_rest(ctx):
    g = _gate()
    a, why = g.admit(10.0, now=1000.0, running=2, cpu_busy=0.10)
    assert (a, g.ceiling_bound, g.ceiling_exempt) == (1, True, 3)


def test_gate_under_the_ceiling_cuts_nothing(ctx):
    ctx["ceiling"] = 20
    g = _gate()
    a, _ = g.admit(10.0, now=1000.0, running=2, cpu_busy=0.10)
    assert (a, g.ceiling_bound, g.ceiling_exempt) == (4, False, 0)


def test_exemption_is_never_wider_than_utilization(ctx):
    g = _gate()
    a, why = g.admit(10.0, now=1000.0, running=3, cpu_busy=0.97)   # host saturated AND at the ceiling
    assert a == 0 and g.ceiling_exempt == 0 and g.ceiling_bound is False and "saturated:cpu" in why


def test_no_ceiling_configured_is_unchanged(ctx):
    ctx["ceiling"] = None
    g = _gate()
    a, why = g.admit(10.0, now=1000.0, running=50, cpu_busy=0.10)
    assert (a, why, g.ceiling_bound, g.ceiling_exempt) == (4, None, False, 0)


def test_ceiling_is_read_from_the_table_on_every_tick(tmp_path, monkeypatch):
    """Deliver 1: config in the fleet host table (hosts.<studio>.consumers.kanban), read per tick, no restart."""
    monkeypatch.setenv("PLACEMENT_HOME", str(tmp_path))
    (tmp_path / "config").mkdir()
    table = tmp_path / "config" / "fleet-hosts.json"

    def write(kanban_row):
        table.write_text(json.dumps({
            "schema": 1, "default_first": "studio", "saturate_to": 1.0,
            "footprints": {"kanban": {"ram_gb": 3, "threads": 1, "scratch_gb": 0, "gpu_mb": 0}},
            "hosts": {"studio": {"aliases": ["local"], "cores": 24, "threads": 32, "mem_total_gb": 512,
                                 "mem_reserve_gb": 128, "cores_reserve": 0, "consumers": {"kanban": kanban_row}}},
        }), encoding="utf-8")

    write({"eligible": True, "ceiling": 8, "_ceiling_reason": "backstop t_103a4648"})
    c = klg.utilization_context()
    assert (c["host"], c["ceiling"], c["ceiling_reason"]) == ("studio", 8, "backstop t_103a4648")
    write({"eligible": True, "ceiling": 2})
    assert klg.utilization_context()["ceiling"] == 2
    write({"_ceiling_removed": "no ceiling"})                  # today's live row: no ceiling at all
    assert klg.utilization_context()["ceiling"] is None


# -- the wait page ---------------------------------------------------------------------------------------------------

class _Pager:
    def __init__(self):
        self.calls = []

    def __call__(self, verb, text):
        self.calls.append((verb, text))
        return True


def test_59_minutes_is_silent_60_pages_once_and_a_second_card_does_not_repage():
    pager = _Pager()
    w = ksc.CeilingWaitWatch(pager=pager)
    t0 = 1_000_000.0
    w.observe(["t_a"], now=t0, minutes=60, ceiling=8, running=8)
    w.observe(["t_a"], now=t0 + 59 * 60, minutes=60, ceiling=8, running=8)
    assert pager.calls == []
    w.observe(["t_a"], now=t0 + 60 * 60, minutes=60, ceiling=8, running=8)
    assert [c[0] for c in pager.calls] == ["page"]
    assert "t_a" in pager.calls[0][1] and "ceiling 8" in pager.calls[0][1] and "60 min" in pager.calls[0][1]
    w.observe(["t_a", "t_b"], now=t0 + 61 * 60, minutes=60, ceiling=8, running=8)
    w.observe(["t_b"], now=t0 + 125 * 60, minutes=60, ceiling=8, running=8)   # t_b itself crosses 60 min here
    assert [c[0] for c in pager.calls] == ["page"]


def test_episode_clears_once_when_nothing_is_held_and_a_new_episode_pages_again():
    pager = _Pager()
    w = ksc.CeilingWaitWatch(pager=pager)
    t0 = 1_000_000.0
    w.observe(["t_a"], now=t0, minutes=60, ceiling=8, running=8)
    w.observe(["t_a"], now=t0 + 3600, minutes=60, ceiling=8, running=8)
    w.observe([], now=t0 + 3700, minutes=60, ceiling=8, running=5)
    w.observe([], now=t0 + 3800, minutes=60, ceiling=8, running=5)
    assert [c[0] for c in pager.calls] == ["page", "clear"]
    w.observe(["t_c"], now=t0 + 4000, minutes=60, ceiling=8, running=8)
    w.observe(["t_c"], now=t0 + 4000 + 3600, minutes=60, ceiling=8, running=8)
    assert [c[0] for c in pager.calls] == ["page", "clear", "page"]


def test_a_card_that_left_the_ceiling_restarts_its_clock():
    pager = _Pager()
    w = ksc.CeilingWaitWatch(pager=pager)
    t0 = 1_000_000.0
    w.observe(["t_a"], now=t0, minutes=60, ceiling=8, running=8)
    w.observe([], now=t0 + 1800, minutes=60, ceiling=8, running=7)       # it spawned
    w.observe(["t_a"], now=t0 + 1900, minutes=60, ceiling=8, running=8)   # held again (a retry)
    w.observe(["t_a"], now=t0 + 3700, minutes=60, ceiling=8, running=8)   # 30 min into this wait
    assert [c[0] for c in pager.calls] == ["clear"]   # only the fresh-process clear (27939eccbc1d), no page


def test_a_fresh_watcher_clears_an_episode_left_open_on_disk_once():
    """Prism 27939eccbc1d: a gateway restart drops _paged, but episode_pager still holds the open episode. The first
    empty tick of a fresh watcher sends one (idempotent) clear; later empty ticks send nothing."""
    pager = _Pager()
    w = ksc.CeilingWaitWatch(pager=pager)
    t0 = 1_000_000.0
    w.observe([], now=t0, minutes=60, ceiling=8, running=3)
    w.observe([], now=t0 + 60, minutes=60, ceiling=8, running=3)
    assert [c[0] for c in pager.calls] == ["clear"]


def test_a_failed_startup_clear_retries_next_empty_tick():
    calls = []
    w = ksc.CeilingWaitWatch(pager=lambda verb, text: calls.append(verb) and False)
    w.observe([], now=1.0, minutes=60, ceiling=8, running=3)
    w.observe([], now=2.0, minutes=60, ceiling=8, running=3)
    assert calls == ["clear", "clear"]


def test_replay_counts_only_ceiling_time_inside_the_window():
    """Prism 399f775412b6: one 2-hour run at ceiling 1 in a 1-hour window is 1.0 h at the ceiling, not 2.0."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "kanban_studio_ceiling_replay", Path(__file__).resolve().parents[2] / "scripts" / "kanban_studio_ceiling_replay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.replay([(0.0, 7200.0, False, "task", None)], 1, 0.0, 3600.0)["hours_at_ceiling"] == 1.0
    # a run that started before the window counts only from t0
    assert mod.replay([(-3600.0, 5400.0, False, "task", None)], 1, 0.0, 3600.0)["hours_at_ceiling"] == 0.5


def test_a_failed_send_retries_next_tick():
    calls = []
    w = ksc.CeilingWaitWatch(pager=lambda verb, text: calls.append(verb) and False)
    t0 = 1_000_000.0
    w.observe(["t_a"], now=t0, minutes=60, ceiling=8, running=8)
    w.observe(["t_a"], now=t0 + 3600, minutes=60, ceiling=8, running=8)
    w.observe(["t_a"], now=t0 + 3660, minutes=60, ceiling=8, running=8)
    assert calls == ["page", "page"]


def test_threshold_is_config():
    assert ksc.page_minutes({"dispatch_load_gate": {"ceiling_wait_page_minutes": 15}}) == 15
    assert ksc.page_minutes({}) == ksc.DEFAULT_PAGE_MINUTES == 60
    assert ksc.page_minutes({"dispatch_load_gate": {"ceiling_wait_page_minutes": "x"}}) == 60
    assert ksc.page_minutes({"dispatch_load_gate": {"ceiling_wait_page_minutes": 0}}) is None   # 0 = page off


def test_episode_pager_argv(tmp_path, monkeypatch):
    script = tmp_path / "scripts" / "lib" / "episode_pager.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    seen = []

    class R:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(ksc.subprocess, "run", lambda argv, **kw: seen.append(argv) or R())
    send = ksc.episode_pager_sender(tmp_path)
    assert send("page", "held") is True
    assert send("clear", "back under") is True
    page, clear = seen
    assert page[2:4] == ["page", "--producer"] and "kanban-studio-ceiling" in page
    assert page[page.index("--text") + 1] == "held" and "--remind-h" in page
    assert clear[2] == "clear" and clear[clear.index("--resolve-text") + 1] == "back under"
    assert ksc.episode_pager_sender(tmp_path / "nowhere")("page", "x") is False


# -- the dispatcher: asap is exempt, a plain card waits naming the ceiling ------------------------------------------

@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(kb, "_system_memory_sample", lambda: {}, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _tick(conn, **kw):
    order: list[str] = []

    def _spawn(task, workspace, **_):
        order.append(task.id)
        return None

    res = kb.dispatch_once(conn, spawn_fn=_spawn, max_spawn=64, **kw)
    return order, res


CEILING_WHY = "ceiling: 8 running of kanban ceiling 8 (backstop)"


def test_plain_card_waits_at_the_ceiling_and_is_listed_held(board):
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="plain", assignee="daedalus")
        order, res = _tick(conn, spawn_paused=CEILING_WHY, spawn_limit=0, ceiling_exempt=4)
    assert order == []
    assert res.ceiling_held == [t]
    assert res.ceiling_exempt_spawned == 0


def test_asap_card_spawns_through_the_ceiling(board):
    with kb.connect_closing() as conn:
        plain = kb.create_task(conn, title="plain", assignee="daedalus", priority=99)
        urgent = kb.create_task(conn, title="urgent", assignee="daedalus")
        conn.execute("UPDATE tasks SET asap = 1 WHERE id = ?", (urgent,))
        conn.commit()
        order, res = _tick(conn, spawn_paused=CEILING_WHY, spawn_limit=0, ceiling_exempt=4)
    assert order == [urgent]
    assert res.ceiling_held == [plain]
    assert res.ceiling_exempt_spawned == 1


def test_asap_exemption_is_bounded_by_the_room_the_ceiling_cut(board):
    with kb.connect_closing() as conn:
        ids = []
        for i in range(3):
            ids.append(kb.create_task(conn, title=f"u{i}", assignee="daedalus", priority=10 - i))
            conn.execute("UPDATE tasks SET asap = 1 WHERE id = ?", (ids[-1],))
        conn.commit()
        order, res = _tick(conn, spawn_paused=CEILING_WHY, spawn_limit=0, ceiling_exempt=2)
    assert order == ids[:2] and res.ceiling_exempt_spawned == 2 and res.ceiling_held == [ids[2]]


def test_no_exemption_when_the_ceiling_is_not_what_binds(board):
    """A utilization refusal (ceiling_exempt 0) binds asap cards too: the exemption is the ceiling's only."""
    with kb.connect_closing() as conn:
        urgent = kb.create_task(conn, title="urgent", assignee="daedalus")
        conn.execute("UPDATE tasks SET asap = 1 WHERE id = ?", (urgent,))
        conn.commit()
        order, res = _tick(conn, spawn_paused="utilization: saturated:cpu", spawn_limit=0)
    assert order == [] and res.ceiling_exempt_spawned == 0


def test_ceiling_bound_lists_held_cards_after_another_board_used_the_asap_room(board):
    """Prism aa55a32125bc: board A spent the tick's asap room, board B gets ceiling_exempt=0. B's plain card is
    still waiting at the ceiling, so it must stay in ceiling_held (else the wait clock resets / the page clears)."""
    with kb.connect_closing() as conn:
        t = kb.create_task(conn, title="plain", assignee="daedalus")
        order, res = _tick(conn, spawn_paused=CEILING_WHY, spawn_limit=0, ceiling_exempt=0, ceiling_bound=True)
    assert order == [] and res.ceiling_held == [t]


def test_asap_spawns_through_the_ceiling_with_route_first_off(board, monkeypatch):
    """Prism 996af7eca6ca: with kanban.placement.route_first off, a higher-priority plain card at the wall must not
    stop the scan before the asap card that the ceiling room lets through."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_priority_door as door
    monkeypatch.setattr(kbd, "_route_first_enabled", lambda: False)
    monkeypatch.setattr(door, "enabled", lambda: False)
    with kb.connect_closing() as conn:
        plain = kb.create_task(conn, title="plain", assignee="daedalus", priority=99)
        urgent = kb.create_task(conn, title="urgent", assignee="daedalus")
        conn.execute("UPDATE tasks SET asap = 1 WHERE id = ?", (urgent,))
        conn.commit()
        order, res = _tick(conn, spawn_paused=CEILING_WHY, spawn_limit=0, ceiling_exempt=4)
    assert order == [urgent]
    assert res.ceiling_held == [plain] and res.ceiling_exempt_spawned == 1
