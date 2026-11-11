"""The Studio runs a kanban card only for a NAMED Studio-only capability (t_9965b8d5, Ace 2026-11-10).

24 h before this rule: 242 of 639 placements on the Studio; 66 needed it. Rules pinned here:

1. ``local_files`` / ``workspace_kind`` are not capabilities: files are carried, a worktree ports, always, not
   only when the Studio is full; what cannot move WAITS, named.
2. A portable card the pool refuses WAITS; it never takes the Studio as last resort.
3. Every Studio placement records which capability justified it and what established it.
4. A body-sniffed ``studio-state`` (``kanban.db``, ``~/.hermes/state/``) no longer pins; ``requires:`` does.
   ``host: studio`` holds only with a Studio-ONLY word (fleet-ssh, docker are on pool hosts too).
5. Kill switch ``kanban.placement.studio_capability_only: false`` restores the behaviour before the card.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_worker_hosts as kwh

from tests.hermes_cli.test_kanban_worker_pool import _plan, _tick, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_worktree_port import anchor  # noqa: F401
from tests.hermes_cli.test_kanban_studio_pins_and_carry import (  # noqa: F401
    _card_with_files, _row, _studio_last_resort_on, carried,
)

STUDIO_ROW = frozenset({"browser-session", "studio-state", "control-plane", "fleet-ssh", "docker"})
POOL_ROW = frozenset({"fleet-ssh", "docker"})


def _cmap():
    caps = tuple(sorted(STUDIO_ROW))
    return khc.CapabilityMap(
        capabilities=caps, hosts={khc.STUDIO_ROW: STUDIO_ROW, "ace-ai": POOL_ROW},
        sniff=(("studio-state", __import__("re").compile(r"(?<![\w-])kanban\.db(?![\w-])")),),
        soft={c: False for c in caps})


def _caps(mode=khc.ENFORCE):
    return khc.CapabilityTick(_cmap(), mode, tuple(sorted(STUDIO_ROW)), ("ace-ai",))


def _knob(home, value):
    (home / "config.yaml").write_text(
        "kanban:\n  studio_pin_requires_reason: false\n"
        + (f"  placement:\n    studio_capability_only: {str(value).lower()}\n" if value is not None else ""))


def _route(conn, tid, caps=None, plan=None):
    import inspect
    rules, studio = {}, {}
    kw = {"studio_out": studio} if "studio_out" in inspect.signature(kbd._route_row).parameters else {}
    route = kbd._route_row(conn, _row(conn, tid), plan or _plan(free=2), caps=caps or _caps(), rules_out=rules,
                           carry_out={}, **kw)
    return route, rules.get(tid), studio.get(tid, {})


# -- 1. files are no capability ----------------------------------------------------------------------------------

def test_local_files_is_carried_even_while_the_studio_has_room(kanban_home, carried, monkeypatch):
    """Before: 'local while the gate admits, else carried' (101 of 242 Studio placements). Now: always carried."""
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        plan = _plan(free=2)
        plan.default_remote = True
        _res, spawned = _tick(conn, spillover=plan, spawn_limit=2)
    assert (tid, "ace-ai") in spawned and (tid, None) not in spawned
    assert carried == [("ace-ai", path)]


def test_local_files_the_carry_cannot_move_waits_named(kanban_home, carried, monkeypatch):
    monkeypatch.setattr(kwh, "carry_size_ok", lambda path, **_kw: False)
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn)
        route, rule, studio = _route(conn, tid)
    assert route[0] == "wait" and route[1].startswith("studio_capability_only:local_files"), route
    assert not studio


def test_unpushed_dirty_worktree_waits_and_names_the_dirty_path_count(kanban_home, anchor):
    """A worktree is not a capability: a dirty one cannot port, so it waits with the count of dirty paths."""
    from tests.hermes_cli.test_kanban_worktree_port import _commit, _git, _worktree_card
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        tree = anchor / ".worktrees" / tid
        _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(tree), cwd=anchor)
        (tree / "a.txt").write_text("x")
        (tree / "b.txt").write_text("y")
        route, rule, _ = _route(conn, tid)
    assert rule == "workspace_kind"
    assert route == ("wait", "studio_capability_only:workspace_kind "
                             "(not_portable:workspace_kind:unpushed_dirty_tree, 2 dirty paths)"), route


# -- 2. no Studio fallback for a portable card ------------------------------------------------------------------

def test_a_portable_card_the_pool_refuses_waits_never_takes_the_studio(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="p", assignee="alpha", body="fix the lint")
        plan = _plan(free=0)
        plan.default_remote = True
        res, spawned = _tick(conn, spillover=plan, spawn_limit=2)
    assert spawned == [], spawned
    assert tid in res.placement_waits


# -- 3. the placement record names the capability ---------------------------------------------------------------

def test_studio_placement_records_the_capability_and_its_source(kanban_home, monkeypatch):
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="s", assignee="alpha", body="host: studio\nrequires: studio-state")
        _res, spawned = _tick(conn, spillover=_plan(free=0), spawn_limit=2)
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='placement'", (tid,)).fetchone()
    assert (tid, None) in spawned
    rec = json.loads(ev[0])["studio_capability"]
    assert rec["capability"] == "studio-state" and rec["source"] == "declared", rec


# -- 4. body words and pool-served words no longer pin -----------------------------------------------------------

def test_a_kanban_db_mention_no_longer_pins_studio_state(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="b", assignee="alpha", body="fix how create reads kanban.db rows")
        route, _rule, _ = _route(conn, tid)
    assert route == ("portable", None), route


def test_an_explicit_requires_studio_state_still_runs_on_the_studio(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="b", assignee="alpha", body="requires: studio-state\nread kanban.db")
        route, rule, studio = _route(conn, tid)
    assert route[0] == "local" and rule == "requires:studio-state", (route, rule)
    assert studio["capability"] == "studio-state" and studio["source"] == "declared"


@pytest.mark.parametrize("word", ["fleet-ssh", "docker"])
def test_host_studio_with_a_pool_served_word_is_dropped(kanban_home, word):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="h", assignee="alpha", body=f"host: studio\nrequires: {word}")
        route, _rule, _ = _route(conn, tid)
    assert route[0] != "local", route


def test_host_studio_with_a_studio_only_word_stays(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="h", assignee="alpha", body="host: studio\nrequires: control-plane")
        route, _rule, studio = _route(conn, tid)
    assert route[0] == "local" and studio["capability"] == "control-plane", (route, studio)


def test_an_unknown_assignee_is_structural_not_waited(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="u", assignee="nobody-serves-me", body="fix it")
        route, rule, studio = _route(conn, tid)
    assert route[0] == "local" and rule == "profile" and studio["kind"] == "structural", (route, rule, studio)


# -- 5. kill switch --------------------------------------------------------------------------------------------

def test_kill_switch_restores_local_files_continuity(kanban_home, carried, monkeypatch):
    _knob(kanban_home, False)
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn)
        plan = _plan(free=2)
        plan.default_remote = True
        _res, spawned = _tick(conn, spillover=plan, spawn_limit=2)
    assert (tid, None) in spawned and carried == []


def test_kill_switch_restores_the_body_sniff_and_the_pool_word_pin(kanban_home):
    _knob(kanban_home, False)
    with kb.connect_closing() as conn:
        sniff = kb.create_task(conn, title="b", assignee="alpha", body="fix how create reads kanban.db rows")
        ssh = kb.create_task(conn, title="h", assignee="alpha", body="host: studio\nrequires: fleet-ssh")
        assert _route(conn, sniff)[0][0] == "local"
        assert _route(conn, ssh)[0][0] == "local"


def test_no_capability_map_leaves_the_rule_off(kanban_home, carried):
    """An unreadable map = the vocabulary is unknown: nothing moves on a broken file (studio_pin_reasons' rule)."""
    with kb.connect_closing() as conn:
        tid, _ = _card_with_files(conn)
        rules: dict = {}
        route = kbd._route_row(conn, _row(conn, tid), _plan(free=2), caps=None, rules_out=rules, carry_out={})
    assert route[0] != "wait" or not str(route[1]).startswith("studio_capability_only"), route


# -- 1b. over the carry limit: carry everything but the git clones, re-clone those (operator 2026-11-10 15:3x) -----
# t_b41d322b ran on the Studio under local_files with a 1.1 GB scratch dir (four repo clones), over CARRY_MAX_BYTES.

def _clone_in(ws, name="repo", *, dirty=False, ahead=False):
    import subprocess
    from pathlib import Path
    repo = Path(ws) / name
    repo.mkdir()

    def g(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / "big.bin").write_bytes(b"x" * 20000)
    g("add", "big.bin")
    g("commit", "-q", "-m", "base")
    g("remote", "add", "origin", "https://github.com/acme/widget.git")
    g("update-ref", "refs/remotes/origin/main", "HEAD")
    if ahead:
        (repo / "more.txt").write_text("unpushed\n")
        g("add", "more.txt")
        g("commit", "-q", "-m", "unpushed")
    if dirty:
        (repo / "wip.txt").write_text("uncommitted\n")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                         text=True).stdout.strip()
    return sha


@pytest.fixture
def small_carry(monkeypatch):
    """CARRY_MAX_BYTES scaled down: the 20 kB clone puts the dir over, notes.md alone fits."""
    kwh._carry_scan_cache.clear()
    getattr(kwh, "_carry_clone_cache", {}).clear()
    monkeypatch.setitem(kwh.carry_size_ok.__kwdefaults__, "max_bytes", 8192)
    if hasattr(kwh, "carry_split"):  # absent before the split (red-first run on the base)
        monkeypatch.setitem(kwh.carry_split.__kwdefaults__, "max_bytes", 8192)


def _route_carry(conn, tid):
    carry, rules = {}, {}
    route = kbd._route_row(conn, _row(conn, tid), _plan(free=2), caps=_caps(), rules_out=rules, carry_out=carry,
                           studio_out={})
    return route, carry.get(tid)


def test_over_limit_workspace_carries_without_its_clones(kanban_home, small_carry):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        sha = _clone_in(path)
        assert not kwh.carry_size_ok(path)  # the plain carry refuses it, as for t_b41d322b
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait" and route[0] != "local", route
    assert plan["exclude"] == ("repo",), plan
    assert plan["clones"] == [{"rel": "repo", "slug": "acme/widget", "branch": "main", "sha": sha, "push": None}]


def test_over_limit_clone_with_unpushed_commits_is_pushed_first(kanban_home, small_carry):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        sha = _clone_in(path, ahead=True)
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait", route
    push = plan["clones"][0]["push"]
    assert push[1:] == ("main", push[2], sha) and push[2] and push[2] != sha, push


def test_over_limit_with_a_dirty_clone_waits_named(kanban_home, small_carry):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path, dirty=True)
        route, plan = _route_carry(conn, tid)
    assert plan is None
    assert route[0] == "wait" and route[1].startswith("studio_capability_only:local_files"), route
    assert "clone repo cannot be rebuilt from origin: dirty_tree" in route[1], route


def test_over_limit_without_clones_waits_named(kanban_home, small_carry):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        (__import__("pathlib").Path(path) / "blob.bin").write_bytes(b"y" * 20000)
        route, plan = _route_carry(conn, tid)
    assert plan is None and route[0] == "wait", route
    assert "over the carry limit even without its git clones" in route[1], route


def test_over_limit_spawn_carries_minus_clones_then_reclones_on_the_host(kanban_home, small_carry, monkeypatch):
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    calls = []
    monkeypatch.setattr(kwh, "carry_workspace",
                        lambda host, ws, runner=None, exclude=(): calls.append(("carry", host.name, ws, exclude)))
    monkeypatch.setattr(kwh, "carry_clones",
                        lambda host, ws, clones, fleet_dir, runner=None: calls.append(
                            ("clone", host.name, ws, [c["rel"] for c in clones])))
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        sha = _clone_in(path)
        plan = _plan(free=2)
        plan.default_remote = True
        _res, spawned = _tick(conn, spillover=plan, spawn_limit=2)
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                          (tid, kwh.CARRIED_EVENT)).fetchone()
    assert (tid, "ace-ai") in spawned and (tid, None) not in spawned, spawned
    assert calls == [("carry", "ace-ai", path, ("repo",)), ("clone", "ace-ai", path, ["repo"])], calls
    assert json.loads(ev[0])["recloned"] == [{"rel": "repo", "slug": "acme/widget", "branch": "main", "sha": sha}]


def test_carry_workspace_excludes_the_clone_roots(kanban_home):
    argvs = []

    class _P:
        returncode = 0
        stdout = stderr = ""

    def runner(argv, **_kw):
        argvs.append(argv)
        return _P()
    from tests.hermes_cli.test_kanban_worker_pool import _host
    host = _host()
    kwh.carry_workspace(host, "/srv/ws/t_x", runner=runner, exclude=("repo", "sub/other"))
    rsync = next(a for a in argvs if a[0] == "rsync")
    assert "--exclude=/repo/" in rsync and "--exclude=/sub/other/" in rsync, rsync
    assert rsync.index("--exclude=/repo/") < rsync.index("--"), rsync


def test_kill_switch_keeps_the_over_limit_card_on_the_old_rule(kanban_home, small_carry):
    _knob(kanban_home, False)
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        route, plan = _route_carry(conn, tid)
    assert plan is None and route[0] == "local", route


def test_carry_clones_rebuilds_the_clone_at_the_same_commit(kanban_home, tmp_path, monkeypatch):
    """The host-side script, run locally: ssh is stripped from the argv and origin is a local bare repo."""
    import subprocess
    from hermes_cli import kanban_worktree_port as kwt
    from tests.hermes_cli.test_kanban_worker_pool import _host
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    src = tmp_path / "src"
    subprocess.run(["git", "clone", "-q", str(origin), str(src)], check=True, capture_output=True)
    for args in (("config", "user.email", "t@t"), ("config", "user.name", "t")):
        subprocess.run(["git", "-C", str(src), *args], check=True)
    (src / "a.txt").write_text("1\n")
    subprocess.run(["git", "-C", str(src), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "one"], check=True)
    subprocess.run(["git", "-C", str(src), "push", "-q", "origin", "main"], check=True, capture_output=True)
    first = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], check=True, capture_output=True,
                           text=True).stdout.strip()
    (src / "a.txt").write_text("2\n")
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-am", "two"], check=True)
    subprocess.run(["git", "-C", str(src), "push", "-q", "origin", "main"], check=True, capture_output=True)

    real_clone_script = kwt.clone_script
    monkeypatch.setattr(kwt, "clone_script", lambda w, slug, m, b: real_clone_script(w, slug, m, b,
                                                                                      url=str(origin)))

    def local(argv, **kw):
        if argv[0] == "ssh":
            argv = ["sh", "-c", argv[-1]]
        return subprocess.run(argv, **{k: v for k, v in kw.items() if k != "stdin"})
    ws = tmp_path / "ws"
    ws.mkdir()
    kwh.carry_clones(_host(), str(ws), [{"rel": "repo", "slug": "acme/widget", "branch": "main", "sha": first,
                                         "push": None}], kanban_home / "fleet", runner=local)
    head = subprocess.run(["git", "-C", str(ws / "repo"), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    assert head == first  # the Studio's commit, not origin's newer tip
    assert (ws / "repo" / "a.txt").read_text() == "1\n"
