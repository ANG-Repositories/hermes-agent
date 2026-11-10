"""Worktree cards on pool hosts via the host's git mirror (t_6d9e7d90)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_pool as kwp
from hermes_cli import kanban_worktree_port as kwt

from tests.hermes_cli.test_kanban_worker_pool import _host, _plan, _tick, kanban_home  # noqa: F401

SLUG = "ANG-Ventures/hermes-home"


def _git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def anchor(tmp_path):
    repo = tmp_path / "anchor"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("remote", "add", "origin", f"https://github.com/{SLUG}.git", cwd=repo)
    kwt._origin_cache.clear()
    return repo


def _mirrors(home: Path, hosts=("ace-ai",), slug=SLUG):
    fleet = home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 1, "repos": {
        slug: {"mirror": "/srv/ci/git-mirrors/hermes-home-mirror.git", "hosts": list(hosts)}}}))


def _worktree_card(conn, anchor, body=None):
    return kb.create_task(conn, title="wt", assignee="alpha", body=body,
                          workspace_kind="worktree", workspace_path=str(anchor))


def _row(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()


def test_worktree_card_without_a_mirror_row_stays_local(kanban_home, anchor, monkeypatch):
    monkeypatch.setenv(kwt.DERIVED_ENV, "0")  # the hand list (kill switch, t_1d2c5939)
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        rules: dict = {}
        route, reason = kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules)
        assert route == "local"
        assert "worktree is Studio-only" in reason
        assert "kanban-worktree-mirrors.json" in reason
        assert "clean pushed" in reason
        assert rules[tid] == "workspace_kind"
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        assert spawned == []
        assert res.placement_waits[tid] == reason


def test_worktree_card_with_a_mirror_is_portable_and_clones_on_the_host(
        kanban_home, anchor, monkeypatch):
    _mirrors(kanban_home)
    calls = []

    def fake_clone(host, workspace, slug, mirror, branch, runner=None):
        calls.append((host.name, workspace, slug, mirror, branch))

    monkeypatch.setattr(kwt, "clone_on_host", fake_clone)
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("portable", None)
        assert out[tid][0] == SLUG
        res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        row = _row(conn, tid)
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                          (tid, kwt.PORTED_EVENT)).fetchone()
    assert (tid, "ace-ai") in spawned
    assert calls and calls[0][0] == "ace-ai" and calls[0][2] == SLUG and calls[0][4] == f"wt/{tid}"
    assert row["workspace_kind"] == "scratch" and row["workspace_path"] == calls[0][1]
    assert row["branch_name"] == f"wt/{tid}"
    assert json.loads(ev[0])["host"] == "ace-ai"
    assert not (anchor / ".worktrees" / tid).exists()  # no Studio worktree made
    # A rerun binds to the host holding the clone (Prism 5487e612d7e8).
    with kb.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
        conn.commit()
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules) == ("pin", "ace-ai")
        assert rules[tid].startswith("bind_host:ace-ai")


def test_worktree_card_pinned_to_a_host_without_the_mirror_stays_local(kanban_home, anchor, monkeypatch):
    monkeypatch.setenv(kwt.DERIVED_ENV, "0")  # the hand list (kill switch, t_1d2c5939)
    _mirrors(kanban_home, hosts=("ace-media",))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor, body="host: ace-ai")
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2))[0] in ("local", "wait")


def test_a_prior_local_worktree_keeps_the_card_local(kanban_home, anchor):
    _mirrors(kanban_home)
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        (anchor / ".worktrees" / tid).mkdir(parents=True)
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules)[0] == "local"
        assert rules[tid] == "workspace_kind"


def test_other_repo_does_not_port(kanban_home, anchor, monkeypatch):
    monkeypatch.setenv(kwt.DERIVED_ENV, "0")  # the hand list (kill switch, t_1d2c5939)
    _mirrors(kanban_home, slug="ANG-Ventures/something-else")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2))[0] == "local"


def test_read_mirrors_per_host_override_and_old_shape(tmp_path):
    mbp = "/Users/Shared/git-mirrors/hermes-home-mirror.git"
    (tmp_path / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 1, "repos": {
        SLUG: {"mirror": "/srv/ci/git-mirrors/hermes-home-mirror.git", "hosts": ["ci-box", "mbp-2026"],
               "mirrors": {"MBP-2026": mbp}},
        "ANG-Ventures/hermes-agent": {"mirror": "/srv/ci/git-mirrors/hermes-mirror.git", "hosts": ["ci-box"]},
        "ANG-Ventures/bad": {"mirror": "/srv/x.git", "hosts": ["ci-box"], "mirrors": {"mbp-2026": "rel/x.git"}},
        "ANG-Ventures/bad2": {"mirror": "/srv/x.git", "hosts": ["ci-box"], "mirrors": ["mbp-2026"]},
    }}))
    got = kwt.read_mirrors(tmp_path)
    assert got[SLUG.lower()] == ("/srv/ci/git-mirrors/hermes-home-mirror.git", ("ci-box", "mbp-2026"),
                                 {"mbp-2026": mbp})
    # Old shape: no override, every host reads ``mirror``.
    assert got["ang-ventures/hermes-agent"] == ("/srv/ci/git-mirrors/hermes-mirror.git", ("ci-box",), {})
    # A malformed override fails the row closed.
    assert "ang-ventures/bad" not in got and "ang-ventures/bad2" not in got
    port = (SLUG, *got[SLUG.lower()])
    assert kwt.mirror_for(port, "mbp-2026") == mbp
    assert kwt.mirror_for(port, "ci-box") == "/srv/ci/git-mirrors/hermes-home-mirror.git"
    assert kwt.mirror_for(port[:3], "mbp-2026") == "/srv/ci/git-mirrors/hermes-home-mirror.git"


def test_ported_clone_uses_the_placed_hosts_mirror(kanban_home, anchor, monkeypatch):
    mbp = "/Users/Shared/git-mirrors/hermes-home-mirror.git"
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 1, "repos": {
        SLUG: {"mirror": "/srv/ci/git-mirrors/hermes-home-mirror.git", "hosts": ["ace-ai"],
               "mirrors": {"ace-ai": mbp}}}}))
    calls = []
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None:
                        calls.append((host.name, mirror)))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                          (tid, kwt.PORTED_EVENT)).fetchone()
    assert (tid, "ace-ai") in spawned
    assert calls == [("ace-ai", mbp)]
    assert json.loads(ev[0])["mirror"] == mbp


def test_malformed_mirrors_file_ports_nothing(tmp_path):
    (tmp_path / kwt.MIRRORS_FILE).write_text("{not json")
    assert kwt.read_mirrors(tmp_path) == {}
    (tmp_path / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2, "repos": {}}))
    assert kwt.read_mirrors(tmp_path) == {}


def test_portable_worktree_kind_needs_hosts():
    pool = kwp.PoolConfig(hosts=("ace-ai",), profiles=("alpha",))
    kw = dict(workspace_has_content=False, assignee="alpha", body=None, pool=pool)
    assert kwp.portable(workspace_kind="worktree", **kw)[0] is False
    assert kwp.portable(workspace_kind="worktree", worktree_hosts=("ace-ai",), **kw)[0] is True
    assert kwp.portable(workspace_kind="dir", worktree_hosts=("ace-ai",), **kw)[0] is False


def test_clone_script_against_a_real_local_origin(tmp_path):
    """The remote sh, run locally: clone with reference, new branch off the
    default; a second run is a no-op; a non-empty foreign dir is refused."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "f").write_text("x")
    _git("add", "f", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c", cwd=origin)
    mirror = tmp_path / "mirror.git"
    _git("clone", "-q", "--mirror", str(origin), str(mirror), cwd=tmp_path)
    ws = tmp_path / "ws" / "t_x"
    script = kwt.clone_script(str(ws), SLUG, str(mirror), "wt/t_x", url=str(origin))
    assert subprocess.run(["sh", "-c", script]).returncode == 0
    out = subprocess.run(["git", "-C", str(ws), "branch", "--show-current"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "wt/t_x" and (ws / "f").read_text() == "x"
    # t_69e47673: the pool workspace is never world-searchable (git clone into the empty dir keeps its mode)
    assert (ws.stat().st_mode & 0o7777) == 0o770, oct(ws.stat().st_mode)
    # --dissociate: nothing borrowed from the mirror survives the clone
    assert not (ws / ".git" / "objects" / "info" / "alternates").exists()
    assert subprocess.run(["sh", "-c", script]).returncode == 0  # idempotent
    other = tmp_path / "ws" / "busy"
    other.mkdir()
    (other / "keep").write_text("1")
    bad = kwt.clone_script(str(other), SLUG, str(mirror), "b", url=str(origin))
    assert subprocess.run(["sh", "-c", bad]).returncode == 3
    assert (other / "keep").read_text() == "1"


@pytest.mark.parametrize("url, slug", [
    ("https://github.com/ANG-Ventures/hermes-home.git", "ANG-Ventures/hermes-home"),
    ("https://github.com/ANG-Ventures/hermes-agent", "ANG-Ventures/hermes-agent"),
    ("git@github.com:ANG-Ventures/hermes-home.git", "ANG-Ventures/hermes-home"),
    ("ssh://git@github.com/acme/repo.git", "acme/repo"),
    ("https://notgithub.com/acme/repo.git", None),           # Prism afc85da13a79
    ("https://evil.example/github.com/acme/repo.git", None),
    ("https://github.com.evil/acme/repo.git", None),
])
def test_origin_slug_matches_github_only(tmp_path, url, slug):
    repo = tmp_path / "r"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("remote", "add", "origin", url, cwd=repo)
    kwt._origin_cache.clear()
    assert kwt.origin_slug(repo) == slug


def _origin_with_feature(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    ident = ("-c", "user.email=t@t", "-c", "user.name=t")
    _git("init", "-q", "-b", "main", cwd=origin)
    (origin / "f").write_text("main")
    _git("add", "f", cwd=origin)
    _git(*ident, "commit", "-qm", "m", cwd=origin)
    _git("checkout", "-q", "-b", "feature", cwd=origin)
    (origin / "f").write_text("feature")
    _git(*ident, "commit", "-qam", "f", cwd=origin)
    _git("checkout", "-q", "main", cwd=origin)
    mirror = tmp_path / "mirror.git"
    _git("clone", "-q", "--mirror", str(origin), str(mirror), cwd=tmp_path)
    return origin, mirror


def test_existing_remote_branch_is_the_baseline(tmp_path):
    """Prism 9b397aa5183d: the baseline is the clone's own origin/<branch>."""
    origin, mirror = _origin_with_feature(tmp_path)
    ws = tmp_path / "ws"
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin))
    assert subprocess.run(["sh", "-c", sc]).returncode == 0
    assert (ws / "f").read_text() == "feature"


def test_rerun_after_an_interrupted_checkout_finishes_it(tmp_path):
    """Prism f43e48b43fc3: a clone left on main by a failed checkout is
    finished on the retry, never reported done on the wrong branch."""
    origin, mirror = _origin_with_feature(tmp_path)
    ws = tmp_path / "ws"
    _git("clone", "-q", str(origin), str(ws), cwd=tmp_path)   # clone ok, checkout "failed"
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin))
    assert subprocess.run(["sh", "-c", sc]).returncode == 0
    out = subprocess.run(["git", "-C", str(ws), "branch", "--show-current"],
                         capture_output=True, text=True).stdout.strip()
    assert out == "feature" and (ws / "f").read_text() == "feature"


def test_a_checkout_of_another_origin_is_refused(tmp_path):
    origin, mirror = _origin_with_feature(tmp_path)
    ws = tmp_path / "ws"
    _git("clone", "-q", str(origin), str(ws), cwd=tmp_path)
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url="https://github.com/x/y.git")
    assert subprocess.run(["sh", "-c", sc]).returncode == 3


def _commit(repo, name):
    (repo / name).write_text(name)
    _git("add", name, cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", name, cwd=repo)


def test_a_linked_worktree_path_is_not_an_anchor(kanban_home, anchor):
    """Prism 4a24c6d61776: a rerun whose persisted path is the Studio's
    .worktrees/<id> keeps its tree; it is never re-cloned on a pool host."""
    _mirrors(kanban_home)
    _commit(anchor, "base")
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        wt = anchor / ".worktrees" / tid
        _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(wt), cwd=anchor)
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(wt), tid))
        conn.commit()
        assert kwt.anchor_repo(tid, str(wt), None) is None
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2))[0] == "local"


def test_a_local_branch_ahead_of_origin_keeps_the_card_local(kanban_home, anchor):
    """Prism 044612a3554e: the card's branch has commits only the Studio has."""
    _mirrors(kanban_home)
    _commit(anchor, "base")
    _git("update-ref", "refs/remotes/origin/feature", "HEAD", cwd=anchor)
    _git("checkout", "-q", "-b", "feature", cwd=anchor)
    _commit(anchor, "local-only")
    _git("checkout", "-q", "main", cwd=anchor)
    assert kwt.anchor_repo("t_x", str(anchor), None, "feature") is None
    # in step with origin: portable
    _git("update-ref", "refs/remotes/origin/feature", "feature", cwd=anchor)
    assert kwt.anchor_repo("t_x", str(anchor), None, "feature") == anchor.resolve()
    # a local branch origin never saw: local
    _git("branch", "only-here", cwd=anchor)
    assert kwt.anchor_repo("t_x", str(anchor), None, "only-here") is None
    # no local branch at all: the clone makes it, portable
    assert kwt.anchor_repo("t_x", str(anchor), None, "wt/t_x") == anchor.resolve()


# ---- derived mode (t_1d2c5939): every repo, every pool host, push-then-port ----

def _origin_with_push(anchor, tmp_path):
    """Local fetch repository; tests can install a distinct pushurl to exercise it."""
    bare = tmp_path / "pushed.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    _git("remote", "set-url", "origin", str(bare), cwd=anchor)
    return bare


def _ref(repo, ref):
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", ref],
                          capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def test_derived_an_unlisted_repo_ports_to_every_pool_host(kanban_home, anchor, monkeypatch):
    """No mirrors file, a repo no hand list ever named: portable, every pool host, default mirror path."""
    _git("remote", "set-url", "origin", "https://github.com/ANG-Ventures/claude-apx.git", cwd=anchor)
    kwt._origin_cache.clear()
    assert not (kanban_home / "fleet" / kwt.MIRRORS_FILE).exists()
    calls = []
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None:
                        calls.append((host.name, slug, mirror, branch)))
    plan = _plan(hosts=[_host("ace-ai", slots=2), _host("ace-media", slots=2)])
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), plan, worktree_out=out) == ("portable", None)
        slug, mirror, hosts, per_host, push = out[tid]
        assert slug == "ANG-Ventures/claude-apx" and set(hosts) == {"ace-ai", "ace-media"}
        assert mirror == "~/.hermes/mirrors/ANG-Ventures/claude-apx.git" and push is None
        _res, spawned = _tick(conn, spillover=plan, spawn_limit=0, spawn_paused="test")
        row = _row(conn, tid)
    assert len(spawned) == 1 and spawned[0][1] in ("ace-ai", "ace-media")
    assert calls == [(spawned[0][1], "ANG-Ventures/claude-apx",
                      "~/.hermes/mirrors/ANG-Ventures/claude-apx.git", f"wt/{tid}")]
    assert row["workspace_kind"] == "scratch"


def test_derived_schema1_rows_are_mirror_paths_and_hosts_are_ignored(kanban_home, anchor):
    _mirrors(kanban_home, hosts=("ace-media",))  # the old row lists only ace-media
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor, body="host: ace-ai")
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("pin", "ace-ai")
    assert out[tid][1] == "/srv/ci/git-mirrors/hermes-home-mirror.git" and out[tid][2] == ("ace-ai",)


def test_derived_overrides_exclude_and_per_host_paths(tmp_path):
    (tmp_path / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2, "exclude": ["acme/Secret"],
        "mirror_paths": {SLUG: {"*": "/srv/m.git", "mbp-2026": "/Users/Shared/m.git"},
                         "acme/plain": "~/m/plain.git"}}))
    why: list = []
    assert kwt.derived_route("ACME/secret", tmp_path, ("ci-box",), why) is None and why == ["excluded"]
    assert kwt.derived_route(SLUG, tmp_path, ("ci-box", "MBP-2026")) == (
        "/srv/m.git", ("ci-box", "mbp-2026"), {"mbp-2026": "/Users/Shared/m.git"})
    assert kwt.derived_route("acme/plain", tmp_path, ("ci-box",))[0] == "~/m/plain.git"
    why = []
    assert kwt.derived_route("acme/x", tmp_path, (), why) is None and why == ["no_pool_host"]
    # A hand list cannot come back through schema 2: a malformed file ports nothing (fail closed).
    for bad in ({"schema": 2, "exclude": "acme/x"}, {"schema": 2, "mirror_paths": {SLUG: "rel/x.git"}},
                {"schema": 3}):
        (tmp_path / kwt.MIRRORS_FILE).write_text(json.dumps(bad))
        why = []
        assert kwt.derived_route(SLUG, tmp_path, ("ci-box",), why) is None
        assert why == ["overrides_file_malformed"]
    (tmp_path / kwt.MIRRORS_FILE).write_text("{not json")
    assert kwt.derived_route(SLUG, tmp_path, ("ci-box",)) is None


def test_derived_excluded_repo_stays_local_named(kanban_home, anchor):
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2, "exclude": [SLUG]}))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2)) == (
            "local", "not_portable:workspace_kind:excluded")


def test_kill_switch_file_or_env_restores_the_hand_list(kanban_home, anchor, monkeypatch):
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    assert kwt.derived_enabled(fleet)
    (fleet / kwt.MIRRORS_FILE).write_text(json.dumps({"schema": 2, "derived": False}))
    assert not kwt.derived_enabled(fleet)
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2))[0] == "portable"
    (fleet / kwt.MIRRORS_FILE).unlink()
    monkeypatch.setenv(kwt.DERIVED_ENV, "0")
    assert not kwt.derived_enabled(fleet)


def test_derived_unpushed_commits_are_pushed_then_ported(kanban_home, anchor, tmp_path, monkeypatch):
    """A card branch with commits origin never saw: pushed (lease: must not exist) BEFORE the clone."""
    bare = _origin_with_push(anchor, tmp_path)
    monkeypatch.setattr(kwt, "origin_slug", lambda repo, runner=None: SLUG)
    _commit(anchor, "base")
    order = []
    real_push = kwt.push_card_branch
    monkeypatch.setattr(kwt, "push_card_branch", lambda push, runner=subprocess.run:
                        (order.append("push"), real_push(push))[1])
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None, sha=None:
                        order.append(("clone", host.name, branch)))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        _git("branch", f"wt/{tid}", cwd=anchor)
        _git("checkout", "-q", f"wt/{tid}", cwd=anchor)
        _commit(anchor, "studio-only")
        _git("checkout", "-q", "main", cwd=anchor)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("portable", None)
        assert out[tid][4] == (str(anchor.resolve()), f"wt/{tid}", "", _ref(anchor, f"wt/{tid}"))
        _res, spawned = _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="test")
    assert (tid, "ace-ai") in spawned
    assert order == ["push", ("clone", "ace-ai", f"wt/{tid}")]
    assert _ref(bare, f"refs/heads/wt/{tid}") == _ref(anchor, f"refs/heads/wt/{tid}") != ""


def test_push_lease_refuses_a_branch_moved_on_origin(anchor, tmp_path):
    bare = _origin_with_push(anchor, tmp_path)
    _commit(anchor, "base")
    _git("push", "-q", "origin", "main:refs/heads/feature", cwd=anchor)  # someone else made it
    theirs = _ref(bare, "refs/heads/feature")
    _git("branch", "feature", cwd=anchor)
    _commit(anchor, "ours")
    _git("branch", "-f", "feature", "HEAD", cwd=anchor)
    # the routing saw no origin ref (""): origin's feature appeared since, so the lease refuses
    with pytest.raises(RuntimeError, match="before the port failed"):
        kwt.push_card_branch((str(anchor), "feature", "", _ref(anchor, "feature")))
    assert _ref(bare, "refs/heads/feature") == theirs


def test_derived_dirty_own_tree_stays_local_named(kanban_home, anchor, monkeypatch):
    """The only remaining local-only worktree case: the card's own Studio tree has uncommitted work."""
    _commit(anchor, "base")
    monkeypatch.setattr(kbd, "_studio_last_resort", lambda fleet_dir: True)
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        wt = anchor / ".worktrees" / tid
        _git("worktree", "add", "-q", "-b", f"wt/{tid}", str(wt), cwd=anchor)
        (wt / "scratch.txt").write_text("uncommitted")
        rules: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), rules_out=rules) == (
            "local", "not_portable:workspace_kind:unpushed_dirty_tree")
        assert rules[tid] == "workspace_kind"
        # committed (still unpushed): portable, with a push
        _git("add", "scratch.txt", cwd=wt)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c", cwd=wt)
        out: dict = {}
        assert kbd._route_row(conn, _row(conn, tid), _plan(free=2), worktree_out=out) == ("portable", None)
        assert out[tid][4][1:] == (f"wt/{tid}", "", _ref(wt, "HEAD"))


def test_derived_diverged_branch_stays_local_and_ahead_branch_pushes(anchor):
    _commit(anchor, "base")
    base = _ref(anchor, "HEAD")
    _git("branch", "feature", cwd=anchor)
    _git("update-ref", "refs/remotes/origin/feature", "HEAD", cwd=anchor)
    _git("checkout", "-q", "feature", cwd=anchor)
    _commit(anchor, "studio-only")
    _git("checkout", "-q", "main", cwd=anchor)
    # ahead only (fast-forward): pushed with the lease pinned to origin's sha
    assert kwt.assess("t_x", str(anchor), None, "feature", push_ok=True) == (
        anchor.resolve(), (str(anchor.resolve()), "feature", base, _ref(anchor, "feature")))
    # the hand-list semantics are unchanged: local
    assert kwt.anchor_repo("t_x", str(anchor), None, "feature") is None
    # origin moved to a commit the Studio lacks: a push would drop it, so local, named
    _commit(anchor, "origin-only")
    _git("update-ref", "refs/remotes/origin/feature", "main", cwd=anchor)
    why: list = []
    assert kwt.assess("t_x", str(anchor), None, "feature", push_ok=True, why=why) == (None, None)
    assert why == ["diverged_from_origin"]


def test_push_uses_assessed_sha_despite_branch_reset_and_ignores_pushurl(anchor, tmp_path):
    bare = _origin_with_push(anchor, tmp_path)
    _commit(anchor, "parent")
    parent = _ref(anchor, "HEAD")
    _commit(anchor, "remote")
    _git("push", "-q", "origin", "main:refs/heads/feature", cwd=anchor)
    remote = _ref(anchor, "HEAD")
    _git("checkout", "-q", "-b", "feature", cwd=anchor)
    _commit(anchor, "assessed")
    assessed = _ref(anchor, "HEAD")
    _, push = kwt.assess("t_x", str(anchor), None, "feature", push_ok=True)
    other = tmp_path / "wrong.git"
    _git("init", "-q", "--bare", str(other), cwd=tmp_path)
    _git("remote", "set-url", "--push", "origin", str(other), cwd=anchor)
    _git("config", f"url.{other}.pushInsteadOf", str(bare), cwd=anchor)
    _git("checkout", "-q", "main", cwd=anchor)
    _git("branch", "-f", "feature", parent, cwd=anchor)
    kwt.push_card_branch(push)
    assert _ref(bare, "refs/heads/feature") == assessed != remote
    assert not _ref(other, "refs/heads/feature")


def test_assessment_checks_immutable_shas_when_branch_moves(anchor):
    _commit(anchor, "base")
    _git("branch", "feature", cwd=anchor)
    _commit(anchor, "remote")
    remote = _ref(anchor, "HEAD")
    _git("update-ref", "refs/remotes/origin/feature", remote, cwd=anchor)
    _git("checkout", "-q", "feature", cwd=anchor)
    _commit(anchor, "divergent")
    divergent = _ref(anchor, "HEAD")
    def racing_runner(argv, **kwargs):
        if "rev-list" in argv:
            _git("update-ref", "refs/heads/feature", remote, cwd=anchor)
        return subprocess.run(argv, **kwargs)
    why = []
    assert kwt.assess("t_x", str(anchor), None, "feature", runner=racing_runner,
                      push_ok=True, why=why) == (None, None)
    assert why == ["diverged_from_origin"]
    assert divergent != remote


def test_clone_script_expands_a_home_relative_mirror(tmp_path):
    origin, mirror = _origin_with_feature(tmp_path)
    home = tmp_path / "home"
    (home / ".hermes" / "mirrors" / "acme").mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--mirror", str(origin),
                    str(home / ".hermes" / "mirrors" / "acme" / "r.git")], check=True)
    ws = tmp_path / "ws"
    sc = kwt.clone_script(str(ws), "acme/r", "~/.hermes/mirrors/acme/r.git", "feature", url=str(origin))
    import os
    env = dict(os.environ, HOME=str(home))
    assert subprocess.run(["sh", "-c", sc], env=env).returncode == 0
    assert (ws / "f").read_text() == "feature"
    # absent mirror: --reference-if-able, a plain clone, never a refusal
    ws2 = tmp_path / "ws2"
    sc2 = kwt.clone_script(str(ws2), "acme/r", "~/.hermes/mirrors/acme/none.git", "feature", url=str(origin))
    assert subprocess.run(["sh", "-c", sc2], env=env).returncode == 0


# -- pool-host clone off the dispatcher tick (t_915b8d2d) ------------------------------------------------------

@pytest.fixture
def clone_prefetch():
    kwt._prefetch.clear()
    yield kwt._prefetch
    for fut, _t0 in list(kwt._prefetch.values()):
        fut.cancel()
    kwt._prefetch.clear()


def _wait_prefetch_done(timeout=10.0):
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if kwt._prefetch and all(f.done() for f, _ in kwt._prefetch.values()):
            return
        _t.sleep(0.02)
    raise AssertionError("background clone never finished")


def test_gateway_tick_does_not_wait_on_a_slow_pool_host_clone(kanban_home, anchor, monkeypatch, clone_prefetch):
    """A clone that takes as long as it likes never holds up the tick: the card waits clone_pending, its slot
    goes back to the next portable card in the SAME tick, and the clone's card launches on a later tick."""
    import threading
    import time as _t
    _mirrors(kanban_home)
    gate, calls = threading.Event(), []

    def slow_clone(host, workspace, slug, mirror, branch, runner=None):
        calls.append((host.name, workspace, branch))
        assert gate.wait(30), "test released the clone"

    monkeypatch.setattr(kwt, "clone_on_host", slow_clone)
    with kb.connect_closing() as conn:
        wt = kb.create_task(conn, title="wt", assignee="alpha", priority=9,
                            workspace_kind="worktree", workspace_path=str(anchor))
        other = kb.create_task(conn, title="plain", assignee="alpha", priority=1)
        t0 = _t.monotonic()
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                             defer_pool_clone=True)
        wall = _t.monotonic() - t0
        assert wall < 10, f"tick waited on the clone ({wall:.1f}s)"
        assert res.placement_waits[wt] == f"{kwt.CLONE_PENDING}:ace-ai"
        assert (other, "ace-ai") in spawned          # the slot went to the next portable card this tick
        assert all(tid != wt for tid, _ in spawned)
        assert _row(conn, wt)["status"] == "ready"   # never claimed while the clone runs
        assert res.clone_ms < 5000
        gate.set()
        _wait_prefetch_done()
        conn.execute("UPDATE tasks SET status='done', claim_lock=NULL WHERE id=?", (other,))
        conn.commit()
        res2, spawned2 = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                               defer_pool_clone=True)
        row = _row(conn, wt)
    assert (wt, "ace-ai") in spawned2
    assert len(calls) == 1                           # collected, not cloned a second time
    assert row["workspace_kind"] == "scratch" and row["workspace_path"] == calls[0][1]
    assert calls[0][2] == f"wt/{wt}"
    assert not kwt._prefetch


def test_failed_background_clone_takes_the_existing_failure_path(kanban_home, anchor, monkeypatch,
                                                                 clone_prefetch):
    _mirrors(kanban_home)

    def bad_clone(host, workspace, slug, mirror, branch, runner=None):
        raise RuntimeError(f"worktree clone of {slug} on {host.name} failed rc=128: boom")

    monkeypatch.setattr(kwt, "clone_on_host", bad_clone)
    with kb.connect_closing() as conn:
        wt = _worktree_card(conn, anchor)
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                             defer_pool_clone=True)
        assert spawned == [] and res.placement_waits[wt].startswith(kwt.CLONE_PENDING)
        _wait_prefetch_done()
        plan = _plan(free=1)
        res2, spawned2 = _tick(conn, spillover=plan, spawn_limit=0, spawn_paused="test",
                               defer_pool_clone=True)
        row = _row(conn, wt)
        run = conn.execute("SELECT outcome, error FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                           (wt,)).fetchone()
        # The refunded slot is free again: a fresh portable card takes it in the next tick.
        other = kb.create_task(conn, title="plain", assignee="alpha")
        _res3, spawned3 = _tick(conn, spillover=plan, spawn_limit=0, spawn_paused="test")
    assert spawned2 == [] and wt in res2.spawn_failed
    assert row["status"] in ("ready", "blocked") and row["claim_lock"] is None
    assert run["outcome"] == "spawn_failed" and "boom" in (run["error"] or "")
    assert (other, "ace-ai") in spawned3


def test_cli_tick_keeps_the_synchronous_clone(kanban_home, anchor, monkeypatch, clone_prefetch):
    """Without defer_pool_clone (CLI, admit, dashboard) the clone runs in the tick as before: a one-shot
    process must not exit with a clone thread still running."""
    _mirrors(kanban_home)
    calls = []
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None:
                        calls.append(host.name))
    with kb.connect_closing() as conn:
        wt = _worktree_card(conn, anchor)
        _res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
    assert (wt, "ace-ai") in spawned and calls == ["ace-ai"] and not kwt._prefetch


# -- Prism 3e2f8506c8ff / d4847ef87c43: the pending clone is keyed on the tip and visible to every dispatcher ---

def test_tip_moved_after_prefetch_never_launches_the_stale_clone(kanban_home, anchor, tmp_path, monkeypatch,
                                                                 clone_prefetch):
    """Prism 3e2f8506c8ff: prefetch at A, commit B, dispatch -> the card launches on B, never on A's clone."""
    _origin_with_push(anchor, tmp_path)
    monkeypatch.setattr(kwt, "origin_slug", lambda repo, runner=None: SLUG)
    _commit(anchor, "base")
    pushes, clones = [], []
    real_push = kwt.push_card_branch
    monkeypatch.setattr(kwt, "push_card_branch", lambda push, runner=subprocess.run:
                        (pushes.append(push[3]), real_push(push))[1])
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None, sha=None:
                        clones.append(sha))
    with kb.connect_closing() as conn:
        tid = _worktree_card(conn, anchor)
        _git("checkout", "-q", "-b", f"wt/{tid}", cwd=anchor)
        _commit(anchor, "A")
        sha_a = _ref(anchor, f"wt/{tid}")
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                             defer_pool_clone=True)
        assert spawned == [] and res.placement_waits[tid].startswith(kwt.CLONE_PENDING)
        _wait_prefetch_done()
        _git("fetch", "-q", "origin", cwd=anchor)       # the Studio sees its own push
        _commit(anchor, "B")                             # the tip moves before the clone is collected
        sha_b = _ref(anchor, f"wt/{tid}")
        _git("checkout", "-q", "main", cwd=anchor)
        res2, spawned2 = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                               defer_pool_clone=True)
        assert spawned2 == [], "launched on the clone of the old tip"
        assert res2.placement_waits[tid].startswith(kwt.CLONE_PENDING)
        _wait_prefetch_done()
        _res3, spawned3 = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                                defer_pool_clone=True)
    assert (tid, "ace-ai") in spawned3
    assert pushes == [sha_a, sha_b]
    assert clones == [sha_a, sha_b]                      # the launched workspace is pinned to B
    assert not kwt._prefetch


def test_deferred_then_sync_tick_never_starts_a_second_clone(kanban_home, anchor, monkeypatch, clone_prefetch):
    """Prism d4847ef87c43: a gateway (deferred) tick starts the clone; a CLI/admit (synchronous) tick before it
    finishes must skip the card, never run a second writer into the same workspace."""
    import threading
    _mirrors(kanban_home)
    gate, calls = threading.Event(), []

    def slow_clone(host, workspace, slug, mirror, branch, runner=None):
        calls.append(workspace)
        assert gate.wait(30), "test released the clone"

    monkeypatch.setattr(kwt, "clone_on_host", slow_clone)
    with kb.connect_closing() as conn:
        wt = _worktree_card(conn, anchor)
        _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test", defer_pool_clone=True)
        res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
        assert spawned == [] and res.placement_waits[wt].startswith(kwt.CLONE_PENDING)
        assert _row(conn, wt)["status"] == "ready"
        assert len(calls) == 1, "a second clone started while the first was running"
        gate.set()
        _wait_prefetch_done()
        _res2, spawned2 = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
    assert (wt, "ace-ai") in spawned2 and len(calls) == 1   # the sync tick collected the finished clone


def test_a_clone_held_by_another_dispatcher_process_is_skipped_by_every_tick(kanban_home, anchor, monkeypatch,
                                                                            clone_prefetch):
    """The pending marker is a per-workspace lock any dispatcher process sees (the in-process cache is not):
    while another process holds it, deferred and synchronous ticks both skip the card; it dies with that
    process, so a crashed dispatcher never strands the card."""
    import sys
    _mirrors(kanban_home)
    calls = []
    monkeypatch.setattr(kwt, "clone_on_host", lambda host, ws, slug, mirror, branch, runner=None:
                        calls.append(ws))
    with kb.connect_closing() as conn:
        wt = _worktree_card(conn, anchor)
        out: dict = {}
        kbd._route_row(conn, _row(conn, wt), _plan(free=1), worktree_out=out)
        _key, _job, lock = kbd._pool_clone_job(wt, None, _host(), out[wt], kb.workspaces_root() / wt)
        lock.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import fcntl,os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600); "
                                   "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)",
                                   str(lock)], stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "held"
            for defer in (True, False):
                res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test",
                                     defer_pool_clone=defer)
                assert spawned == [] and res.placement_waits[wt].startswith(kwt.CLONE_PENDING), defer
            assert calls == [] and not kwt._prefetch
        finally:
            holder.kill()
            holder.wait()
        _res, spawned = _tick(conn, spillover=_plan(free=1), spawn_limit=0, spawn_paused="test")
    assert (wt, "ace-ai") in spawned and len(calls) == 1


def test_clone_script_pins_a_reused_workspace_to_the_tip(tmp_path):
    """A workspace an earlier (stale) clone left at A is moved to the pushed tip B, and proved there (rc 5)."""
    origin, mirror = _origin_with_feature(tmp_path)
    ws = tmp_path / "ws"
    sha_a = _ref(origin, "feature")
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin), sha=sha_a)
    assert subprocess.run(["sh", "-c", sc]).returncode == 0
    _git("checkout", "-q", "feature", cwd=origin)
    _commit(origin, "b")
    _git("checkout", "-q", "main", cwd=origin)
    sha_b = _ref(origin, "feature")
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin), sha=sha_b)
    assert subprocess.run(["sh", "-c", sc]).returncode == 0
    assert _ref(ws, "HEAD") == sha_b
    sc = kwt.clone_script(str(ws), SLUG, str(mirror), "feature", url=str(origin), sha="0" * 40)
    assert subprocess.run(["sh", "-c", sc], capture_output=True).returncode == 5
