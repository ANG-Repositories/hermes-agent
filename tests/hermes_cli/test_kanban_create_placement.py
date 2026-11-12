"""``kanban create`` prints where the card will run and which line decided it (t_0a5740f3).

Ace 2026-11-11 (Discord 1570197131329937549): kanban workers stay off the Mac Studio unless truly necessary.
The author of a card saw nothing at mint time; the first sign of a Studio placement was the placement event.
The line comes from the dispatcher's own route (``kanban_db_dispatch._route_row``), never a re-implementation.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_create_placement as kcp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd

from tests.hermes_cli.test_kanban_worker_pool import kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_studio_capability_only import _caps


@pytest.fixture
def pool(monkeypatch):
    """The live shape: one pool host (ace-ai), the capability layer on, the Studio last resort."""
    from tests.hermes_cli.test_kanban_worker_pool import _plan
    monkeypatch.setattr(kcp, "_pool_config", lambda: _plan(free=2).config)
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    monkeypatch.setattr(kbd, "_studio_last_resort", lambda fleet: True)
    monkeypatch.setattr(kbd, "_studio_capability_on", lambda caps, last: True)


def _line(conn, body, **kw):
    tid = kb.create_task(conn, title="t", assignee=kw.pop("assignee", "alpha"), body=body, **kw)
    return tid, kcp.preview_line(conn, tid)


def test_requires_studio_state_names_the_studio_and_the_line(kanban_home, pool):
    with kb.connect_closing() as conn:
        tid, line = _line(conn, "host: studio\nrequires: studio-state (reads the live state.db file)\nwork")
    assert line.startswith("will run on the Mac Studio"), line
    assert "`requires: studio-state (reads the live state.db file)`" in line, line


def test_an_unpinned_card_runs_on_a_pool_host(kanban_home, pool):
    with kb.connect_closing() as conn:
        _tid, line = _line(conn, "fix the lint")
    assert line.startswith("will run on a pool host"), line
    assert "no line pins it" in line, line


def test_a_pool_pin_names_its_host_line(kanban_home, pool):
    with kb.connect_closing() as conn:
        _tid, line = _line(conn, "host: ace-ai\nfix the lint")
    assert line.startswith("will run on ace-ai"), line
    assert "`host: ace-ai`" in line, line


def test_a_card_that_cannot_place_says_it_waits(kanban_home, pool):
    with kb.connect_closing() as conn:
        _tid, line = _line(conn, "host: nowhere-box\nwork")
    assert line.startswith("will wait"), line


def test_a_structural_studio_reason_is_named(kanban_home, pool):
    """An assignee no pool host runs keeps the card on the Studio: the line says which rule."""
    with kb.connect_closing() as conn:
        _tid, line = _line(conn, "work", assignee="gamma")
    assert "rule profile" in line, line


def test_a_fleet_pause_hold_says_the_card_waits_not_a_host(kanban_home, pool, monkeypatch):
    """A held route never reaches _route_row's origin step; the line says it is held, not a host it won't run on."""
    from hermes_cli import kanban_pause_hold as kph
    seen = {}

    def held(rows, cache):
        seen["ids"] = [r["id"] for r in rows]
        return {r["id"]: ("wait", "fleet_pause:ace-ai") for r in rows}

    monkeypatch.setattr(kph, "held_routes", held)
    with kb.connect_closing() as conn:
        tid, line = _line(conn, "host: ace-ai\nfix the lint")
    assert seen["ids"] == [tid]
    assert line.startswith("will wait (fleet_pause:ace-ai)"), line
    assert "fleet pause hold" in line, line
    assert "will run on" not in line, line


def test_preview_never_raises_and_writes_no_event(kanban_home, pool, monkeypatch):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t", assignee="alpha", body="fix")
        before = conn.execute("SELECT count(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
        kcp.preview_line(conn, tid)
        after = conn.execute("SELECT count(*) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
        assert before == after
        monkeypatch.setattr(kbd, "_route_row", lambda *a, **k: 1 / 0)
        assert kcp.preview_line(conn, tid) is None
        assert kcp.preview_line(conn, "t_missing0") is None


def _cli(*argv):
    import argparse
    from hermes_cli import kanban as kc
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    return kc._cmd_create(root.parse_args(["kanban", "create", "probe", "--unhomed", *argv]))


def test_cli_create_prints_the_placement_line(kanban_home, pool, capsys):
    assert _cli("--assignee", "alpha", "--body", "fix the lint") == 0
    err = capsys.readouterr().err
    assert "placement: t_" in err and "will run on a pool host" in err, err


def test_cli_create_json_stays_clean(kanban_home, pool, capsys):
    assert _cli("--assignee", "alpha", "--body", "fix the lint", "--json") == 0
    assert "placement:" not in capsys.readouterr().err


def test_tool_create_returns_the_placement_line(kanban_home, pool, monkeypatch):
    import json
    from tools import kanban_tools as kt
    monkeypatch.setattr(kt, "_auto_wake", lambda *a, **k: False, raising=False)
    out = json.loads(kt._handle_create({"title": "t", "assignee": "alpha", "body": "host: ace-ai\nfix",
                                        "unhomed": True, "board": "default"}))
    assert out.get("ok"), out
    assert out["placement"].startswith("will run on ace-ai"), out
