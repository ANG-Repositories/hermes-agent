"""A timed-out ``git worktree add`` leaves nothing behind (t_f609a5a0).

The hang is a REAL git: a smudge filter that sleeps runs inside the add's ``git reset --hard`` child, i.e. inside
the window where git holds the new tree locked ``initializing`` and has not written its index yet, the exact state
t_33172156's spawn left on the Studio. The filter records its pid so the test can prove no process outlived the kill.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_worktree_residue as kwr


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, check=check).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.invalid")
    (repo / ".gitattributes").write_text("*.slow filter=slow\n")
    for i in range(3):
        (repo / f"f{i}.slow").write_text(f"{i}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    monkeypatch.setattr(kwr, "ADD_TIMEOUT_S", 2)
    return repo


def slow_checkout(repo, pidfile: Path, on=True):
    git(repo, "config", "filter.slow.smudge",
        f"sh -c 'echo $$ >> {pidfile}; sleep 60; cat'" if on else "cat")


def registrations(repo) -> int:
    return git(repo, "worktree", "list", "--porcelain").count("\nworktree ") + 1


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_hung_add_is_killed_whole_and_leaves_nothing_then_a_second_spawn_succeeds(repo, tmp_path):
    pidfile = tmp_path / "smudge.pids"
    slow_checkout(repo, pidfile)
    target = repo / ".worktrees" / "t_hang"
    before = registrations(repo)

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out after 2 seconds"):
        kbw._ensure_git_worktree(repo, target, "wt/t_hang")
    assert time.monotonic() - t0 < 30

    pids = [int(p) for p in pidfile.read_text().split()]
    assert pids, "the smudge filter never ran: the add did not hang inside its checkout"
    time.sleep(0.5)
    assert [p for p in pids if alive(p)] == []                      # no process of the group outlived it
    assert not target.exists()                                       # the path is gone
    assert registrations(repo) == before                             # the registration is gone
    assert not list((repo / ".git" / "worktrees").glob("*")) if (repo / ".git" / "worktrees").exists() else True
    assert git(repo, "branch", "--list", "wt/t_hang") == ""          # the branch it created is gone

    slow_checkout(repo, pidfile, on=False)
    kbw._ensure_git_worktree(repo, target, "wt/t_hang")              # the retry starts clean
    assert git(target, "branch", "--show-current") == "wt/t_hang"
    assert git(target, "status", "--porcelain") == ""
    assert registrations(repo) == before + 1


def test_a_timed_out_add_never_deletes_a_branch_it_did_not_create(repo, tmp_path):
    git(repo, "branch", "wt/t_mine")
    other = tmp_path / "scratch"
    git(repo, "worktree", "add", "-q", str(other), "wt/t_mine")
    (other / "work.txt").write_text("commits of its own\n")
    git(other, "add", "work.txt")
    git(other, "commit", "-qm", "own work")
    own = git(repo, "rev-parse", "wt/t_mine")
    git(repo, "worktree", "remove", str(other))
    slow_checkout(repo, tmp_path / "pids")
    target = repo / ".worktrees" / "t_mine"

    with pytest.raises(RuntimeError, match="timed out"):
        kbw._ensure_git_worktree(repo, target, "wt/t_mine")

    assert not target.exists()
    assert git(repo, "rev-parse", "wt/t_mine") == own                # pre-existing branch, untouched


def _half_built(repo, target, branch, reason=kwr.INIT_LOCK, age_s=0):
    """The incident state by hand: a registered tree with files, no index, locked ``reason``."""
    git(repo, "worktree", "add", "-q", "-b", branch, str(target), "HEAD")
    admin = Path(git(target, "rev-parse", "--absolute-git-dir"))
    (admin / "index").unlink()
    git(repo, "worktree", "lock", "--reason", reason, str(target))
    if age_s:
        old = time.time() - age_s
        os.utime(admin / "locked", (old, old))
    return admin


def test_a_lock_reason_other_than_initializing_is_never_touched(repo):
    target = repo / ".worktrees" / "t_keep"
    admin = _half_built(repo, target, "wt/t_keep", reason="operator: keep this", age_s=10_000)

    assert kwr.half_built(repo, target) is None
    assert kwr.clear_half_built(repo, target, branch="wt/t_keep")["cleared"] is False
    rep = kwr.remove_failed_add(repo, target, branch="wt/t_keep", created_branch=True,
                                base_sha=git(repo, "rev-parse", "HEAD"))
    assert rep["kept"] and not rep["removed"] and not rep["branch_deleted"]
    assert target.is_dir() and admin.is_dir()
    assert git(repo, "branch", "--list", "wt/t_keep") != ""


def test_a_half_built_tree_is_cleared_but_a_branch_with_its_own_commits_is_kept(repo):
    git(repo, "branch", "wt/t_own")
    tmp = repo.parent / "own"
    git(repo, "worktree", "add", "-q", str(tmp), "wt/t_own")
    (tmp / "x.txt").write_text("x\n")
    git(tmp, "add", "x.txt")
    git(tmp, "commit", "-qm", "own")
    own = git(repo, "rev-parse", "wt/t_own")
    git(repo, "worktree", "remove", str(tmp))
    target = repo / ".worktrees" / "t_own"
    git(repo, "worktree", "add", "-q", str(target), "wt/t_own")
    admin = Path(git(target, "rev-parse", "--absolute-git-dir"))
    (admin / "index").unlink()
    git(repo, "worktree", "lock", "--reason", kwr.INIT_LOCK, str(target))
    old = time.time() - 10_000
    os.utime(admin / "locked", (old, old))

    res = kwr.clear_half_built(repo, target, branch="wt/t_own")

    assert res["cleared"] is True and res["branch_kept"] is True
    assert not target.exists() and not admin.exists()
    assert git(repo, "rev-parse", "wt/t_own") == own


def test_a_young_half_built_tree_is_named_not_cleared(repo):
    target = repo / ".worktrees" / "t_young"
    _half_built(repo, target, "wt/t_young")
    res = kwr.clear_half_built(repo, target, branch="wt/t_young")
    assert res["cleared"] is False and "initializing lock is" in res["why"]
    assert target.is_dir()


def test_spawn_on_a_young_half_built_tree_is_held_uncharged_named(repo):
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable
    target = repo / ".worktrees" / "t_y2"
    _half_built(repo, target, "wt/t_y2")
    with pytest.raises(WorkspaceUnavailable, match=kwr.HALF_BUILT_HELD):
        kbw._ensure_git_worktree(repo, target, "wt/t_y2")


def test_spawn_on_an_old_half_built_tree_clears_it_and_builds_a_real_checkout(repo):
    target = repo / ".worktrees" / "t_old"
    _half_built(repo, target, "wt/t_old", age_s=10_000)
    kbw._ensure_git_worktree(repo, target, "wt/t_old")
    assert git(target, "status", "--porcelain") == ""                # the index exists again: a real checkout
    assert kwr.half_built(repo, target) is None


def _audit(*args):
    import sys
    script = Path(__file__).resolve().parents[2] / "scripts" / "kanban_worktree_residue_audit.py"
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)


def test_audit_drain_removes_only_dispatcher_trees_of_finished_cards(repo, tmp_path):
    import sqlite3
    db = tmp_path / "kanban.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, branch_name TEXT)")
    c.execute("CREATE TABLE task_events (task_id TEXT, kind TEXT)")
    c.executemany("INSERT INTO tasks VALUES (?, ?, NULL)", [("t_0000d0e0", "done"), ("t_000ready", "ready")])
    c.executemany("INSERT INTO task_events VALUES (?, 'spawned')", [("t_0000d0e0",), ("t_000ready",)])
    c.commit()
    c.close()
    wt = repo / ".worktrees"
    done = _half_built(repo, wt / "t_0000d0e0", "wt/t_0000d0e0", age_s=10_000)
    _half_built(repo, wt / "t_000ready", "wt/t_000ready", age_s=10_000)   # not hex: not a dispatcher id
    _half_built(repo, wt / "fold-x", "fold-x", age_s=10_000)               # another tool's tree
    _half_built(repo, wt / "t_0000beef", "wt/t_0000beef", age_s=10_000)    # no card with that id
    _half_built(repo, wt / "t_0000feed", "wt/t_0000feed", reason="operator", age_s=10_000)

    chk = _audit("--repo", str(repo), "--check")
    assert chk.returncode == 1 and chk.stdout.count("finding: locked-initializing") == 4
    dry = _audit("--repo", str(repo), "--db", str(db), "--drain")
    assert dry.returncode == 0 and "would remove" in dry.stdout and dry.stdout.count("would remove") == 1
    assert (wt / "t_0000d0e0").is_dir()                                  # a dry run touches nothing
    run = _audit("--repo", str(repo), "--db", str(db), "--drain", "--execute")
    assert run.returncode == 0, run.stderr
    assert not (wt / "t_0000d0e0").exists() and not done.exists()
    assert git(repo, "branch", "--list", "wt/t_0000d0e0") != ""          # no remote has its tip: branch kept
    for kept in ("t_000ready", "fold-x", "t_0000beef", "t_0000feed"):
        assert (wt / kept).is_dir(), kept


def test_audit_check_is_silent_when_clean(repo):
    r = _audit("--repo", str(repo), "--check")
    assert r.returncode == 0 and r.stdout == ""
