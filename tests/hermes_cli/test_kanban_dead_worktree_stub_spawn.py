"""A dead linked-worktree stub at a card's worktree target is moved aside or
held, and never burns the retry budget (t_42e59999).

Regression for t_5c6c67c5: ``.worktrees/<id>`` was a directory whose ``.git``
gitfile pointed at a pruned ``.git/worktrees/<id>``. Every ``git worktree add``
refused "already exists", and the card burned all 3 spawns to ``gave_up``.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worktree_stub as stubs


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def conn(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    with kb.connect() as c:
        yield c


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "a.txt").write_text("a\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "b.py").write_text("b = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "a")
    return repo


def card_with_dead_stub(conn, repo):
    """The incident shape: branch wt/<id> at main tip, its worktree's admin dir pruned."""
    tid = kb.create_task(conn, title="dead stub", assignee="daedalus-opus",
                         workspace_kind="worktree", workspace_path=str(repo))
    stub = repo / ".worktrees" / tid
    git(repo, "worktree", "add", "-q", "-b", f"wt/{tid}", str(stub), "HEAD")
    shutil.rmtree(repo / ".git" / "worktrees" / tid)
    with pytest.raises(subprocess.CalledProcessError):  # git itself refuses it now
        git(stub, "rev-parse", "--git-dir")
    return tid, stub


def row(conn, tid):
    return conn.execute(
        "SELECT status, consecutive_failures FROM tasks WHERE id=?", (tid,),
    ).fetchone()


def spawned_into(record):
    def spawn(task, workspace, board=None):
        record.append(Path(workspace))
        return 4242
    return spawn


def test_dead_stub_holding_nothing_unique_is_moved_aside_and_spawn_proceeds(conn, repo):
    tid, stub = card_with_dead_stub(conn, repo)
    launched = []

    result = kbd.dispatch_once(conn, spawn_fn=spawned_into(launched))

    assert tid not in result.spawn_failed
    assert launched == [stub.resolve()] or launched == [stub]
    assert git(stub, "branch", "--show-current") == f"wt/{tid}"
    aside = list((repo / ".worktrees" / ".aside").glob(f"{tid}-stub-*"))
    assert len(aside) == 1 and (aside[0] / "a.txt").read_text() == "a\n"
    assert row(conn, tid)["consecutive_failures"] == 0


def test_dead_stub_with_one_modified_file_is_held_untouched_and_uncharged(conn, repo):
    tid, stub = card_with_dead_stub(conn, repo)
    (stub / "pkg" / "b.py").write_text("b = 2  # unpushed work\n")
    before = sorted(p.relative_to(stub) for p in stub.rglob("*"))

    for _ in range(4):  # more ticks than the default retry budget
        result = kbd.dispatch_once(conn, spawn_fn=spawned_into([]))
        assert tid not in result.spawn_failed
        refused = dict(result.workspace_refused)
        assert refused[tid].startswith(f"{stubs.DEAD_STUB_HELD}: {stub}")
        assert "modified file pkg/b.py" in refused[tid]
        assert "mv " in refused[tid]  # names the operator command

    assert (stub / "pkg" / "b.py").read_text() == "b = 2  # unpushed work\n"
    assert sorted(p.relative_to(stub) for p in stub.rglob("*")) == before
    assert not (repo / ".worktrees" / ".aside").exists()
    state = row(conn, tid)
    assert state["status"] == "ready" and state["consecutive_failures"] == 0
    assert not conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,),
    ).fetchone()


def test_untracked_file_in_stub_is_held(conn, repo):
    tid, stub = card_with_dead_stub(conn, repo)
    (stub / "notes.md").write_text("draft\n")

    result = kbd.dispatch_once(conn, spawn_fn=spawned_into([]))

    assert "untracked file notes.md" in dict(result.workspace_refused)[tid]
    assert (stub / "notes.md").exists()


def assert_held(conn, tid, stub, result, reason):
    refused = dict(result.workspace_refused)[tid]
    assert refused.startswith(f"{stubs.DEAD_STUB_HELD}: {stub}") and reason in refused
    assert tid not in result.spawn_failed
    assert not (stub.parent / ".aside").exists()
    state = row(conn, tid)
    assert state["status"] == "ready" and state["consecutive_failures"] == 0


def test_tracked_file_deleted_from_stub_is_held(conn, repo):
    tid, stub = card_with_dead_stub(conn, repo)
    (stub / "pkg" / "b.py").unlink()

    result = kbd.dispatch_once(conn, spawn_fn=spawned_into([]))

    assert_held(conn, tid, stub, result, "deleted tracked file pkg/b.py")
    assert (stub / "a.txt").exists()


def test_chmod_only_change_in_stub_is_held(conn, repo):
    tid, stub = card_with_dead_stub(conn, repo)
    (stub / "a.txt").chmod(0o755)

    result = kbd.dispatch_once(conn, spawn_fn=spawned_into([]))

    assert_held(conn, tid, stub, result, "mode change a.txt (100644 -> 100755)")
    assert (stub / "a.txt").stat().st_mode & 0o100


def test_staged_blob_only_in_registered_index_is_held(repo):
    """Prism 8d54cfe00a58: the staged blob lives only in the admin dir's index."""
    stub = repo / ".worktrees" / "t_x"
    git(repo, "worktree", "add", "-q", "--detach", str(stub), "HEAD")
    (stub / "a.txt").write_text("staged only\n")
    git(stub, "add", "a.txt")
    git(stub, "restore", "--source=HEAD", "--worktree", "a.txt")
    (stub / ".git").write_text("gitdir: /nonexistent/admin\n")
    admin = repo / ".git" / "worktrees" / "t_x"
    index_before = (admin / "index").read_bytes()

    with pytest.raises(stubs.WorkspaceUnavailable, match="staged changes in"):
        stubs.clear_dead_stub(repo, stub, "HEAD")

    assert (admin / "index").read_bytes() == index_before
    assert not (repo / ".git" / "worktrees.aside").exists()
    assert (stub / "a.txt").read_text() == "a\n" and not (stub.parent / ".aside").exists()


def test_unreadable_target_is_held_not_charged(conn, repo):
    tid, stub = card_with_dead_stub(conn, repo)
    stub.chmod(0o000)
    try:
        result = kbd.dispatch_once(conn, spawn_fn=spawned_into([]))
    finally:
        stub.chmod(0o755)

    assert_held(conn, tid, stub, result, "unreadable")
    assert (stub / "a.txt").read_text() == "a\n"


def test_stub_check_over_budget_is_held_and_the_tick_stays_bounded(conn, repo, monkeypatch):
    """A stub too big to check in budget (a full checkout) is UNKNOWN: held, never moved."""
    tid, stub = card_with_dead_stub(conn, repo)
    budget, hash_s = 0.5, 30.0
    monkeypatch.setattr(stubs, "STUB_CHECK_BUDGET_S", budget)
    real_git = stubs._git

    def slow_hasher(repo_root, *args, input=None, timeout=120):
        if args and args[0] == "hash-object":  # a huge tree; honours its timeout like subprocess
            if timeout < hash_s:
                time.sleep(timeout)
                raise subprocess.TimeoutExpired(["git", *args], timeout)
            time.sleep(hash_s)
        return real_git(repo_root, *args, input=input, timeout=timeout)

    monkeypatch.setattr(stubs, "_git", slow_hasher)
    before = sorted(p.relative_to(stub) for p in stub.rglob("*"))

    started = time.monotonic()
    result = kbd.dispatch_once(conn, spawn_fn=spawned_into([]))
    elapsed = time.monotonic() - started

    assert elapsed < budget + 5.0
    refused = dict(result.workspace_refused)[tid]
    assert refused.startswith(f"{stubs.DEAD_STUB_HELD}: {stub}")
    assert f"check exceeded {budget:g}s" in refused
    assert tid not in result.spawn_failed
    assert sorted(p.relative_to(stub) for p in stub.rglob("*")) == before
    assert not (repo / ".worktrees" / ".aside").exists()
    state = row(conn, tid)
    assert state["status"] == "ready" and state["consecutive_failures"] == 0


def test_live_worktree_at_target_is_reused_unchanged(conn, repo):
    tid = kb.create_task(conn, title="live", assignee="daedalus-opus",
                         workspace_kind="worktree", workspace_path=str(repo))
    target = repo / ".worktrees" / tid
    git(repo, "worktree", "add", "-q", "-b", f"wt/{tid}", str(target), "HEAD")
    (target / "wip.txt").write_text("in-flight\n")
    launched = []

    result = kbd.dispatch_once(conn, spawn_fn=spawned_into(launched))

    assert tid not in result.spawn_failed and not result.workspace_refused
    assert len(launched) == 1
    assert (target / "wip.txt").read_text() == "in-flight\n"
    assert not (repo / ".worktrees" / ".aside").exists()


def test_still_registered_admin_dir_is_moved_out_of_worktrees(tmp_path, repo):
    """A stub whose registration survived (pointing at it) must not block the add."""
    stub = repo / ".worktrees" / "t_x"
    git(repo, "worktree", "add", "-q", "--detach", str(stub), "HEAD")
    (stub / ".git").write_text("gitdir: /nonexistent/admin\n")  # dead gitfile, admin kept

    aside = stubs.clear_dead_stub(repo, stub, "HEAD")

    assert not stub.exists() and (aside / "a.txt").exists()
    assert not (repo / ".git" / "worktrees" / "t_x").exists()
    assert list((repo / ".git" / "worktrees.aside").glob("t_x-*"))
    git(repo, "worktree", "add", "-q", "--detach", str(stub), "HEAD")
