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
    assert json.loads(ev[0]) == {"host": "ace-ai", "workspace": path, "path": path}


# -- Prism 86a85c6c299a / 6c03238719d2: a carried card is bound to the host holding the copy -------------------

@pytest.mark.parametrize("continuation_on_studio_room", [True, False])
def test_a_carried_card_reruns_on_the_carry_host_never_on_the_stale_studio_copy(kanban_home, carried,
                                                                              continuation_on_studio_room):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
        assert carried == [("ace-ai", path)]
        # the run ends; the card is ready again (retry / requeue) with the Studio copy still present
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        conn.commit()
        route, rule, carry = _route(conn, tid, None)
        assert route == ("pin", "ace-ai") and rule.startswith("bind_host:ace-ai") and not carry, (route, rule)
        kw = {"spawn_limit": 2} if continuation_on_studio_room else {"spawn_limit": 0, "spawn_paused": "x"}
        _res, spawned = _tick(conn, spillover=_plan(free=2), **kw)
    assert (tid, "ace-ai") in spawned and (tid, None) not in spawned
    assert carried == [("ace-ai", path)]  # never carried a second time over the remote work


def test_a_carried_card_whose_host_is_gone_waits_never_runs_local(kanban_home, carried):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        kb._append_event(conn, tid, kwh.CARRIED_EVENT, {"host": "ace-ai", "workspace": path, "path": path})
        conn.commit()
        plan = _plan(free=2)
        plan.config = plan.config.__class__(**{**plan.config.__dict__, "profiles": ()})  # no host takes alpha
        route, _rule, _ = _route(conn, tid, None, plan)
    assert route[0] in ("wait", "pin") and route != ("local", None)


def test_a_repointed_carried_card_is_unbound(kanban_home, carried):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        kb._append_event(conn, tid, kwh.CARRIED_EVENT, {"host": "ace-ai", "workspace": "/elsewhere"})
        conn.commit()
        assert kwh.carried_host(conn, tid, path) is None


# -- Prism e8e4061341d6: one remote-first predicate for the spawn loop and the door publisher ------------------

def test_door_publisher_waits_a_continuity_row_where_the_spawn_loop_starts_it(kanban_home):
    from hermes_cli.kanban_priority import waiting_by_host
    plan = _plan(free=2)
    plan.default_remote = True
    cards = [{"id": "lf", "assignee": "alpha", "asap": False}, {"id": "p", "assignee": "alpha", "asap": False}]
    routes = {"lf": ("portable", None), "p": ("portable", None)}
    w = waiting_by_host(cards, plan, routes=routes, local_host="studio", continuity={"lf": True})
    assert [c["id"] for c in w["studio"]] == ["lf"]
    assert all(c["id"] != "p" for c in w["studio"])
    assert kbd._portable_goes_remote(True, None, plan, continuity=True) is False
    assert kbd._portable_goes_remote(True, None, plan) is True


# -- Prism a39b11fdf16d (P0): nothing caller-controlled reaches a remote shell unquoted ------------------------

HOSTILE = ["/tmp/carry;touch /tmp/carry-pwn;#", "/tmp/w; rm -rf ~", "/tmp/$(touch /tmp/pwn)", "/tmp/`id`",
           "/tmp/a b", "/tmp/-rf", "/tmp/../etc", "/tmp/x\nid", "relative/path", "/tmp/a|b", "/tmp/a&b"]


@pytest.mark.parametrize("path", HOSTILE)
def test_carry_refuses_a_shell_active_path_before_any_remote_command(path):
    calls = []
    host = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user=kwp.SSH_USER, slots=1, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True, priority=0)
    assert not kwh.carry_path_safe(path)
    kwh._carry_scan_cache.clear()
    assert not kwh.carry_size_ok(path)
    with pytest.raises(RuntimeError, match="shell-inert"):
        kwh.carry_workspace(host, path, runner=lambda argv, **kw: calls.append(argv))
    assert calls == []


def test_carry_path_safe_admits_real_workspace_paths():
    assert kwh.carry_path_safe("/Volumes/fleet-scratch/workspaces/default/t_e604c0f9")
    assert kwh.carry_path_safe("/home/kanbanw/.hermes/kanban/boards/x-y/workspaces/t_1/")


def test_mkdir_quotes_hostile_paths():
    """prepare_remote_workspace (the other remote command in this diff) shlex-quotes the path."""
    import shlex as _sh
    calls = []

    class P:
        returncode, stderr = 0, ""
    host = kwp.PoolHost(name="ace-ai", ssh_host="ace-ai", ssh_user=kwp.SSH_USER, slots=1, capacity_pct=0.8,
                        absence="required", profiles=("alpha",), state="active", enabled=True, priority=0)
    for path in HOSTILE[:4]:
        kwh.prepare_remote_workspace(host, path, runner=lambda argv, **kw: (calls.append(argv), P())[1])
        assert f"-- {_sh.quote(path)} " in calls[-1][-1]


# -- Prism 7eba47409a75: the kill switch restores the old worktree-target pin and the old create output --------

def test_kill_switch_keeps_a_not_yet_made_worktrees_target_local(kanban_home, anchor):
    _mirrors(kanban_home)
    _kill_switch(kanban_home, False)
    with kb.connect_closing() as conn:
        tid = _wt_card(conn, anchor)
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules)[0] == "local"
    assert rules[tid] == "workspace_kind"
    assert kwt.anchor_repo(tid, str(anchor / ".worktrees" / tid), None) is None


def test_kill_switch_leaves_kanban_create_byte_for_byte(kanban_home, anchor):
    _mirrors(kanban_home)
    _kill_switch(kanban_home, False)
    target = str(anchor / ".worktrees" / "fix-lint")
    assert kwt.mint_rewrite("worktree", target, kanban_home / "fleet") == (target, None)
    assert kwt.mint_rewrite("worktree", str(anchor), kanban_home / "fleet") == (str(anchor), None)


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


# -- Prism round 3 (ea59f384dc) ------------------------------------------------------------------------------

def test_a_studio_pin_a_skill_needs_is_kept(kanban_home, monkeypatch):
    """Prism f5bbe97bc028: a bare `host: studio` on a card whose ATTACHED skill needs an identity-only Studio
    capability (docker) is justified. Dropping it would make the card an unpinned docker card: wait forever."""
    cmap = khc.CapabilityMap(capabilities=tuple(sorted(STUDIO_CAPS)),
                             hosts={khc.STUDIO_ROW: STUDIO_CAPS, "ace-ai": frozenset()},
                             index={"docker-ops": ("docker",)}, identity_only=frozenset({"docker"}))
    caps = khc.CapabilityTick(cmap, khc.ENFORCE, ("docker",), ("ace-ai",))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", assignee="alpha", body="host: studio\nrun the compose stack",
                             skills=["docker-ops"])
        route, rule, _ = _route(conn, tid, caps)
        bare = kb.create_task(conn, title="y", assignee="alpha", body="host: studio\nfix the lint")
        assert _route(conn, bare, caps)[0] == ("portable", None)  # no such skill: still dropped
    # The skill's need is enforced by the capability verdict first, so the card is held local under that rule.
    assert route == ("local", "studio_affinity:requires:docker") and rule == "requires:docker", (route, rule)
    monkeypatch.setattr(khc, "load_map", lambda fleet: cmap)
    assert kwp.mint_studio_pin_note("host: studio", kanban_home / "fleet", ["docker-ops"]) is None
    assert kwp.mint_studio_pin_note("host: studio", kanban_home / "fleet") is not None


def test_carried_host_reads_the_persisted_spelling(kanban_home):
    """Prism 34bd0265a601: custody holds when the row now carries the spelling the claim persisted ("path"),
    not the one stored at carry time ("workspace")."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        kb._append_event(conn, tid, kwh.CARRIED_EVENT, {"host": "ace-ai", "workspace": "/old/spelling", "path": path})
        conn.commit()
        assert kwh.carried_host(conn, tid, path) == "ace-ai"
        assert kwh.carried_host(conn, tid, "/old/spelling") == "ace-ai"
        assert kwh.carried_host(conn, tid, None) is None


def test_a_shared_workspace_is_never_carried(kanban_home, carried):
    """Prism e25ae3385b2f: a carry copies onto the same path on the pool host, so a directory another card points
    at (or already carried) is not carried; that card keeps the old local_files rule."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        other = kb.create_task(conn, title="o", assignee="alpha")
        kbw.set_workspace_path(conn, other, path)
        assert kwh.carry_path_shared(conn, tid, path)
        # The shared-workspace pin fires before local_files: local, not carried.
        assert _route(conn, tid, None) == (("local", None), f"workspace_shared:{other}", False)
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu")
    assert carried == [] and all(t != tid for t, _h in spawned)


def test_a_workspace_another_card_carried_is_not_carried_again(kanban_home, carried):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        other = kb.create_task(conn, title="o", assignee="alpha")
        kb._append_event(conn, other, kwh.CARRIED_EVENT, {"host": "ace-ai", "workspace": "/x", "path": path})
        conn.commit()
        assert kwh.carry_path_shared(conn, tid, path)
        assert not kwh.carry_path_shared(conn, other, "/somewhere/else")
        assert _route(conn, tid, None)[1:] == ("local_files", False)


def test_a_carried_card_waits_when_there_is_no_pool_plan(kanban_home, carried):
    """Prism fd679a4c5702: with no plan this tick the bind is never computed; custody still holds, so the card
    waits pool_unavailable instead of running on the Studio's stale copy."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        kb._append_event(conn, tid, kwh.CARRIED_EVENT, {"host": "ace-ai", "workspace": path, "path": path})
        plain = kb.create_task(conn, title="p", assignee="alpha")
        conn.commit()
        _res, spawned = _tick(conn, spillover=None, spawn_limit=4)
    assert (tid, None) not in spawned and (plain, None) in spawned
