"""The Studio is the last resort for kanban work (t_e604c0f9; Ace 2026-11-08 16:13 / 16:38 / 17:17).

Three dispatcher rules pinned a card to the Studio before the FWA scorer ran (24 h census 16:38: workspace_kind 196,
local_files 173, host_studio 86 of 483 Studio spawns):

* a bare ``host: studio`` line holds only with a ``requires:`` reason the Studio serves; else it is dropped;
* ``local_files`` (the card's Studio scratch dir holds files) is continuity: local while the local gate admits, else
  the dir is carried to the FWA-chosen host;
* a ``worktree:<repo>/.worktrees/<id>`` target not made yet anchors on the repo, so the existing port applies;
  ``kanban create`` rewrites ``.worktrees/<x>`` targets and prints the rewrite.

Kill switch: the ONE shared setting (t_c2fce87e) ``host_preference.consumers.kanban`` in
fleet/placement-policy.json; off / absent = every old pin, exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_worker_hosts as kwh
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli import kanban_worktree_port as kwt

from tests.hermes_cli.test_kanban_worker_pool import _plan, _tick, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_worktree_port import SLUG, _mirrors, anchor  # noqa: F401
from tests.hermes_cli.test_kanban_studio_last_resort import HOST_PREF, _write_policy

STUDIO_CAPS = frozenset({"browser-session", "studio-state", "control-plane", "docker"})


def _caps(mode=khc.WARN):
    """A valid map whose Studio row serves STUDIO_CAPS; warn mode, so no requires: word is a hard rule and the
    pin rule alone decides."""
    cmap = khc.CapabilityMap(capabilities=tuple(sorted(STUDIO_CAPS)),
                             hosts={khc.STUDIO_ROW: STUDIO_CAPS, "ace-ai": frozenset()})
    return khc.CapabilityTick(cmap, mode, (), ("ace-ai",))


def _row(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


@pytest.fixture(autouse=True)
def _studio_last_resort_on(kanban_home):
    """The live setting (hermes-home fleet/placement-policy.json since #10047): kanban on, mac-studio last."""
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    _write_policy(fleet)


def _kill_switch(home: Path, value) -> None:
    """``value`` False = host_preference.consumers.kanban off (the kill switch)."""
    _write_policy(home / "fleet", {**HOST_PREF, "consumers": {**HOST_PREF["consumers"],
                                                               "kanban": "on" if value else "off"}})


def _route(conn, tid, caps, plan=None):
    rules: dict = {}
    carry: dict = {}
    route = kbd._route_row(conn, _row(conn, tid), plan or _plan(free=2), caps=caps, rules_out=rules,
                           carry_out=carry)
    return route, rules.get(tid), carry.get(tid, False)


# -- rule 3: host: studio ------------------------------------------------------------------------------------

def test_bare_host_studio_is_dropped_and_placed_by_fwa(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio\nfix the lint")
        assert _route(conn, tid, _caps())[:2] == (("portable", None), "policy")


@pytest.mark.parametrize("requires", ["studio-state", "browser-session", "docker", "control-plane-read",
                                      "studio-state (the board DBs)"])
def test_host_studio_with_a_studio_reason_stays_local(kanban_home, requires):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body=f"host: studio\nrequires: {requires}")
        route, rule, _ = _route(conn, tid, _caps())
    assert route[0] == "local" and rule.startswith(("host_studio", "access_bound")), (route, rule)


def test_host_studio_with_a_reason_the_studio_does_not_serve_is_dropped(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio\nrequires: none")
        assert _route(conn, tid, _caps())[0] == ("portable", None)


def test_no_capability_map_honours_every_studio_pin(kanban_home):
    """No map this tick = the vocabulary is unknown: Studio work never moves on an unreadable file."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio")
        route, rule, _ = _route(conn, tid, None)
    assert route[0] == "local" and rule == "host_studio"


def test_kill_switch_restores_the_bare_studio_pin(kanban_home):
    _kill_switch(kanban_home, False)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio")
        route, rule, _ = _route(conn, tid, _caps())
    assert route[0] == "local" and rule == "host_studio"


def test_dropped_pin_keeps_the_prose_studio_signals(kanban_home):
    """The guard against sending Studio-only work remote: a dropped pin still meets studio_affinity."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha",
                             body="host: studio\nadd to cart in the signed-in browser")
        route, rule, _ = _route(conn, tid, _caps())
    assert route == ("local", "studio_affinity:studio_body") and rule == "studio_body"


def test_dropped_studio_pin_spawns_on_the_pool_while_the_studio_is_saturated(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio")
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
    assert (tid, "ace-ai") in spawned


def test_create_prints_the_drop(kanban_home, monkeypatch):
    monkeypatch.setattr(khc, "load_map", lambda fleet: _caps().cmap)
    note = kwp.mint_studio_pin_note("host: studio\nfix it", kanban_home / "fleet")
    assert note and "DROPPED" in note and "studio-state" in note
    assert kwp.mint_studio_pin_note("host: studio\nrequires: studio-state", kanban_home / "fleet") is None
    assert kwp.mint_studio_pin_note("host: ace-ai", kanban_home / "fleet") is None
    _kill_switch(kanban_home, False)
    assert kwp.mint_studio_pin_note("host: studio", kanban_home / "fleet") is None


# -- rule 2: local_files is continuity ------------------------------------------------------------------------

def _card_with_files(conn, body=None):
    tid = kb.create_task(conn, title="lf", assignee="alpha", body=body)
    path = kbw.resolve_workspace(kb.get_task(conn, tid))
    Path(path).mkdir(parents=True, exist_ok=True)
    (Path(path) / "notes.md").write_text("prior run's work\n")
    kbw.set_workspace_path(conn, tid, str(path))
    return tid, str(path)


@pytest.fixture
def carried(monkeypatch):
    calls = []
    monkeypatch.setattr(kwh, "carry_workspace", lambda host, ws, runner=None: calls.append((host.name, ws)))
    kwh._carry_scan_cache.clear()
    return calls


def test_local_files_runs_on_the_studio_while_its_gate_admits(kanban_home, carried):
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn)
        assert _route(conn, tid, None)[1:] == ("local_files", True)
        plan = _plan(free=2)
        plan.default_remote = True  # remote-first for ordinary portable cards; continuity keeps this one home
        _res, spawned = _tick(conn, spillover=plan, spawn_limit=2)
    assert (tid, None) in spawned and carried == []


def test_local_files_is_carried_to_the_pool_when_the_studio_is_full(kanban_home, carried):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                          (tid, kwh.CARRIED_EVENT)).fetchone()
    assert (tid, "ace-ai") in spawned
    assert carried == [("ace-ai", path)]
    assert json.loads(ev[0]) == {"host": "ace-ai", "workspace": path}


def test_local_files_carry_failure_is_a_spawn_failure_never_an_empty_dir_run(kanban_home, monkeypatch):
    def boom(host, ws, runner=None):
        raise RuntimeError("rsync rc=12")
    monkeypatch.setattr(kwh, "carry_workspace", boom)
    kwh._carry_scan_cache.clear()
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn)
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
    assert spawned == [] and tid in res.spawn_failed


def test_local_files_kill_switch_keeps_the_old_pin(kanban_home, carried):
    _kill_switch(kanban_home, False)
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn)
        assert _route(conn, tid, None) == (("local", None), "local_files", False)
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
    assert spawned == [] and carried == []


def test_local_files_too_big_to_carry_keeps_the_old_pin(kanban_home, carried, monkeypatch):
    monkeypatch.setattr(kwh, "carry_size_ok", lambda path: False)
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn)
        assert _route(conn, tid, None) == (("local", None), "local_files", False)


def test_local_files_with_another_studio_rule_stays_local(kanban_home, carried):
    """Only the files are continuity: a card some other rule keeps home is not carried."""
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn, body="add to cart in the signed-in browser")
        route, rule, carry = _route(conn, tid, None)
    assert route[0] == "local" and rule == "local_files" and not carry


def test_carry_size_ok_bounds(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    (d / "a").write_bytes(b"x" * 10)
    kwh._carry_scan_cache.clear()
    assert kwh.carry_size_ok(str(d))
    kwh._carry_scan_cache.clear()
    assert not kwh.carry_size_ok(str(d), max_bytes=5)
    kwh._carry_scan_cache.clear()
    assert not kwh.carry_size_ok(str(d), max_entries=0)
    assert not kwh.carry_size_ok(None)


def test_carry_workspace_runs_mkdir_then_rsync():
    calls = []

    class P:
        returncode, stderr = 0, ""

    def runner(argv, **kw):
        calls.append(argv)
        return P()
    host = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user=kwp.SSH_USER, slots=1, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True, priority=0)
    ws = "/Volumes/fleet-scratch/workspaces/default/t_1"
    kwh.carry_workspace(host, ws + "/", runner=runner)
    assert calls[0][0] == "ssh" and f"mkdir -p -m 0770 -- {ws}" in calls[0][-1]
    assert calls[1][:2] == ["rsync", "-a"]
    assert calls[1][-2:] == [f"{ws}/", f"{host.target}:{ws}/"]


# -- rule 1 / 5: worktree targets ------------------------------------------------------------------------------

def _wt_card(conn, anchor, target_leaf=None, make=False):
    tid = kb.create_task(conn, title="wt", assignee="alpha", workspace_kind="worktree",
                         workspace_path=str(anchor))
    target = anchor / ".worktrees" / (target_leaf or tid)
    if make:
        target.mkdir(parents=True)
    conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(target), tid))
    conn.commit()
    return tid


def test_a_not_yet_made_worktrees_target_ports(kanban_home, anchor):
    _mirrors(kanban_home)
    with kb.connect_closing() as conn:
        tid = _wt_card(conn, anchor)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("portable", None)
    assert out[tid][0] == SLUG


@pytest.mark.parametrize("leaf,make", [(None, True), ("someone-elses-name", False)])
def test_an_existing_or_foreign_worktrees_target_stays_local(kanban_home, anchor, leaf, make):
    _mirrors(kanban_home)
    with kb.connect_closing() as conn:
        tid = _wt_card(conn, anchor, leaf, make)
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules)[0] == "local"
    assert rules[tid] == "workspace_kind"


def test_create_rewrites_a_portable_worktree_target(kanban_home, anchor):
    _mirrors(kanban_home)
    path, note = kwt.mint_rewrite("worktree", str(anchor / ".worktrees" / "fix-lint"), kanban_home / "fleet")
    assert path == str(anchor.resolve()) and note.startswith("workspace rewritten:") and SLUG in note
    path, note = kwt.mint_rewrite("worktree", str(anchor), kanban_home / "fleet")
    assert path == str(anchor) and "portable" in note


def test_create_says_why_a_worktree_stays_local(kanban_home, anchor):
    _mirrors(kanban_home, slug="ANG-Ventures/something-else")
    target = str(anchor / ".worktrees" / "fix-lint")
    path, note = kwt.mint_rewrite("worktree", target, kanban_home / "fleet")
    assert path == target and "stays Studio-local" in note and SLUG in note
    assert kwt.mint_rewrite("scratch", None, kanban_home / "fleet") == (None, None)


# -- scope add 2 (Apollo 21:3x): does the CLI one-shot gate zero REMOTE spawns? ------------------------------

def test_a_paused_local_gate_still_places_a_portable_card_remote(kanban_home):
    """What the CLI passes on a saturated Studio (spawn_paused=<reason>, spawn_limit=0) caps LOCAL spawns only:
    with a pool plan that has room, a portable card is placed remote this call."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="p", assignee="alpha")
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0,
                             spawn_paused="utilization: saturated:cpu -0.10 (binding cpu)")
    assert (tid, "ace-ai") in spawned


# -- item 7: the replay of a window of placement events ----------------------------------------------------------

def test_replay_counts_studio_placements_before_and_after(kanban_home, monkeypatch, anchor):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "kanban_studio_pin_replay", Path(__file__).resolve().parents[2] / "scripts" / "kanban_studio_pin_replay.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    monkeypatch.setattr(khc, "load_map", lambda fleet: _caps().cmap)
    kwh._carry_scan_cache.clear()
    _mirrors(kanban_home)
    with kb.connect_closing() as conn:
        bare = kb.create_task(conn, title="a", assignee="alpha", body="host: studio\nfix it")
        kept = kb.create_task(conn, title="b", assignee="alpha", body="host: studio\nrequires: studio-state")
        lf, _ = _card_with_files(conn)
        wt = _wt_card(conn, anchor)
        remote = kb.create_task(conn, title="r", assignee="alpha")
        for tid, route, rule in ((bare, "local", "host_studio"), (bare, "local", "host_studio"),
                                 (kept, "local", "host_studio"), (lf, "local", "local_files"),
                                 (wt, "local", "workspace_kind"), (remote, "remote-policy", "policy"),
                                 (remote, "local", "default")):
            kb._append_event(conn, tid, "placement", {"route": route, "rule": rule})
        conn.commit()
    res = rp.replay(kanban_home / "kanban.db", 24, kanban_home / "fleet")
    assert res["placements"] == 7 and res["studio_before"] == 6
    assert res["before"] == {"host_studio": 3, "local_files": 1, "workspace_kind": 1, "default": 1}
    assert res["after"] == {"fwa": 3, "host_studio": 1, "studio-while-room": 1, "default": 1}
    assert res["studio_after_pinned"] == 2
    assert res["moved"]["host_studio->fwa"] == [bare]
    assert "host_studio" in rp.render(res)
