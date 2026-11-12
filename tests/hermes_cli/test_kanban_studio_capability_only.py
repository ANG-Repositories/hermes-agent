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
    assert "clone repo cannot be rebuilt from origin: unpushed_commits_on_default_branch" in route[1], route
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
    assert "clone repo cannot be rebuilt from origin: dirty_tree" in route[1], route


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


def test_clone_with_a_stash_is_refused_at_routing_and_at_spawn(kanban_home, tmp_path, monkeypatch):
    """Prism 425d85646d96: a stash lives only in the Studio clone's .git and a re-clone drops it."""
    import subprocess
    from hermes_cli import kanban_worktree_port as kwt
    from tests.hermes_cli.test_kanban_worker_pool import _host
    ws = tmp_path / "ws"
    ws.mkdir()
    sha = _clone_in(ws)
    repo = str(ws / "repo")
    rec, why = kwh.assess_clone(repo)
    assert why is None
    (ws / "repo" / "big.bin").write_bytes(b"stashed edit")
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "stash", "-q"], check=True)
    assert kwh.assess_clone(repo) == (None, "stash_present")
    cloned = []
    monkeypatch.setattr(kwt, "clone_on_host", lambda *a, **k: cloned.append(a))
    clone = [{"rel": "repo", "slug": "acme/widget", "branch": "main", "sha": sha, "push": None}]
    with pytest.raises(RuntimeError, match="has a stash"):
        kwh.carry_clones(_host(), str(ws), clone, kanban_home / "fleet")
    assert cloned == []


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


def test_unrecoverable_small_worktree_whose_clone_is_outside_is_carried_as_files(kanban_home, wt_carry, tmp_path):
    """Its main checkout (and gitdir) stays on the Studio untouched; the dirty worktree's files ride the carry and
    count, while the clone next to it is rebuilt."""
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
    assert route[0] != "wait", route
    assert plan["exclude"] == ("repo",) and [c["rel"] for c in plan["clones"]] == ["repo"], plan


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
