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
                          text=True, encoding="utf-8", errors="replace", check=check).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.invalid")
    (repo / ".gitattributes").write_text("*.slow filter=slow\n", encoding="utf-8")
    for i in range(3):
        (repo / f"f{i}.slow").write_text(f"{i}\n", encoding="utf-8")
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
        os.kill(pid, 0)  # windows-footgun: ok — the test drives POSIX process groups
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

    pids = [int(p) for p in pidfile.read_text(encoding="utf-8-sig").split()]
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
    (other / "work.txt").write_text("commits of its own\n", encoding="utf-8")
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
        _age_tree(target, admin, age_s)
    return admin


def _age_tree(target: Path, admin: Path, age_s: int) -> None:
    """A real dead add: its lock and every file it checked out are as old as the kill."""
    old = time.time() - age_s
    for p in [admin / "locked", *(q for q in target.rglob("*") if ".git" not in q.parts and q.is_file())]:
        os.utime(p, (old, old))


def test_a_lock_reason_other_than_initializing_is_never_touched(repo):
    target = repo / ".worktrees" / "t_keep"
    admin = _half_built(repo, target, "wt/t_keep", reason="operator: keep this", age_s=10_000)

    assert kwr.half_built(repo, target) is None
    assert kwr.clear_half_built(repo, target, branch="wt/t_keep")["cleared"] is False
    rep = kwr.remove_failed_add(repo, target, pre={"absent": True, "admins": []}, branch="wt/t_keep",
                                created_branch=True, base_sha=git(repo, "rev-parse", "HEAD"), timed_out=True)
    assert rep["kept"] and not rep["removed"] and not rep["branch_deleted"]
    assert kwr.drain_half_built(repo, target, branch="wt/t_keep", delete_branch=True)["kept"]
    assert target.is_dir() and admin.is_dir()
    assert git(repo, "branch", "--list", "wt/t_keep") != ""


def test_a_half_built_tree_is_cleared_but_a_branch_with_its_own_commits_is_kept(repo):
    git(repo, "branch", "wt/t_own")
    tmp = repo.parent / "own"
    git(repo, "worktree", "add", "-q", str(tmp), "wt/t_own")
    (tmp / "x.txt").write_text("x\n", encoding="utf-8")
    git(tmp, "add", "x.txt")
    git(tmp, "commit", "-qm", "own")
    own = git(repo, "rev-parse", "wt/t_own")
    git(repo, "worktree", "remove", str(tmp))
    target = repo / ".worktrees" / "t_own"
    git(repo, "worktree", "add", "-q", str(target), "wt/t_own")
    admin = Path(git(target, "rev-parse", "--absolute-git-dir"))
    (admin / "index").unlink()
    git(repo, "worktree", "lock", "--reason", kwr.INIT_LOCK, str(target))
    _age_tree(target, admin, 10_000)

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
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, encoding="utf-8")


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


def test_a_half_built_tree_someone_wrote_into_after_the_add_died_is_kept(repo):
    target = repo / ".worktrees" / "t_late"
    _half_built(repo, target, "wt/t_late", age_s=10_000)
    (target / "notes.txt").write_text("written into the dead tree by hand\n", encoding="utf-8")
    res = kwr.clear_half_built(repo, target, branch="wt/t_late")
    assert res["cleared"] is False and "written after the add died" in res["why"]
    assert (target / "notes.txt").is_file()


# --- review r1 (Apollo, hermes-agent#265 @329537e1): a failed add never deletes what it did not create ----------


def _race(monkeypatch, make_foreign):
    """The check-then-act gap: ``make_foreign`` runs after the spawn's occupied check passed, before its add."""
    from hermes_cli import kanban_worktree_stub as kws

    def occupied_then_raced(repo_root, target):
        make_foreign(target)
        return False
    monkeypatch.setattr(kws, "target_occupied", occupied_then_raced)


def test_already_exists_never_removes_a_foreign_finished_tree_with_uncommitted_work(repo, monkeypatch):
    target = repo / ".worktrees" / "t_cardA"

    def foreign(t):
        git(repo, "worktree", "add", "-q", "-b", "wt/other", str(t), "HEAD")
        (t / "uncommitted.txt").write_text("someone's work\n", encoding="utf-8")
    _race(monkeypatch, foreign)
    before = registrations(repo)

    with pytest.raises(RuntimeError, match="already exists"):
        kbw._ensure_git_worktree(repo, target, "wt/t_cardA")

    assert (target / "uncommitted.txt").read_text(encoding="utf-8") == "someone's work\n"
    assert registrations(repo) == before + 1
    assert git(target, "branch", "--show-current") == "wt/other"


def test_already_exists_never_removes_a_foreign_plain_directory(repo, monkeypatch):
    target = repo / ".worktrees" / "t_cardB"

    def foreign(t):
        t.mkdir(parents=True)
        (t / "file.txt").write_text("not a worktree\n", encoding="utf-8")
    _race(monkeypatch, foreign)

    with pytest.raises(RuntimeError, match="already exists"):
        kbw._ensure_git_worktree(repo, target, "wt/t_cardB")

    assert (target / "file.txt").read_text(encoding="utf-8") == "not a worktree\n"


def test_remove_failed_add_keeps_a_registration_that_predates_the_add(repo):
    """Function level, both shapes, without the stderr hint: the snapshot alone keeps them."""
    target = repo / ".worktrees" / "t_pre"
    git(repo, "worktree", "add", "-q", "-b", "wt/pre", str(target), "HEAD")
    (target / "u.txt").write_text("u\n", encoding="utf-8")
    pre = kwr.snapshot(repo, target)
    rep = kwr.remove_failed_add(repo, target, pre=pre, branch="wt/t_pre", created_branch=False,
                                base_sha=git(repo, "rev-parse", "HEAD"), timed_out=True)
    assert not rep["removed"] and (target / "u.txt").is_file() and kwr.admin_dirs_for(kwr.common_dir(repo), target)

    plain = repo / ".worktrees" / "t_plain"
    plain.mkdir()
    (plain / "f").write_text("f\n", encoding="utf-8")
    pre = kwr.snapshot(repo, plain)
    rep = kwr.remove_failed_add(repo, plain, pre=pre, branch="wt/t_plain", created_branch=False, base_sha="",
                                timed_out=True)
    assert not rep["removed"] and (plain / "f").is_file()


def test_a_finished_tree_that_appeared_during_a_timed_out_add_is_kept(repo):
    """New since the snapshot, but it has an index (a concurrent add that finished): not this add's residue."""
    target = repo / ".worktrees" / "t_conc"
    pre = kwr.snapshot(repo, target)
    git(repo, "worktree", "add", "-q", "-b", "wt/t_conc", str(target), "HEAD")
    (target / "w.txt").write_text("w\n", encoding="utf-8")
    rep = kwr.remove_failed_add(repo, target, pre=pre, branch="wt/t_conc", created_branch=True,
                                base_sha=git(repo, "rev-parse", "HEAD"), timed_out=True)
    assert not rep["removed"] and (target / "w.txt").is_file()
    assert not rep["branch_deleted"]                                 # checked out in that tree


def _with_remote(repo, tmp_path):
    """A bare clone as ``origin``: the trunk ref is ``origin/main``."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True, capture_output=True)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "fetch", "-q", "origin")
    return remote


def test_clear_half_built_keeps_a_pushed_card_branch_and_the_next_spawn_resumes_it(repo, tmp_path):
    _with_remote(repo, tmp_path)
    git(repo, "branch", "wt/t_push")
    tmp = tmp_path / "work"
    git(repo, "worktree", "add", "-q", str(tmp), "wt/t_push")
    (tmp / "done.txt").write_text("pushed work\n", encoding="utf-8")
    git(tmp, "add", "done.txt")
    git(tmp, "commit", "-qm", "worker commit")
    git(tmp, "push", "-q", "origin", "wt/t_push")
    tip = git(repo, "rev-parse", "wt/t_push")
    git(repo, "worktree", "remove", str(tmp))
    target = repo / ".worktrees" / "t_push"
    git(repo, "worktree", "add", "-q", str(target), "wt/t_push")    # the re-add, then killed
    admin = Path(git(target, "rev-parse", "--absolute-git-dir"))
    (admin / "index").unlink()
    git(repo, "worktree", "lock", "--reason", kwr.INIT_LOCK, str(target))
    _age_tree(target, admin, 10_000)

    res = kwr.clear_half_built(repo, target, branch="wt/t_push")

    assert res["cleared"] is True and res["branch_kept"] is True
    assert git(repo, "rev-parse", "wt/t_push") == tip                # the recorded branch_name still resolves
    kbw._ensure_git_worktree(repo, target, "wt/t_push")
    assert git(target, "rev-parse", "HEAD") == tip and (target / "done.txt").is_file()


def test_clear_half_built_deletes_a_branch_the_trunk_already_contains(repo, tmp_path):
    _with_remote(repo, tmp_path)
    target = repo / ".worktrees" / "t_triv"
    _half_built(repo, target, "wt/t_triv", age_s=10_000)
    res = kwr.clear_half_built(repo, target, branch="wt/t_triv")
    assert res["cleared"] is True and res["branch_kept"] is False
    assert git(repo, "branch", "--list", "wt/t_triv") == ""
    kbw._ensure_git_worktree(repo, target, "wt/t_triv")              # re-created from the trunk
    assert git(target, "branch", "--show-current") == "wt/t_triv"
