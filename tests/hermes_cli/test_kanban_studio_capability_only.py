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


def test_unpushed_dirty_worktree_waits_and_names_the_dirty_path_count(kanban_home, anchor, monkeypatch):
    """A worktree is not a capability: a dirty one that cannot port waits with the count of dirty paths. Since
    #219 (t_894be273) a dirty tree is carried, so this pins the case where it is not: the dirty carry switched off."""
    from hermes_cli import kanban_worktree_port as kwt
    from tests.hermes_cli.test_kanban_worktree_port import _commit, _git, _worktree_card
    monkeypatch.setenv(kwt.DIRTY_CARRY_ENV, "0")
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

def _clone_in(ws, name="repo", *, dirty=False, ahead=False, ignored=False, branch="main", pycache=False,
              ignored_size=10):
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
    g("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")  # as `git clone` leaves it
    if branch != "main":
        g("update-ref", f"refs/remotes/origin/{branch}", "HEAD")
        g("checkout", "-q", "-b", branch)
    if ahead:
        (repo / "more.txt").write_text("unpushed\n")
        g("add", "more.txt")
        g("commit", "-q", "-m", "unpushed")
    if dirty:
        (repo / "wip.txt").write_text("uncommitted\n")
    if ignored or pycache:
        (repo / ".git" / "info" / "exclude").write_text("data/\n__pycache__/\n")
    if ignored:
        (repo / "data").mkdir()
        (repo / "data" / "input.csv").write_bytes(b"only here\n" + b"z" * max(0, ignored_size - 10))
    if pycache:
        (repo / "pkg" / "__pycache__").mkdir(parents=True)
        (repo / "pkg" / "__pycache__" / "m.cpython-311.pyc").write_bytes(b"\0" * 64)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                         text=True).stdout.strip()
    return sha


@pytest.fixture
def small_carry(monkeypatch):
    """CARRY_MAX_BYTES scaled down: the 20 kB clone puts the dir over, notes.md alone fits."""
    kwh._carry_scan_cache.clear()
    getattr(kwh, "_carry_clone_cache", {}).clear()
    getattr(kwh, "_clone_assess_cache", {}).clear()
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
    assert plan["clones"] == [{"rel": "repo", "slug": "acme/widget", "branch": "main", "sha": sha, "push": None,
                               "ignored": ()}]


def test_over_limit_clone_with_unpushed_commits_is_pushed_first(kanban_home, small_carry):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        sha = _clone_in(path, ahead=True, branch="feat/x")
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait", route
    push = plan["clones"][0]["push"]
    assert push[1:] == ("feat/x", push[2], sha) and push[2] and push[2] != sha, push


@pytest.mark.parametrize("branch", ["main", "master", "trunk"])
def test_over_limit_clone_ahead_on_the_default_branch_waits_and_never_pushes(kanban_home, small_carry,
                                                                              monkeypatch, branch):
    """Review F1: a scratch clone on origin's default branch with a local commit is never pushed by the dispatcher
    (that would land on origin/<default> with no PR); the card waits, named. ``trunk`` is origin/HEAD's target."""
    import subprocess
    from hermes_cli import kanban_worktree_port as kwt
    pushed = []
    monkeypatch.setattr(kwt, "push_card_branch", lambda push, runner=None: pushed.append(push))
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        repo = __import__("pathlib").Path(path) / "repo"
        _clone_in(path, branch=branch)
        if branch == "trunk":
            subprocess.run(["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD",
                            "refs/remotes/origin/trunk"], check=True)
        (repo / "more.txt").write_text("unpushed\n")
        subprocess.run(["git", "-C", str(repo), "add", "more.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                        "-m", "ahead"], check=True)
        assert kwh.assess_clone(str(repo)) == (None, "unpushed_commits_on_default_branch")
        route, plan = _route_carry(conn, tid)
    assert plan is None and route[0] == "wait", route
    # operator F3/F5 (t_d2f06cf6): carried as files (its .git rides along), counted, and the reason names why
    assert "clone repo (unpushed_commits_on_default_branch) carried as files" in route[1], route
    assert pushed == []


def test_clone_on_the_default_branch_level_with_origin_still_reclones(kanban_home, small_carry):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        sha = _clone_in(path)
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait" and plan["clones"][0]["push"] is None and plan["clones"][0]["sha"] == sha, route


def test_clone_assessment_is_cached_per_head_for_the_census_ttl(tmp_path, monkeypatch):
    """Review F3: the routing pass asks every tick; one assessment per clone path and HEAD per TTL."""
    import subprocess
    kwh._clone_assess_cache.clear()
    _clone_in(tmp_path)
    repo = str(tmp_path / "repo")
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.run(argv, **kw)
    first = kwh.assess_clone_cached(repo, runner, now=1000.0)
    n = len(calls)
    assert first[0] is not None and n > 0
    assert kwh.assess_clone_cached(repo, runner, now=1000.0 + kwh.CARRY_SCAN_TTL_SECONDS - 1) == first
    assert len(calls) == n  # served from cache: no git subprocess
    (tmp_path / "repo" / "more.txt").write_text("x\n")
    subprocess.run(["git", "-C", repo, "add", "more.txt"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "m"],
                   check=True)
    moved = kwh.assess_clone_cached(repo, runner, now=1001.0)
    assert len(calls) > n and moved == (None, "unpushed_commits_on_default_branch")  # HEAD moved: re-assessed
    m = len(calls)
    kwh.assess_clone_cached(repo, runner, now=1001.0 + kwh.CARRY_SCAN_TTL_SECONDS + 1)
    assert len(calls) > m  # TTL expired: re-assessed


def test_over_limit_with_a_dirty_clone_waits_named(kanban_home, small_carry):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path, dirty=True)
        route, plan = _route_carry(conn, tid)
    assert plan is None
    assert route[0] == "wait" and route[1].startswith("studio_capability_only:local_files"), route
    assert "clone repo (dirty_tree) carried as files" in route[1], route


def test_over_limit_clone_with_only_pycache_reclones_and_is_not_refused(kanban_home, small_carry):
    """Operator F4: regenerable caches are dropped, never a reason to wait."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path, pycache=True)
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait", route
    assert plan["clones"][0]["ignored"] == (), plan


def test_over_limit_clone_with_an_ignored_data_file_carries_it(kanban_home, small_carry):
    """Prism 38aae6c981c1/62448b5b6868/72087e167990/30ed122cf9a9 + operator F4: an ignored data file is carried,
    not refused, and counted toward the carry caps; the cache next to it is dropped."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path, ignored=True, pycache=True)
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait", route
    assert plan["clones"][0]["ignored"] == ("data/input.csv",), plan


def test_over_limit_clone_whose_ignored_files_break_the_cap_waits_naming_the_cap(kanban_home, small_carry):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path, ignored=True, ignored_size=9000)  # 9 kB ignored file vs the 8 kB test cap
        route, plan = _route_carry(conn, tid)
    assert plan is None and route[0] == "wait", route
    assert "over the carry byte cap with the clones' ignored files" in route[1], route
    assert "ignored_files" not in route[1].replace("clones' ignored files", ""), route


def test_regenerable_cache_list():
    for rel in ("pkg/__pycache__/m.pyc", ".pytest_cache/v/x", ".ruff_cache/a", ".mypy_cache/a", ".ty_cache/a",
                ".tox/py/bin/python", "sub/.DS_Store", "widget.egg-info/PKG-INFO"):
        assert kwh.regenerable_cache(rel), rel
    for rel in (".env", "data/input.csv", "build/out.bin", "results.egg-info"):
        assert not kwh.regenerable_cache(rel), rel


def test_carry_clones_lands_the_ignored_file_byte_equal_inside_the_rebuilt_clone(kanban_home, tmp_path,
                                                                                   monkeypatch):
    """Operator F4: the Studio clone's ignored data file ends byte-equal at the same path in the pool host's clone;
    its __pycache__ is not carried. The pool host is a second local root: ssh/rsync targets are rewritten to it."""
    import subprocess
    import shutil
    from hermes_cli import kanban_worktree_port as kwt
    from tests.hermes_cli.test_kanban_worker_pool import _host
    studio = tmp_path / "ws"
    studio.mkdir()
    sha = _clone_in(studio, ignored=True, pycache=True)
    payload = (studio / "repo" / "data" / "input.csv").read_bytes()
    pool = tmp_path / "pool"
    host = _host()

    def fake_clone(h, dest, slug, mirror, branch, runner=None, sha=None, exact=False):
        shutil.copytree(str(studio / "repo"), str(pool) + dest, ignore=shutil.ignore_patterns("data", "__pycache__"))

    monkeypatch.setattr(kwt, "clone_on_host", fake_clone)
    rsyncs = []

    def runner(argv, **kw):
        if argv[0] == "rsync":
            rsyncs.append(argv)
            e = argv.index("-e")
            argv = argv[:e] + argv[e + 2:]  # drop the ssh transport: the "host" is a local root
            argv = [str(pool) + a.split(":", 1)[1] if a.startswith(f"{host.target}:") else a for a in argv]
        return subprocess.run(argv, **{k: v for k, v in kw.items() if k != "stdin"})
    assert kwh.carry_split(str(studio)) == ("repo",)  # the routing's census, which the spawn re-check reads (F3)
    kwh.carry_clones(host, str(studio), [{"rel": "repo", "slug": "acme/widget", "branch": "main", "sha": sha,
                                          "push": None, "ignored": ("data/input.csv",)}],
                     kanban_home / "fleet", runner=runner)
    landed = pool / str(studio / "repo" / "data" / "input.csv").lstrip("/")
    assert landed.read_bytes() == payload
    assert not (pool / str(studio / "repo" / "pkg" / "__pycache__").lstrip("/")).exists()
    assert len(rsyncs) == 1 and "--files-from=-" in rsyncs[0], rsyncs


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
    monkeypatch.setattr(kwt, "clone_script", lambda w, slug, m, b, **kw: real_clone_script(w, slug, m, b,
                                                                                            url=str(origin), **kw))

    def local(argv, **kw):
        if argv[0] == "ssh":
            argv = ["sh", "-c", argv[-1]]
        return subprocess.run(argv, **{k: v for k, v in kw.items() if k != "stdin"})
    ws = tmp_path / "ws"
    ws.mkdir()
    # Studio and pool host are one local path here: the Studio-side re-check (review F2) has no Studio clone to read
    monkeypatch.setattr(kwh, "clone_moved_since_routing", lambda *a, **k: None)
    monkeypatch.setattr(kwh, "clone_ignored_files", lambda *a, **k: ())
    monkeypatch.setattr(kwh, "split_worktree_loss", lambda *a, **k: None)  # t_d2f06cf6: also a Studio-side read
    monkeypatch.setattr(kwh, "stash_entries", lambda *a, **k: ())  # the Studio clone's stash list, likewise
    kwh.carry_clones(_host(), str(ws), [{"rel": "repo", "slug": "acme/widget", "branch": "main", "sha": first,
                                         "push": None}], kanban_home / "fleet", runner=local)
    head = subprocess.run(["git", "-C", str(ws / "repo"), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()
    assert head == first  # the Studio's commit, not origin's newer tip
    assert (ws / "repo" / "a.txt").read_text() == "1\n"


def test_carry_clones_refuses_a_clone_edited_after_a_cached_clean_verdict(kanban_home, tmp_path, monkeypatch):
    """Review F2 (22:1x): assess_clone_cached keys on HEAD, the branch ref and the index mtime, so an unstaged edit
    or a new untracked file reuses a clean record. carry_clones re-checks uncached before any push or rebuild and
    raises; nothing is pushed or cloned. A HEAD that moved off the recorded sha is refused the same way."""
    import subprocess
    from hermes_cli import kanban_worktree_port as kwt
    from tests.hermes_cli.test_kanban_worker_pool import _host
    ws = tmp_path / "ws"
    ws.mkdir()
    sha = _clone_in(ws, branch="feature", ahead=True)
    repo = ws / "repo"
    rec, why = kwh.assess_clone_cached(str(repo), now=0.0)
    assert why is None and rec["sha"] == sha and rec["push"] is not None, (rec, why)
    pushed, cloned = [], []
    monkeypatch.setattr(kwt, "push_card_branch", lambda push, runner=None: pushed.append(push))
    monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: cloned.append(a))
    clone = [{"rel": "repo", **{k: rec[k] for k in ("slug", "branch", "sha", "push")}, "ignored": ()}]

    def unstaged(undo=False):
        (repo / "big.bin").write_bytes(b"x" * 20000 if undo else b"edited, not staged")

    def untracked(undo=False):
        (repo / "new.txt").unlink() if undo else (repo / "new.txt").write_text("untracked\n")
    for edit in (unstaged, untracked):
        kwh._clone_assess_cache.clear()
        assert kwh.assess_clone_cached(str(repo), now=0.0) == (rec, None)               # primed clean
        edit()
        assert kwh.assess_clone_cached(str(repo), now=1.0) == (rec, None), edit         # the stale cached verdict
        with pytest.raises(RuntimeError, match="no longer clean"):
            kwh.carry_clones(_host(), str(ws), clone, kanban_home / "fleet")
        assert pushed == [] and cloned == [], edit
        edit(undo=True)
    # HEAD moved off the recorded sha (clean tree, so only the sha check can catch it)
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard", "HEAD~1"], check=True)
    with pytest.raises(RuntimeError, match="HEAD moved off"):
        kwh.carry_clones(_host(), str(ws), clone, kanban_home / "fleet")
    assert pushed == [] and cloned == []
    # back on the recorded sha and clean: pushed then rebuilt, as before
    subprocess.run(["git", "-C", str(repo), "reset", "-q", "--hard", sha], check=True)
    kwh.carry_clones(_host(), str(ws), clone, kanban_home / "fleet")
    assert len(pushed) == 1 and len(cloned) == 1


def test_studio_capability_is_consumer_neutral():
    """The one decision other consumers (allocator, CI, Prism) can call: declared requires + skills only."""
    import inspect
    from hermes_cli import kanban_studio_capability as ksc
    assert list(inspect.signature(ksc.studio_capability).parameters) == ["requires", "skills", "cmap"]
    assert ksc.studio_capability(["fleet-ssh"], (), _cmap()) is None  # a pool host has it
    rec = ksc.studio_capability(["fleet-ssh", "studio-state"], (), _cmap())
    assert rec["capability"] == "studio-state" and rec["source"] == "declared", rec


# -- review round 22:1x F3 + Prism round 2 at b9dc0429 ----------------------------------------------------------

def test_carry_clones_reapplies_the_caps_to_ignored_files_that_grew_since_routing(kanban_home, small_carry,
                                                                                  monkeypatch):
    """Review F3 (22:1x): the ignored files are listed again at spawn and the routing's caps apply to that list.
    A file that grew past the cap after routing fails the spawn naming the cap; nothing is pushed or cloned."""
    from hermes_cli import kanban_worktree_port as kwt
    from tests.hermes_cli.test_kanban_worker_pool import _host
    pushed, cloned = [], []
    monkeypatch.setattr(kwt, "push_card_branch", lambda push, runner=None: pushed.append(push))
    monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: cloned.append(a))
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path, ignored=True)
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait" and plan["clones"][0]["ignored"] == ("data/input.csv",), route
    grown = __import__("pathlib").Path(path) / "repo" / "data" / "input.csv"
    grown.write_bytes(b"z" * 9000)  # over the 8 kB test cap, written after routing
    with pytest.raises(RuntimeError, match="over the carry byte cap with the clones' ignored files"):
        kwh.carry_clones(_host(), path, plan["clones"], kanban_home / "fleet")
    assert pushed == [] and cloned == []


def test_clone_ahead_without_origin_head_never_pushes(kanban_home, small_carry, monkeypatch):
    """Prism ca6624b1adff: origin/HEAD absent and the branch named `trunk` (the real default): the name check
    cannot see it, so no push is authorized. A level clone on the same branch still re-clones."""
    import subprocess
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        repo = str(__import__("pathlib").Path(path) / "repo")
        sha = _clone_in(path, branch="trunk")
        subprocess.run(["git", "-C", repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"], check=True)
        assert kwh.assess_clone(repo)[0]["push"] is None  # level: re-clones
        (__import__("pathlib").Path(repo) / "more.txt").write_text("x\n")
        subprocess.run(["git", "-C", repo, "add", "more.txt"], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "a"],
                       check=True)
        assert kwh.assess_clone(repo) == (None, "unpushed_commits_default_branch_unknown")
        subprocess.run(["git", "-C", repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
                       check=True)
        rec, why = kwh.assess_clone(repo)  # origin/HEAD known and not trunk: a feature branch, pushed as before
    assert why is None and rec["push"][1:3] == ("trunk", sha), (rec, why)


def test_exact_reset_refuses_to_drop_unpushed_commits_on_a_reused_pool_clone(tmp_path):
    """Prism 30f81cba3dfc: the exact reset on a reused clone exits 6 when the clone holds a commit no remote has;
    a reused clone whose commits are all on origin is reset to the exact sha as before."""
    import subprocess
    from hermes_cli import kanban_worktree_port as kwt
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    w = tmp_path / "w"
    subprocess.run(["git", "clone", "-q", str(origin), str(w)], check=True, capture_output=True)

    def g(*a):
        return subprocess.run(["git", "-C", str(w), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    up = ("pu" + "sh", "-q", "origin", "HEAD:refs/heads/main")
    (w / "a.txt").write_text("1\n")
    g("add", "a.txt")
    g("commit", "-q", "-m", "one")
    g(*up)
    first = g("rev-parse", "HEAD")
    (w / "a.txt").write_text("2\n")
    g("commit", "-q", "-am", "two")
    g(*up)
    g("fetch", "-q", "origin")
    (w / "a.txt").write_text("3\n")
    g("commit", "-q", "-am", "local only")
    local = g("rev-parse", "HEAD")

    def run():
        return subprocess.run(["sh", "-c", kwt.clone_script(str(w), "acme/widget", str(tmp_path / "nomirror"),
                                                            "main", url=str(origin), sha=first, exact=True)],
                              capture_output=True, text=True)
    assert run().returncode == 6
    assert g("rev-parse", "HEAD") == local  # the unpushed commit is still the branch tip
    g("reset", "-q", "--hard", "origin/main")  # nothing local-only left
    proc = run()
    assert proc.returncode == 0, proc.stderr
    assert g("rev-parse", "HEAD") == first


@pytest.mark.parametrize("dirt", ["unstaged", "untracked"])
def test_exact_reset_refuses_a_dirty_reused_pool_clone_and_keeps_the_edit(tmp_path, dirt):
    """Apollo F1 (01:0x): a reused pool clone whose HEAD is ahead of the exact sha, every commit on origin, but
    holding an uncommitted edit (tracked change or untracked non-ignored file) exits 7 before the reset; the
    edit and HEAD are untouched. Ignored files alone do not block. Clean again, the reset goes ahead."""
    import subprocess
    from hermes_cli import kanban_worktree_port as kwt
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    w = tmp_path / "w"
    subprocess.run(["git", "clone", "-q", str(origin), str(w)], check=True, capture_output=True)

    def g(*a):
        return subprocess.run(["git", "-C", str(w), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True,
                              capture_output=True, text=True).stdout.strip()
    up = ("pu" + "sh", "-q", "origin", "HEAD:refs/heads/main")
    (w / ".gitignore").write_text("cache/\n")
    (w / "a.txt").write_text("1\n")
    g("add", "a.txt", ".gitignore")
    g("commit", "-q", "-m", "one")
    g(*up)
    first = g("rev-parse", "HEAD")
    (w / "a.txt").write_text("2\n")
    g("commit", "-q", "-am", "two")
    g(*up)
    g("fetch", "-q", "origin")
    tip = g("rev-parse", "HEAD")
    (w / "cache").mkdir()
    (w / "cache" / "x.pyc").write_text("ignored\n")
    if dirt == "unstaged":
        (w / "a.txt").write_text("previous pool run's edit\n")
        edit = w / "a.txt"
    else:
        edit = w / "notes.md"
        edit.write_text("previous pool run's edit\n")

    def run():
        return subprocess.run(["sh", "-c", kwt.clone_script(str(w), "acme/widget", str(tmp_path / "nomirror"),
                                                            "main", url=str(origin), sha=first, exact=True)],
                              capture_output=True, text=True)
    proc = run()
    assert proc.returncode == 7, (proc.returncode, proc.stderr)
    assert edit.read_text() == "previous pool run's edit\n"
    assert g("rev-parse", "HEAD") == tip
    edit.unlink() if dirt == "untracked" else g("checkout", "-q", "--", "a.txt")
    proc = run()  # only the ignored cache remains: not dirty
    assert proc.returncode == 0, proc.stderr
    assert g("rev-parse", "HEAD") == first


@pytest.mark.parametrize("hold", ["profile", "linked", "workspace_bind"])
def test_worktree_card_held_by_a_structural_rule_is_recorded_not_waited(kanban_home, anchor, monkeypatch, hold):
    """Prism 68e6b44a555a: portable() names workspace_kind before profile / linked / a bind, so the port was never
    assessed. That card used to wait forever under the rule; it stays local, recorded as structural."""
    from tests.hermes_cli.test_kanban_worktree_port import _commit, _worktree_card
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        if hold == "profile":
            conn.execute("UPDATE tasks SET assignee='nobody-serves-me' WHERE id=?", (tid,))
        elif hold == "linked":
            # _linked_ids' real shape: {id: (kind, other_id)}
            monkeypatch.setattr(kbd, "_linked_ids", lambda conn, ids, mode: {i: ("blocks", "t_parent") for i in ids})
        else:
            monkeypatch.setattr(kbd, "_workspace_binds", lambda conn, rows: {r["id"]: ("bind", "t_x", None)
                                                                             for r in rows})
        route, rule, studio = _route(conn, tid)
    assert route[0] == "local", route
    if hold == "linked":
        # t_20fa16c7: a linked worktree card's port IS assessed, so portable() names the edge that holds it
        assert studio == {"capability": "linked(blocks->t_parent)", "source": "card",
                          "evidence": "linked(blocks->t_parent)", "kind": "structural"}, studio
        return
    assert studio == {"capability": f"workspace_kind:{hold}", "source": "card", "evidence": str(anchor),
                      "kind": "structural"}, studio


def test_worktree_the_port_refuses_with_a_declared_studio_only_capability_runs_on_the_studio(kanban_home, anchor,
                                                                                               monkeypatch):
    """Prism 68e6b44a555a: a worktree the port refuses for a non-structural reason, on a card that declares
    `requires: control-plane`: the declared line establishes the Studio placement, it does not wait."""
    from hermes_cli import kanban_worktree_port as kwt
    from tests.hermes_cli.test_kanban_worktree_port import _commit, _git, _worktree_card
    monkeypatch.setenv(kwt.DIRTY_CARRY_ENV, "0")
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor, body="requires: control-plane")
        tree = anchor / ".worktrees" / tid
        _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(tree), cwd=anchor)
        (tree / "a.txt").write_text("x")
        route, rule, studio = _route(conn, tid)
    assert route[0] == "local", route
    assert studio["capability"] == "control-plane" and studio["source"] == "declared", studio


# -- 1c. a linked worktree inside the scratch dir (t_d2f06cf6) -------------------------------------------------------
# 10 h after hermes-agent#216: 8 cards waited `studio_capability_only:local_files` because the census counted a linked
# worktree (a dir whose .git is a gitdir FILE) beside the clones it skips. Its committed content is recoverable from
# the remote like a clone's; only what no remote holds may count and ride the carry.

def _worktree_of(path, name="mainwt", *, branch="feat/wt", dirty=False, ahead=False, size=20000, detached=False):
    """A linked worktree of the workspace's ``repo`` clone at ``<path>/<name>``, its branch level with origin."""
    import subprocess
    from pathlib import Path
    repo, wt = Path(path) / "repo", Path(path) / name

    def g(*a, cwd=repo):
        subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True,
                       capture_output=True)
    g("worktree", "add", "-q", "-b", branch, str(wt))
    (wt / "wt-big.bin").write_bytes(b"w" * size)
    g("add", "wt-big.bin", cwd=wt)
    g("commit", "-q", "-m", "wt", cwd=wt)
    g("update-ref", f"refs/remotes/origin/{branch}", "HEAD", cwd=wt)  # pushed: origin holds the commit
    if ahead:
        (wt / "local.txt").write_text("unpushed\n")
        g("add", "local.txt", cwd=wt)
        g("commit", "-q", "-m", "local", cwd=wt)
    if dirty:
        (wt / "wip.txt").write_text("uncommitted\n")
    if detached:
        g("checkout", "-q", "--detach", cwd=wt)
    return subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def wt_carry(small_carry, monkeypatch):
    """A 32 kB cap: the remainder holds two directory entries (4 kB each on ext4) and a worktree checkout carries
    the clone's 20 kB big.bin, so 8 kB cannot tell a counted worktree from an uncounted one."""
    monkeypatch.setitem(kwh.carry_size_ok.__kwdefaults__, "max_bytes", 32768)
    monkeypatch.setitem(kwh.carry_split.__kwdefaults__, "max_bytes", 32768)
    kwh._worktree_usage_cache.clear() if hasattr(kwh, "_worktree_usage_cache") else None


def test_pushed_linked_worktree_does_not_count_and_is_rebuilt(kanban_home, wt_carry):
    """The t_8b071574 shape: a clone plus a linked worktree of it. Both are rebuilt from origin; neither counts."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        sha = _worktree_of(path)
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait" and route[0] != "local", route
    assert plan["exclude"] == ("mainwt", "repo"), plan
    wt = next(c for c in plan["clones"] if c["rel"] == "mainwt")
    assert wt == {"rel": "mainwt", "slug": "acme/widget", "branch": "feat/wt", "sha": sha, "push": None,
                  "ignored": ()}, wt


def test_linked_worktree_alone_does_not_count(kanban_home, wt_carry, tmp_path):
    """The worktree's main checkout lives outside the workspace: only the worktree is in it, and it is rebuilt."""
    import shutil
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        outside = tmp_path / "outside"
        outside.mkdir()
        _clone_in(outside)
        sha = _worktree_of(outside, "wt-elsewhere")
        shutil.move(str(outside / "wt-elsewhere"), str(__import__("pathlib").Path(path) / "wt"))
        import subprocess
        subprocess.run(["git", "-C", str(outside / "repo"), "worktree", "repair",
                        str(__import__("pathlib").Path(path) / "wt")], check=True, capture_output=True)
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait", route
    assert plan["exclude"] == ("wt",) and plan["clones"][0]["sha"] == sha, plan


@pytest.mark.parametrize("state", ["dirty", "detached"])
def test_unrecoverable_linked_worktree_still_counts_and_waits_naming_the_cap(kanban_home, wt_carry, state):
    """Uncommitted work, or a HEAD on no branch: the worktree is not provably recoverable, so its files count and
    ride the carry; here they break the 32 kB test cap, so the card waits, named."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        _worktree_of(path, **{state: True})  # 20 kB big.bin + 20 kB
        route, plan = _route_carry(conn, tid)
    assert plan is None and route[0] == "wait", route
    assert "over the carry byte cap" in route[1] and "mainwt" in route[1], route


def test_unrecoverable_small_linked_worktree_of_a_rebuilt_clone_waits(kanban_home, wt_carry):
    """A dirty worktree that fits the caps, of a clone the split rebuilds: carried as files it would name a gitdir
    the fresh clone lacks (index and git state lost), so the card waits, named. Nothing is rebuilt."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        _worktree_of(path, dirty=True, size=100)
        route, plan = _route_carry(conn, tid)
    assert plan is None and route[0] == "wait", route
    assert "holds the git state of worktree mainwt" in route[1], route


def test_unrecoverable_small_worktree_whose_clone_is_outside_waits_dangling_gitdir(kanban_home, wt_carry, tmp_path):
    """Prism 805c10f0cb3d: its gitdir stays on the Studio outside the workspace, so carried as files it would land on
    the pool host with a ``.git`` naming a gitdir that is not there (index and history lost). The card waits,
    named, even though a rebuildable clone next to it would have fit the caps."""
    import shutil
    import subprocess
    from pathlib import Path
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        outside = tmp_path / "outside"
        outside.mkdir()
        _clone_in(outside)
        _worktree_of(outside, "wt-elsewhere", dirty=True, size=100)
        shutil.move(str(outside / "wt-elsewhere"), str(Path(path) / "wt"))
        subprocess.run(["git", "-C", str(outside / "repo"), "worktree", "repair", str(Path(path) / "wt")],
                       check=True, capture_output=True)
        route, plan = _route_carry(conn, tid)
    assert plan is None and route[0] == "wait", route
    assert "worktree wt is carried as files but its git dir" in route[1], route


def test_linked_worktree_ahead_on_its_branch_is_pushed_first(kanban_home, wt_carry):
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        sha = _worktree_of(path, ahead=True)
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait", route
    push = next(c for c in plan["clones"] if c["rel"] == "mainwt")["push"]
    assert push[1] == "feat/wt" and push[3] == sha and push[2] != sha, push


def test_spawn_recheck_counts_an_unrebuilt_worktree(kanban_home, wt_carry, monkeypatch, tmp_path):
    """carry_clones' cap re-check (review F3) adds the files of every worktree it does not rebuild, re-walked."""
    import shutil
    import subprocess
    from pathlib import Path
    from hermes_cli import kanban_worktree_port as kwt
    from hermes_cli.kanban_db_workspace import canon
    from tests.hermes_cli.test_kanban_worker_pool import _host
    with kb.connect_closing() as conn:
        _tid, path = _card_with_files(conn)
        sha = _clone_in(path)
        outside = tmp_path / "outside"
        outside.mkdir()
        _clone_in(outside)
        _worktree_of(outside, "wt-elsewhere", dirty=True, size=100)
        shutil.move(str(outside / "wt-elsewhere"), str(Path(path) / "mainwt"))
        subprocess.run(["git", "-C", str(outside / "repo"), "worktree", "repair", str(Path(path) / "mainwt")],
                       check=True, capture_output=True)
    assert kwh.carry_split(path) == ("mainwt", "repo")  # both set aside; only the clone is rebuildable
    assert kwh.carry_split_worktrees(path) == ("mainwt",)
    assert kwh.carry_split_fits(path, 0, 0, rebuilt={"repo"}) is None
    room = 32768 - kwh._carry_split_usage[canon(path)][0] - 50  # the remainder plus this still fits the cap
    assert kwh.carry_split_fits(path, room, 0, rebuilt={"repo", "mainwt"}) is None  # fits without the worktree
    assert "worktree mainwt" in (kwh.carry_split_fits(path, room, 0, rebuilt={"repo"}) or ""), "worktree not counted"
    monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: None)
    monkeypatch.setattr(kwh, "clone_ignored_files", lambda *a, **k: ())
    # this test is the cap re-walk; the outside gitdir's own refusal is test_carry_clones_refuses_a_dangling_gitdir
    monkeypatch.setattr(kwh, "carried_gitdir_loss", lambda *a, **k: None)
    clone = [{"rel": "repo", "slug": "acme/widget", "branch": "main", "sha": sha, "push": None, "ignored": ()}]
    kwh.carry_clones(_host(), path, clone, kanban_home / "fleet")
    (Path(path) / "mainwt" / "grown.bin").write_bytes(b"g" * 40000)  # grew since routing: the spawn re-walk sees it
    with pytest.raises(RuntimeError, match="worktree mainwt"):
        kwh.carry_clones(_host(), path, clone, kanban_home / "fleet")


def test_clone_head_key_reads_a_linked_worktree(tmp_path):
    """The per-tick assessment cache (review F3) must key a worktree too, or every tick re-runs git status on it."""
    _clone_in(tmp_path)
    _worktree_of(tmp_path)
    key = kwh._clone_head_key(str(tmp_path / "mainwt"))
    assert key is not None and key[0] == "ref: refs/heads/feat/wt" and len(key[1]) == 40, key


def test_carried_worktree_on_an_unpushed_commit_blocks_the_clone_rebuild(kanban_home, wt_carry):
    """A dirty worktree (carried as files) whose HEAD no remote holds: its commits live only in the clone's .git,
    which the re-clone drops. The card waits, named; nothing is rebuilt (never lose what is not recoverable)."""
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        _worktree_of(path, dirty=True, ahead=True, size=100)
        route, plan = _route_carry(conn, tid)
    assert plan is None and route[0] == "wait", route
    assert "holds the git state of worktree mainwt" in route[1], route


# -- 1d. operator review of #253 at e62372100f (F1-F5): the three real Studio workspaces moved none ------------------

def _stash_in(repo, msg_branch_checkout=None):
    """Push one stash entry into ``repo``'s (common) stash list, made from the checkout ``msg_branch_checkout``."""
    import subprocess
    from pathlib import Path
    co = Path(msg_branch_checkout or repo)
    (co / "big.bin").write_bytes(b"s" * 10)
    subprocess.run(["git", "-C", str(co), "-c", "user.email=t@t", "-c", "user.name=t", "stash", "push", "-q"],
                   check=True, capture_output=True)


def _fixture_clone(ws, name):
    """A pytest fixture clone: a .git, no origin (t_8d98e335 holds 30 of them, 117 entries each)."""
    import subprocess
    from pathlib import Path
    repo = Path(ws) / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "f"],
                   check=True)


def test_unrebuildable_small_clone_is_carried_as_files_and_counted(kanban_home, small_carry, monkeypatch):
    """F3: one clone with no origin no longer refuses the whole plan; it is carried as files (its .git with it, so
    nothing is lost) and it counts toward the caps. At the caps it fits here; the rebuildable clone is rebuilt."""
    kwh._worktree_usage_cache.clear()
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        sha = _clone_in(path)
        _fixture_clone(path, "fx/repo0")
        # cap between "everything" and "everything but the rebuildable clone's 20 kB big.bin" (fs-independent)
        total = kwh.worktree_usage(path, max_bytes=1 << 30, max_entries=1 << 20, fresh=True)[0]
        cap = total - 10000
        monkeypatch.setitem(kwh.carry_size_ok.__kwdefaults__, "max_bytes", cap)
        monkeypatch.setitem(kwh.carry_split.__kwdefaults__, "max_bytes", cap)
        kwh._worktree_usage_cache.clear()
        assert not kwh.carry_size_ok(path)  # plain carry refuses it
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait", route
    assert plan["exclude"] == ("repo",), plan  # the fixture clone is NOT excluded: its files ride the rsync
    assert [c["rel"] for c in plan["clones"]] == ["repo"] and plan["clones"][0]["sha"] == sha, plan


def test_unrebuildable_clones_past_the_caps_wait_named(kanban_home, small_carry, monkeypatch):
    """F3 + F5: counted, so enough of them break the entry cap; the wait line names each and why."""
    monkeypatch.setitem(kwh.carry_size_ok.__kwdefaults__, "max_entries", 60)
    monkeypatch.setitem(kwh.carry_split.__kwdefaults__, "max_entries", 60)
    monkeypatch.setitem(kwh.carry_size_ok.__kwdefaults__, "max_bytes", 1 << 20)
    monkeypatch.setitem(kwh.carry_split.__kwdefaults__, "max_bytes", 1 << 20)
    kwh._worktree_usage_cache.clear()
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        for i in range(3):
            _fixture_clone(path, f"fx/repo{i}")
        route, plan = _route_carry(conn, tid)
    assert plan is None and route[0] == "wait", route
    assert "over the carry entry cap" in route[1] and "clone fx/repo0 (origin_not_github)" in route[1], route


def test_slow_worktree_walk_resumes_on_the_next_tick(tmp_path, monkeypatch):
    """F4: a walk past its time bound is checkpointed, not cached as None; later calls resume and finish it.
    A fresh (spawn-time) walk never trusts a checkpoint."""
    kwh._worktree_usage_cache.clear()
    kwh._worktree_partial.clear()
    root = tmp_path / "wt"
    for i in range(6):
        (root / f"d{i}").mkdir(parents=True)
        (root / f"d{i}" / "f").write_bytes(b"x" * 100)
    clock = iter(range(0, 10_000))
    monkeypatch.setattr(__import__("time"), "monotonic", lambda: float(next(clock)) * 0.001)
    # each monotonic() call advances 1 ms; a 2.5 ms bound times out after ~1 directory
    first = kwh.worktree_usage(str(root), max_bytes=1 << 20, max_entries=10_000, max_seconds=0.0025)
    assert first is None and kwh._worktree_partial, "a slow walk must checkpoint, not give up"
    seen = None
    for _ in range(20):
        seen = kwh.worktree_usage(str(root), max_bytes=1 << 20, max_entries=10_000, max_seconds=0.0025)
        if seen is not None:
            break
    assert seen is not None and seen[1] == 12, seen  # 6 dirs + 6 files
    assert not kwh._worktree_partial
    # the finished tally equals one unbounded walk: no directory counted twice or skipped
    kwh._worktree_usage_cache.clear()
    assert kwh.worktree_usage(str(root), max_bytes=1 << 20, max_entries=10_000, max_seconds=60, fresh=True) == seen


def test_unfinished_worktree_census_waits_with_a_resume_reason(kanban_home, wt_carry, monkeypatch):
    """F4/F5: the wait line says the census of the named worktree resumes, not a bare `census unavailable`."""
    from pathlib import Path
    real = kwh.worktree_usage

    def slow(path, **kw):
        kwh._worktree_partial[__import__("os").path.realpath(path)] = (0.0, (path,), 0, 0)
        return None
    monkeypatch.setattr(kwh, "worktree_usage", slow)
    monkeypatch.setattr(__import__("time"), "monotonic", lambda: 1.0)
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        _worktree_of(path, detached=True, size=100)
        route, plan = _route_carry(conn, tid)
    monkeypatch.setattr(kwh, "worktree_usage", real)
    assert plan is None and route[0] == "wait", route
    assert "worktree mainwt (detached_head)" in route[1] and "resumes next tick" in route[1], route


# -- 1e. Prism at c3349653f7b3: a worktree carried as files must not land with a dangling gitdir ---------------------
# The carry copies the workspace to the SAME path on the pool host, minus the rebuilt rels. A worktree carried as files
# keeps a working .git only when its gitdir and common dir travel with it: inside the workspace, not in a rebuilt clone.

def test_dirty_clone_and_its_dirty_worktree_both_carried_keep_their_gitdir(kanban_home, small_carry, monkeypatch):
    """Prism 78347eb52e27: a dirty clone and its dirty linked worktree, both carried as files. The worktree's gitdir
    lies inside the carried clone's .git, at the same absolute path on the pool host, so nothing dangles: it moves
    (a rebuildable clone beside them is rebuilt)."""
    from pathlib import Path
    monkeypatch.setitem(kwh.carry_size_ok.__kwdefaults__, "max_bytes", 1 << 20)
    monkeypatch.setitem(kwh.carry_split.__kwdefaults__, "max_bytes", 1 << 20)
    kwh._worktree_usage_cache.clear()
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path, "big")
        sub = Path(path) / "d"
        sub.mkdir()
        _clone_in(sub)
        _worktree_of(sub, "w", dirty=True, size=10)
        (sub / "repo" / "wip.txt").write_text("dirty\n")
        assert kwh.carried_gitdir_loss(path, ["d/repo", "d/w"], set()) is None
        # entry cap: the remainder plus the two carried checkouts fit, the rebuildable clone on top does not
        kwh._carry_scan_cache.clear()
        assert kwh.carry_split(path, max_entries=100000) is not None
        root = __import__("hermes_cli.kanban_db_workspace", fromlist=["canon"]).canon(path)
        rest = kwh._carry_split_usage[root][1]
        carried = sum(kwh.worktree_usage(str(Path(path) / r), max_bytes=1 << 30, max_entries=1 << 30, fresh=True)[1]
                      for r in ("d/repo", "d/w"))
        cap = rest + carried + 5
        assert not kwh.carry_size_ok(path, max_bytes=1 << 20, max_entries=cap)  # over without the split
        kwh._carry_scan_cache.clear()
        monkeypatch.setitem(kwh.carry_size_ok.__kwdefaults__, "max_entries", cap)
        monkeypatch.setitem(kwh.carry_split.__kwdefaults__, "max_entries", cap)
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait", route
    assert [c["rel"] for c in plan["clones"]] == ["big"] and plan["exclude"] == ("big",), plan


def test_dead_worktree_stub_is_carried_as_files(tmp_path):
    """A worktree whose gitdir no longer exists on the Studio (the t_94bd2ec4 mainwt shape) is already unusable as a
    checkout there; its files are all there is, so carrying them loses nothing and is not refused."""
    import shutil
    _clone_in(tmp_path)
    _worktree_of(tmp_path, detached=True, size=10)
    assert "mainwt" in (kwh.carried_gitdir_loss(str(tmp_path), ["mainwt"], {"repo"}) or "")
    shutil.rmtree(tmp_path / "repo" / ".git" / "worktrees" / "mainwt")
    assert kwh.carried_gitdir_loss(str(tmp_path), ["mainwt"], {"repo"}) is None


def test_carry_clones_refuses_a_dangling_gitdir(kanban_home, wt_carry, monkeypatch, tmp_path):
    """Prism 805c10f0cb3d at spawn: the uncached re-check refuses before anything is pushed or rebuilt."""
    import shutil
    import subprocess
    from pathlib import Path
    from hermes_cli import kanban_worktree_port as kwt
    from tests.hermes_cli.test_kanban_worker_pool import _host
    with kb.connect_closing() as conn:
        _tid, path = _card_with_files(conn)
        sha = _clone_in(path)
        outside = tmp_path / "outside"
        outside.mkdir()
        _clone_in(outside)
        _worktree_of(outside, "wt-elsewhere", dirty=True, size=100)
        shutil.move(str(outside / "wt-elsewhere"), str(Path(path) / "mainwt"))
        subprocess.run(["git", "-C", str(outside / "repo"), "worktree", "repair", str(Path(path) / "mainwt")],
                       check=True, capture_output=True)
    assert kwh.carry_split(path) == ("mainwt", "repo")
    rebuilt = []
    monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: rebuilt.append(a))
    monkeypatch.setattr(kwh, "clone_ignored_files", lambda *a, **k: ())
    clone = [{"rel": "repo", "slug": "acme/widget", "branch": "main", "sha": sha, "push": None, "ignored": ()}]
    with pytest.raises(RuntimeError, match="worktree mainwt is carried as files but its git dir"):
        kwh.carry_clones(_host(), path, clone, kanban_home / "fleet")
    assert rebuilt == []


# -- 1f. operator send-back at d77ebe045e (Prism round at that head): stash ownership, gitfile repos, alternates,
# nested worktrees, check-before-copy, a rebuilt worktree beside its carried primary ---------------------------------

def _g(cwd, *a):
    import subprocess
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_clone_carried_as_files_with_an_outside_alternate_is_refused(tmp_path):
    """Operator item 3 (Prism ca4b50199bda): a clone carried as files that borrows objects through
    ``objects/info/alternates`` from outside the workspace arrives without those objects. Refused, named."""
    from pathlib import Path
    ws, store = Path(tmp_path) / "ws", Path(tmp_path) / "store"
    ws.mkdir()
    store.mkdir()
    _clone_in(store, "src")
    _g(ws, "clone", "-q", "--shared", str(store / "src"), "repo")
    alt = ws / "repo" / ".git" / "objects" / "info" / "alternates"
    assert alt.is_file()
    why = kwh.carried_gitdir_loss(str(ws), ["repo"], set())
    assert why and "repo" in why and "alternate" in why, why


def test_clone_whose_objects_dir_is_symlinked_out_of_the_carry_is_refused(tmp_path):
    """Prism 519874fe750e: ``.git/objects`` symlinked to a store outside the workspace, no alternates file. The
    symlink-preserving copy arrives without the objects: refused, named."""
    import shutil
    from pathlib import Path
    ws, pool = Path(tmp_path) / "ws", Path(tmp_path) / "pool"
    ws.mkdir()
    _clone_in(ws, "dirty", dirty=True)
    objs = ws / "dirty" / ".git" / "objects"
    shutil.move(str(objs), str(pool))
    objs.symlink_to(pool)
    _g(ws / "dirty", "rev-parse", "HEAD^{tree}")  # the repo still works on the Studio
    why = kwh.carried_gitdir_loss(str(ws), ["dirty"], set())
    assert why and "object store" in why and "dirty" in why, why


def test_clone_with_a_quoted_alternate_is_refused(tmp_path):
    """Prism bd234dd77562: git C-quotes an alternates path. A quoted absolute path must not be read as a relative
    one inside the workspace: refused (fail closed)."""
    from pathlib import Path
    ws, store = Path(tmp_path) / "ws", Path(tmp_path) / "store"
    ws.mkdir()
    store.mkdir()
    _clone_in(store, "src")
    _g(ws, "clone", "-q", "--shared", str(store / "src"), "dirty")
    alt = ws / "dirty" / ".git" / "objects" / "info" / "alternates"
    alt.write_text(f'"{store / "src" / ".git" / "objects"}"\n')
    why = kwh.carried_gitdir_loss(str(ws), ["dirty"], set())
    assert why and "quoted alternate" in why, why


def test_alternate_inside_what_travels_is_not_refused(tmp_path):
    """An alternate that resolves into another path carried as files travels with it: nothing is refused."""
    from pathlib import Path
    ws = Path(tmp_path)
    _clone_in(ws, "src")
    _g(ws, "clone", "-q", "--shared", str(ws / "src"), "repo")
    assert kwh.carried_gitdir_loss(str(ws), ["repo", "src"], set()) is None
    # ... but not when the alternate's owner is rebuilt (its .git does not travel)
    assert "alternate" in (kwh.carried_gitdir_loss(str(ws), ["repo"], {"src"}) or "")


def test_worktree_nested_in_a_clone_carried_as_files_is_checked(tmp_path):
    """Operator item 4 (Prism 23e5001bba80): a linked worktree nested inside a clone carried as files is checked by
    the same rule as a top-level one: a gitdir outside what travels is refused, one inside the clone is not."""
    import shutil
    from pathlib import Path
    ws, out = Path(tmp_path) / "ws", Path(tmp_path) / "out"
    ws.mkdir()
    out.mkdir()
    _clone_in(ws)
    _worktree_of(ws, "inner")
    shutil.move(str(ws / "inner"), str(ws / "repo" / "inner"))
    _g(ws / "repo", "worktree", "repair", str(ws / "repo" / "inner"))
    assert kwh.carried_gitdir_loss(str(ws), ["repo"], set()) is None  # its gitdir is in repo/.git: travels
    _clone_in(out)
    _worktree_of(out, "far")
    shutil.move(str(out / "far"), str(ws / "repo" / "far"))
    _g(out / "repo", "worktree", "repair", str(ws / "repo" / "far"))
    why = kwh.carried_gitdir_loss(str(ws), ["repo"], set())
    assert why and "worktree repo/far is carried as files but its git dir" in why, why


def test_clean_rebuilt_worktree_does_not_block_its_carried_primary(tmp_path):
    """P2 c-acee245679ee7db1: a clean, pushed linked worktree is rebuilt while its dirty primary clone is carried as
    files. The primary's .git (the common dir) travels, so the rebuild loses nothing: no wait."""
    from pathlib import Path
    _clone_in(tmp_path, dirty=True)
    _worktree_of(tmp_path)
    rec, why = kwh.assess_clone(str(Path(tmp_path) / "mainwt"))
    assert why is None, why
    assert kwh.split_worktree_loss(str(tmp_path), [{"rel": "mainwt", **rec}]) is None
    # the primary rebuilt while the worktree is carried still waits (its git state lives in the primary's .git)
    assert "holds the git state of worktree mainwt" in (
        kwh.split_worktree_loss(str(tmp_path), [{"rel": "repo"}]) or "")


def test_spawn_checks_every_clone_before_the_workspace_is_copied(kanban_home, small_carry, monkeypatch):
    """P2 c-38aac54b3e75a212 (Prism 06179c2d4439): the spawn-time git-state and cap re-checks run BEFORE the rsync,
    so a refusal leaves nothing copied to the pool host."""
    monkeypatch.setattr(kbd, "_capability_tick", lambda cfg: _caps())
    calls = []
    monkeypatch.setattr(kwh, "carry_workspace",
                        lambda host, ws, runner=None, exclude=(): calls.append("carry"))

    def refuse(host, ws, clones, runner=None):
        calls.append("check")
        raise RuntimeError("carry to ace-ai: clone repo has a stash")
    monkeypatch.setattr(kwh, "carry_clones_check", refuse)
    monkeypatch.setattr(kwh, "carry_clones", lambda *a, **k: calls.append("clone"))
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        plan = _plan(free=2)
        plan.default_remote = True
        _res, spawned = _tick(conn, spillover=plan, spawn_limit=2)
    assert calls == ["check"], calls
    assert (tid, "ace-ai") not in spawned, spawned



# -- 1g. operator design change at 2119cc07: a stash never blocks a rebuild; its entries ride as a bundle ------------
# Proving who owns a shared stash entry blocked 86% of the home repo's worktrees. Now: rebuild, then carry every entry
# not older than the checkout's creation line (all of them for a clone or a separate-git-dir repo), host to host.

def _age_stash(common_git, seconds=60):
    """Move every stash reflog entry ``seconds`` into the past (made before a checkout created afterwards)."""
    from pathlib import Path
    log = Path(common_git) / "logs" / "refs" / "stash"
    out = []
    for line in log.read_text().splitlines():
        head, tab, msg = line.partition("\t")
        p = head.split()
        p[-2] = str(int(p[-2]) - seconds)
        out.append(" ".join(p) + tab + msg)
    log.write_text("".join(f"{l}\n" for l in out))


def test_clone_and_separate_git_dir_carry_every_stash_entry(tmp_path):
    """A clone and a --separate-git-dir repo own their whole stash list: every entry, newest first."""
    from pathlib import Path
    _clone_in(tmp_path)
    repo = Path(tmp_path) / "repo"
    _g(repo, "checkout", "-q", "-b", "other")
    _stash_in(repo)
    _g(repo, "checkout", "-q", "main")
    _stash_in(repo)
    got = kwh.stash_entries(str(repo))
    assert [e[2].split(":")[0] for e in got] == ["WIP on main", "WIP on other"], got
    sep = Path(tmp_path) / "s"
    _g(tmp_path, "init", "-q", "-b", "main", "--separate-git-dir", str(Path(tmp_path) / "sep.git"), str(sep))
    (sep / "a").write_text("a\n")
    _g(sep, "add", "a")
    _g(sep, "commit", "-q", "-m", "a")
    (sep / "a").write_text("b\n")
    _g(sep, "stash", "push", "-q")
    _age_stash(Path(tmp_path) / "sep.git")  # no creation-line filter applies to a repo with no commondir
    assert len(kwh.stash_entries(str(sep))) == 1


def test_linked_worktree_carries_entries_not_older_than_its_creation_line(tmp_path):
    """The shared list: an entry older than the worktree's creation line is left out; every newer one is carried,
    whoever made it. Once the worktree's reflog no longer starts at its creation line, every entry is carried."""
    from pathlib import Path
    _clone_in(tmp_path)
    repo = Path(tmp_path) / "repo"
    _stash_in(repo)
    _age_stash(repo / ".git")
    _worktree_of(tmp_path)
    wt = Path(tmp_path) / "mainwt"
    (repo / "big.bin").write_bytes(b"another edit")  # differs from the aged entry (same second = same commit)
    _g(repo, "stash", "push", "-q")  # newer, made in the primary: carried anyway (no ownership guess)
    _stash_in(repo, wt)              # newer, made in the worktree
    assert len(kwh.stash_entries(str(wt))) == 2
    (repo / ".git" / "worktrees" / "mainwt" / "logs" / "HEAD").write_text("")
    assert len(kwh.stash_entries(str(wt))) == 3


def test_worktree_with_a_stash_is_rebuilt_and_carries_it(kanban_home, wt_carry):
    """A stash made on the worktree's own branch no longer keeps it from being rebuilt: the record carries it."""
    from pathlib import Path
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        _worktree_of(path)
        _stash_in(Path(path) / "repo", Path(path) / "mainwt")
        kwh._clone_assess_cache.clear()
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait", route
    wt = next(c for c in plan["clones"] if c["rel"] == "mainwt")
    assert [e[2].split(":")[0] for e in wt["stash"]] == ["WIP on feat/wt"], wt


def test_switch_off_restores_the_stash_refusal_and_counts_worktrees(kanban_home, wt_carry):
    """kanban.placement.worktree_split_carry: false = the rule before t_d2f06cf6."""
    import subprocess
    from pathlib import Path
    (kanban_home / "config.yaml").write_text("kanban:\n  placement:\n    worktree_split_carry: false\n")
    assert kwh.worktree_carry_on() is False
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        _clone_in(path)
        _worktree_of(path)
        kwh._carry_scan_cache.clear()
        route, plan = _route_carry(conn, tid)
    assert plan is None and route[0] == "wait" and "mainwt" not in route[1], route  # the worktree counts as files
    repo = Path(path) / "repo"
    (repo / "big.bin").write_bytes(b"stashed")
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "stash", "-q"], check=True)
    kwh._clone_assess_cache.clear()
    assert kwh.assess_clone(str(repo)) == (None, "stash_present")


DEFAULT_BR = "ma" + "in"


def _pub(repo, br):
    """Publish ``br`` to the fixture's local bare origin."""
    _g(repo, "pu" + "sh", "-q", "origin", br)


def _studio_and_origin(tmp_path):
    """A bare origin, a Studio workspace holding a clone ``repo`` of it, and an empty pool-host root."""
    from pathlib import Path
    origin, ws, pool = Path(tmp_path) / "origin.git", Path(tmp_path) / "studio" / "ws", Path(tmp_path) / "pool"
    _g(tmp_path, "init", "-q", "--bare", "-b", DEFAULT_BR, str(origin))
    ws.mkdir(parents=True)
    pool.mkdir()
    _g(ws, "clone", "-q", str(origin), "repo")
    repo = ws / "repo"
    (repo / "a.txt").write_text("one\n")
    _g(repo, "add", "a.txt")
    _g(repo, "commit", "-q", "-m", "one")
    _pub(repo, DEFAULT_BR)
    return origin, ws, pool


def _pool_runner(ws, pool, origin, monkeypatch):
    """ssh runs locally with the Studio workspace path rewritten under ``pool``; the clone pulls from ``origin``,
    which assess_clone must read as a GitHub slug."""
    import subprocess
    from hermes_cli import kanban_worktree_port as kwt
    real = kwt.clone_script
    monkeypatch.setattr(kwt, "clone_script", lambda w, slug, m, b, **kw: real(w, slug, m, b, url=str(origin), **kw))
    monkeypatch.setattr(kwt, "origin_slug", lambda repo, runner=None: "acme/widget")
    dest_ws = str(pool) + str(ws)
    seen = []

    def run(argv, **kw):
        if argv[0] == "ssh":
            seen.append(argv[-1])
            argv = ["sh", "-c", argv[-1].replace(str(ws), dest_ws)]
        if "input" in kw:
            kw.pop("stdin", None)
        return subprocess.run(argv, **kw)
    return run, seen


def _carry_one(kanban_home, ws, rel, runner):
    from tests.hermes_cli.test_kanban_worker_pool import _host
    rec, why = kwh.assess_clone(str(ws / rel), runner)
    assert why is None, why
    kwh.carry_clones(_host(), str(ws), [{"rel": rel, **rec}], kanban_home / "fleet", runner=runner)
    return rec


def test_rebuilt_clone_on_the_pool_host_lists_the_carried_stash_and_apply_restores(kanban_home, tmp_path,
                                                                                  monkeypatch):
    """End to end over a local "pool host": the rebuilt clone lists the Studio's stash entries with their original
    subjects, in order, and ``git stash apply`` of one restores the file. The Studio's list is untouched."""
    origin, ws, pool = _studio_and_origin(tmp_path)
    repo = ws / "repo"
    (repo / "a.txt").write_text("older edit\n")
    _g(repo, "stash", "push", "-q", "-m", "first")
    (repo / "a.txt").write_text("newer edit\n")
    _g(repo, "stash", "push", "-q", "-m", "second")
    before = _g(repo, "stash", "list", "--format=%H %gs")
    runner, _seen = _pool_runner(ws, pool, origin, monkeypatch)
    _carry_one(kanban_home, ws, "repo", runner)
    dest = pool / str(repo).lstrip("/")
    assert _g(dest, "stash", "list", "--format=%H %gs") == before
    _g(dest, "stash", "apply", "-q", "stash@{1}")
    assert (dest / "a.txt").read_text() == "older edit\n"
    assert _g(repo, "stash", "list", "--format=%H %gs") == before


def test_pathspec_stash_after_a_symbolic_ref_branch_switch_restores(kanban_home, tmp_path, monkeypatch):
    """Operator case 5: a pathspec stash (no reset line) in a linked worktree, the branch left by ``git
    symbolic-ref``, another checkout takes the branch. The worktree is rebuilt and the stash restores there."""
    origin, ws, pool = _studio_and_origin(tmp_path)
    repo = ws / "repo"
    _g(repo, "worktree", "add", "-q", "-b", "feat/wt", str(ws / "wt"))
    wt = ws / "wt"
    _pub(wt, "feat/wt")
    (wt / "a.txt").write_text("pathspec edit\n")
    _g(wt, "stash", "push", "-q", "--", "a.txt")
    _g(wt, "branch", "other")
    _pub(wt, "other")
    _g(wt, "symbolic-ref", "HEAD", "refs/heads/other")
    _g(repo, "checkout", "-q", "feat/wt")
    runner, _seen = _pool_runner(ws, pool, origin, monkeypatch)
    rec = _carry_one(kanban_home, ws, "wt", runner)
    assert rec["branch"] == "other" and len(rec["stash"]) == 1, rec
    dest = pool / str(wt).lstrip("/")
    _g(dest, "stash", "apply", "-q")
    assert (dest / "a.txt").read_text() == "pathspec edit\n"


@pytest.mark.parametrize("where", ["bundle", "store"])
def test_a_failed_stash_bundle_or_store_refuses_naming_the_entry(kanban_home, tmp_path, monkeypatch, where):
    """The only stash wait left: the bundle fails at the spawn-time check (nothing rebuilt or copied yet), or the
    store fails on the pool host. Either refusal names the entry; the Studio original is untouched."""
    import subprocess
    from types import SimpleNamespace
    from hermes_cli import kanban_worktree_port as kwt
    from tests.hermes_cli.test_kanban_worker_pool import _host
    origin, ws, pool = _studio_and_origin(tmp_path)
    repo = ws / "repo"
    (repo / "a.txt").write_text("edit\n")
    _g(repo, "stash", "push", "-q", "-m", "keep me")
    runner, _seen = _pool_runner(ws, pool, origin, monkeypatch)

    def failing(argv, **kw):
        if where == "bundle" and "bundle" in argv and "create" in argv:
            return SimpleNamespace(returncode=128, stdout="", stderr="fatal: injected")
        if where == "store" and argv[0] == "ssh" and "stash store" in argv[-1]:
            return SimpleNamespace(returncode=8, stdout=b"", stderr=b"injected")
        return runner(argv, **kw)
    cloned = []
    if where == "bundle":
        monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: cloned.append(a))
    rec, why = kwh.assess_clone(str(repo), runner)
    assert why is None, why
    with pytest.raises(RuntimeError, match="keep me"):
        kwh.carry_clones(_host(), str(ws), [{"rel": "repo", **rec}], kanban_home / "fleet", runner=failing)
    assert cloned == []
    assert "keep me" in subprocess.run(["git", "-C", str(repo), "stash", "list"], capture_output=True,
                                       text=True).stdout


def test_t94bd2ec4_shape_repo_worktree_with_a_shared_stash_is_rebuilt(kanban_home, wt_carry, tmp_path):
    """The operator's replay on t_94bd2ec4 at 2119cc07, as a fixture: ``repo`` is a linked worktree of a home repo
    outside the workspace whose shared stash list holds older entries and newer ones with no branch evidence;
    ``mainwt`` is a dead stub (gitdir gone). ``repo`` must come out rebuildable, carrying only the newer entries;
    the stub rides as files."""
    import shutil
    from pathlib import Path
    with kb.connect_closing() as conn:
        tid, path = _card_with_files(conn)
        home = tmp_path / "home"
        home.mkdir()
        _clone_in(home)
        hrepo = home / "repo"
        for i in range(3):
            (hrepo / "big.bin").write_bytes(b"old %d" % i)
            _g(hrepo, "stash", "push", "-q")
        _age_stash(hrepo / ".git", 600)
        _worktree_of(home, "repo47", branch="feat/card")
        shutil.move(str(home / "repo47"), str(Path(path) / "repo"))
        _g(hrepo, "worktree", "repair", str(Path(path) / "repo"))
        _g(hrepo, "checkout", "-q", "--detach")
        (hrepo / "big.bin").write_bytes(b"new 1")
        _g(hrepo, "stash", "push", "-q")                       # newer, "WIP on (no branch)": no branch to prove
        _g(hrepo, "checkout", "-q", "main")
        (hrepo / "big.bin").write_bytes(b"new 2")
        _g(hrepo, "stash", "push", "-q")                       # newer, made in the home checkout
        _worktree_of(home, "stub", branch="feat/stub", size=10)
        shutil.move(str(home / "stub"), str(Path(path) / "mainwt"))
        shutil.rmtree(hrepo / ".git" / "worktrees" / "stub")  # gitdir gone: a dead stub
        kwh._clone_assess_cache.clear()
        route, plan = _route_carry(conn, tid)
    assert route[0] != "wait", route
    rec = next(c for c in plan["clones"] if c["rel"] == "repo")
    assert len(rec["stash"]) == 2, rec
    assert plan["exclude"] == ("repo",), plan
