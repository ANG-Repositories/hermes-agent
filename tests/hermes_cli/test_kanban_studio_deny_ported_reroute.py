"""Studio default-deny on a card that was PORTED to a pool host and came back (t_f914c529).

t_cf13ff3b, 2026-11-12: created with no ``requires:`` line, ported to ci-box (``worktree_ported``), blocked there
``kind=capability``, re-routed by the dispatcher (``placement_rerouted`` to_host ``studio``, body gains
``host: studio`` + ``requires: studio-state``). For 4.5 h every placement_wait read
``not_portable:requires:studio-state``: the old port event still bound the card to ci-box, so the route never reached
the default-deny check. No ``studio_default_deny`` code, no wake to the home session, ``kanban admit`` refused with
the bind's code, and ``studio-override`` changed nothing. A fresh card with the same body read
``studio_default_deny:studio-state`` and started 75 s after an override.

Rules pinned here, built from that event shape:
1. The port bind ends ONLY when the card now carries a hard requirement the port host cannot serve, and only after
   that host's clone is proven clean and pushed (remote GC's predicate). The card is then re-pointed to a FRESH path
   (``worktree_handed_over``: from host, clone path, branch, head) and waits ``studio_default_deny:<cap>``.
2. A dirty/unpushed/unreadable clone keeps the card held ``port_stranded:<host> (<clone path>: <why> ...)``.
3. A ported, re-routed card with no hard requirement its host lacks keeps its bind (today's behaviour).
4. The full tick writes the same ``mechanical_wake``; a hold whose capability CHANGES wakes again (t_17732be2).
5. ``studio-override`` then releases it; ``kanban admit --host studio`` names the override command, and the spawn
   gets the fresh path filled with the card's branch at the clone's head.
6. ``studio-override`` on a card whose latest wait is not a default-deny hold says the override does not release it.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_studio_allowlist as ksa
from hermes_cli import kanban_worktree_port as kwt

from tests.hermes_cli.test_kanban_worker_pool import _plan, _tick, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_studio_pins_and_carry import _row, _studio_last_resort_on  # noqa: F401
from tests.hermes_cli.test_kanban_studio_capability_only import _caps
from tests.hermes_cli.test_kanban_studio_default_deny import _cfg

# t_cf13ff3b's body after the dispatcher re-route (kanban_placement_block.pin_body)
REROUTED_BODY = ("host: studio\nrequires: studio-state  # re-routed after capability block: Placed on ci-box\n"
                 "Follow-up to t_73572f17: replay the Studio lcm.db")


@pytest.fixture
def clean_clone(monkeypatch):
    """The port host's clone passes remote GC's proof at HEAD ``abc123`` (no ssh in tests)."""
    calls = []

    def probe(host, path, runner=None):
        calls.append((host.name, path))
        return True, "abc123"
    monkeypatch.setattr(kwt, "probe_clone", probe)
    kwt._probe_memo.clear()
    return calls


@pytest.fixture
def operator(kanban_home, monkeypatch):
    p = kb.operator_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("tok")
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, "tok")
    return kanban_home


def _ported_card(conn, *, rerouted=True, host="ace-ai", body=REROUTED_BODY):
    """A ready scratch card with t_cf13ff3b's event history: ported to ``host``, ran there, blocked, re-routed."""
    tid = kb.create_task(conn, title="lcm writer fix", assignee="alpha", body=body)
    # the port persists the pool-host path; the hermetic home stands in for /Volumes/fleet-scratch
    path = str(kb.kanban_home() / "fleet-scratch" / "workspaces" / "default" / tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (path, tid))
        kb._append_event(conn, tid, kwt.PORTED_EVENT, {"host": host, "repo": "NousResearch/hermes-agent",
                                                        "mirror": "~/.hermes/mirrors/x.git",
                                                        "branch": f"wt/{tid}", "workspace": path})
        kb._append_event(conn, tid, "worker_placed", {"host": host, "workspace": path, "reason": "overflow"})
        kb._append_event(conn, tid, "blocked", {"kind": "capability", "reason": "Placed on ci-box"})
        if rerouted:
            kb._append_event(conn, tid, "placement_rerouted", {"from_host": host, "to_host": "studio", "run_id": 1})
    return tid, path


def _route(conn, tid):
    return kbd._route_row(conn, _row(conn, tid), _plan(free=2), caps=_caps(), rules_out={}, carry_out={},
                          studio_out={})


def test_ported_host_is_unchanged_by_a_reroute(kanban_home):
    """ported_host() still names the port host; the dispatcher decides whether the bind holds."""
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        assert kwt.ported_host(conn, tid, path) == "ace-ai"


# -- 1. the wait code and the hand-over --------------------------------------------------------------------------

def test_rerouted_ported_card_is_handed_over_and_waits_on_studio_default_deny(kanban_home, clean_clone):
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        route = _route(conn, tid)
        new = conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        ev = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                                     (tid, kwt.HANDOVER_EVENT)).fetchone()[0])
    assert route[0] == "wait", route
    assert route[1].startswith("studio_default_deny:studio-state ("), route
    assert clean_clone == [("ace-ai", path)]
    assert new == f"{path}-from-ace-ai" and new != path
    assert ev == {"from_host": "ace-ai", "workspace": path, "path": new, "repo": "NousResearch/hermes-agent",
                  "mirror": "~/.hermes/mirrors/x.git", "branch": f"wt/{tid}", "head": "abc123",
                  "missing": ["studio-state"]}


def test_rerouted_ported_card_routes_local_when_default_deny_is_off(kanban_home, clean_clone):
    with kb.connect_closing() as conn:
        tid, _path = _ported_card(conn)
        assert _route(conn, tid)[0] == "local"


@pytest.mark.parametrize("why", ["dirty, ignored or untracked work", "local commits not on any remote",
                                 "probe failed: timed out"])
def test_a_clone_with_work_on_the_port_host_stays_held_and_named(kanban_home, monkeypatch, why):
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kwt, "probe_clone", lambda host, path, runner=None: (False, why))
    kwt._probe_memo.clear()
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        route = _route(conn, tid)
        still = conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        handed = conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind=?",
                              (tid, kwt.HANDOVER_EVENT)).fetchone()[0]
    assert route == ("wait", route[1]) and route[1].startswith(f"port_stranded:ace-ai ({path}: {why};"), route
    assert "studio-state" in route[1]
    assert still == path and handed == 0


def test_an_existing_directory_at_the_handover_path_is_never_reused(kanban_home, clean_clone):
    import os
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        os.makedirs(f"{path}-from-ace-ai")
        route = _route(conn, tid)
    assert route[1].startswith("port_stranded:ace-ai") and "never reused" in route[1], route


def test_rerouted_port_without_a_hard_requirement_the_host_lacks_keeps_its_bind(kanban_home, clean_clone):
    """Operator send-back 1: 55 ported cards were re-routed in 7 days, most after hours of work in their clone.
    Only a hard requirement the port host cannot serve ends the bind."""
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn, body="host: studio\nfix the lint")
        route = _route(conn, tid)
        assert conn.execute("SELECT workspace_path FROM tasks WHERE id=?", (tid,)).fetchone()[0] == path
    assert route[0] == "pin" and route[1] == "ace-ai", route
    assert clean_clone == []


def test_port_without_a_later_studio_reroute_still_binds(kanban_home, clean_clone):
    _cfg(kanban_home, True, [])
    with kb.connect_closing() as conn:
        tid, _path = _ported_card(conn, rerouted=False, body="plain work")
        route = _route(conn, tid)
    assert route == ("pin", "ace-ai"), route


# -- 2. the notice ----------------------------------------------------------------------------------------------

def test_full_tick_wakes_the_home_session_once_with_the_override_command(kanban_home, monkeypatch,
                                                                          clean_clone):
    _cfg(kanban_home, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid, _path = _ported_card(conn)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=2)
        _tick(conn, spillover=_plan(free=2), spawn_limit=2)
        wakes = [json.loads(p) for (p,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='mechanical_wake'", (tid,))]
        wait = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='placement_wait' "
                                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])
    assert tid not in [t for t, _h in spawned], spawned
    assert res.placement_waits[tid].startswith("studio_default_deny:studio-state"), res.placement_waits
    assert wait["reason"].startswith("studio_default_deny:studio-state"), wait
    assert len(wakes) == 1, wakes
    assert wakes[0]["source"] == ksa.HOLD_WAKE_SOURCE
    assert f"hermes kanban studio-override {tid} --capability studio-state" in wakes[0]["text"]


def test_a_hold_whose_capability_changes_wakes_again(kanban_home):
    """t_17732be2: held studio-state, overridden, ran, sent back, held again as workspace_kind:origin_not_ours with
    no notice. A wake now records its capability; a new capability in the same episode is a new notice."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="requires: studio-state")
        first = "studio_default_deny:studio-state (declared: x; no pool host has it; not on the Studio allowlist)"
        second = ("studio_default_deny:workspace_kind:origin_not_ours (card: /w; no pool host has it; "
                  "not on the Studio allowlist)")
        assert ksa.notify_holds(conn, {tid: first}) == [tid]
        assert ksa.notify_holds(conn, {tid: first}) == []
        assert ksa.notify_holds(conn, {tid: second}) == [tid]
        assert ksa.notify_holds(conn, {tid: second}) == []
        wakes = [json.loads(p) for (p,) in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='mechanical_wake' ORDER BY id", (tid,))]
    assert [w["capability"] for w in wakes] == ["studio-state", "workspace_kind:origin_not_ours"]
    assert (f"hermes kanban studio-override {tid} --capability workspace_kind:origin_not_ours"
            in wakes[1]["text"])


# -- 3. override + admit ----------------------------------------------------------------------------------------

def test_override_releases_the_rerouted_ported_card(operator, clean_clone):
    _cfg(operator, True, [])
    with kb.connect_closing() as conn:
        tid, _path = _ported_card(conn)
        assert _route(conn, tid)[0] == "wait"
        ksa.override(conn, tid, "studio-state", "must replay the live Studio lcm.db", actor="apollo")
        assert _route(conn, tid)[0] == "local"


def test_admit_on_the_rerouted_ported_card_names_the_override_then_spawns_in_the_handed_over_clone(
        operator, monkeypatch, clean_clone):
    """The spawn reads ``workspace`` (operator send-back 2: the old stub ignored it). The Studio run gets a fresh path
    filled with the card's branch at the clone's head, never the port host's path."""
    from hermes_cli import kanban_admit as ka
    _cfg(operator, True, [])
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    clones, spawned = [], []

    class Proc:
        returncode, stderr = 0, ""

    def runner(argv, **kw):
        clones.append(argv[-1])
        return Proc()
    real = kwt.materialize_handover
    monkeypatch.setattr(kwt, "materialize_handover",
                        lambda conn, task, workspace, placed_host=None, runner_=None: real(
                            conn, task, workspace, placed_host, runner=runner))

    def spawn(task, workspace, *, board=None, placement=None):
        spawned.append((task.id, placement.name if placement else "studio", str(workspace)))
        return 4242

    with kb.connect_closing() as conn:
        tid, path = _ported_card(conn)
        res = kbd.admit_once(conn, tid, "studio", spawn_fn=spawn, spillover=_plan(free=2))
        assert spawned == [], spawned
        line = ka.refusal_from(res, tid)
        assert f"hermes kanban studio-override {tid} --capability studio-state" in line, line
        ksa.override(conn, tid, "studio-state", "must replay the live Studio lcm.db", actor="apollo")
        res = kbd.admit_once(conn, tid, "studio", spawn_fn=spawn, spillover=_plan(free=2))
    assert spawned == [(tid, "studio", f"{path}-from-ace-ai")], ka.refusal_from(res, tid)
    assert len(clones) == 1, clones
    script = clones[0]
    assert f"w={path}-from-ace-ai;" in script and f"wt/{tid}" in script and "abc123" in script, script
    assert "https://github.com/NousResearch/hermes-agent.git" in script


# -- 5. the override verb names a hold it cannot release --------------------------------------------------------

def test_override_on_a_non_deny_hold_says_it_does_not_release(operator, capsys):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="requires: studio-state")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "placement_wait", {"code": "not_portable:requires:studio-state",
                                                           "reason": "not_portable:requires:studio-state",
                                                           "route": "wait", "host": None})
    rc = ksa.cmd_studio_override(argparse.Namespace(task_id=tid, capability="studio-state",
                                                    why="must replay the live lcm.db", until=None, board=None))
    err = capsys.readouterr().err
    assert rc == 0
    assert "not_portable:requires:studio-state" in err and "does not release" in err, err
