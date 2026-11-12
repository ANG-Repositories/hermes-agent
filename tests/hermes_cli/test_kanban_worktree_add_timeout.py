"""A timed-out ``git worktree add`` is killed whole and named; nothing is ever deleted (t_f609a5a0).

The hang is a REAL git: a smudge filter that sleeps runs inside the add's ``git reset --hard`` child, i.e. inside
the window where git holds the new tree locked ``initializing`` and has not written its index yet, the exact state
t_33172156's spawn left on the Studio. The filter records its pid so the test can prove no process outlived the kill.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_worktree_residue as kwr


_TXT = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")


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
    """The smudge filter records its pid and hangs. ``pidfile`` is shell-quoted (Prism #265 P2 c-d5881e1a2f1d7ae5):
    a tmp path with a space or quote still lands in the file."""
    script = f"echo $$ >> {shlex.quote(str(pidfile))}; sleep 60; cat"
    git(repo, "config", "filter.slow.smudge", f"sh -c {shlex.quote(script)}" if on else "cat")


def _wait_for(pred, timeout=30.0):
    """Poll ``pred`` (a readiness marker, never a fixed sleep: Prism #265 P2 c-ca6771b4c8c38a46)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def registrations(repo) -> int:
    return git(repo, "worktree", "list", "--porcelain").count("\nworktree ") + 1


def alive(pid: int) -> bool:
    """False once ``pid`` is gone OR a zombie (Prism #265 P2 c-658240d2a3610b9e): a killed process whose parent
    has not reaped it yet still answers kill(0) but runs nothing."""
    try:
        os.kill(pid, 0)  # windows-footgun: ok — the test drives POSIX process groups
    except ProcessLookupError:
        return False
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, encoding="utf-8",
                        errors="replace").stdout.strip()
    return bool(st) and not st.startswith("Z")


def test_a_hung_add_is_killed_whole_and_the_spawn_removes_nothing(repo, tmp_path):
    pidfile = tmp_path / "smudge pids 'q'"                            # a space and quotes: the path is quoted
    slow_checkout(repo, pidfile)
    target = repo / ".worktrees" / "t_hang"
    before = registrations(repo)

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out after 2 seconds") as ei:
        kbw._ensure_git_worktree(repo, target, "wt/t_hang")
    assert time.monotonic() - t0 < 30
    assert "nothing removed" in str(ei.value) and "inspect" in str(ei.value)

    pids = [int(p) for p in pidfile.read_text(encoding="utf-8-sig").split()]
    assert pids, "the smudge filter never ran: the add did not hang inside its checkout"
    assert _wait_for(lambda: not [p for p in pids if alive(p)], 5.0)  # no process of the group outlived it
    # SIGKILL only: git's SIGTERM cleanup never ran, so the half-built tree, its registration and the branch stay,
    # named and held for the operator.
    assert target.is_dir() and kwr.half_built(repo, target) is not None
    assert registrations(repo) == before + 1
    assert git(repo, "branch", "--list", "wt/t_hang") != ""


def test_a_file_written_into_the_target_before_the_timeout_survives_the_kill(repo, tmp_path):
    """Prism #265 P1 c-f6873eb7b7d1e8fb: SIGTERM first let `git worktree add` run its own cleanup, which deleted the
    incomplete tree and a file someone wrote into it. The group gets SIGKILL directly; the file survives."""
    pidfile = tmp_path / "smudge.pids"
    slow_checkout(repo, pidfile)
    target = repo / ".worktrees" / "t_written"
    seen = {}

    def write_when_hung():
        # readiness marker: the smudge filter has started, i.e. the add is inside its checkout with the tree locked
        if _wait_for(lambda: pidfile.exists() and pidfile.read_text(encoding="utf-8-sig").strip(), 20.0):
            (target / "someones-note.txt").write_text("mine\n", encoding="utf-8")
            seen["written"] = True

    import threading
    th = threading.Thread(target=write_when_hung)
    th.start()
    with pytest.raises(RuntimeError, match="timed out"):
        kbw._ensure_git_worktree(repo, target, "wt/t_written")
    th.join()
    assert seen.get("written"), "the add never reached its checkout"
    assert (target / "someones-note.txt").read_text(encoding="utf-8-sig") == "mine\n"


def test_an_exceptional_exit_while_waiting_kills_the_group_and_reraises(tmp_path, monkeypatch):
    """Prism #265 P2 c-038d5f46d9069eb3: KeyboardInterrupt (any BaseException) mid-wait kills the group too."""
    pidfile = tmp_path / "child pid"
    calls = {"n": 0}
    real = subprocess.Popen.communicate

    def interrupted(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            assert _wait_for(lambda: pidfile.exists() and pidfile.read_text(encoding="utf-8-sig").strip(), 10.0)
            raise KeyboardInterrupt
        return real(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    inner = f"echo $$ > {shlex.quote(str(pidfile))}; exec sleep 60"
    script = f"sh -c {shlex.quote(inner)} & wait"                     # a grandchild in the group, not the leader
    with pytest.raises(KeyboardInterrupt):
        kwr.run_git_group(["sh", "-c", script], timeout=30)
    child = int(pidfile.read_text(encoding="utf-8-sig").split()[0])
    assert _wait_for(lambda: not alive(child), 5.0)


def test_a_second_spawn_on_a_half_built_tree_is_held_named_and_touches_nothing(repo, tmp_path):
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable
    target = repo / ".worktrees" / "t_y2"
    admin = _half_built(repo, target, "wt/t_y2", age_s=10_000)
    files = sorted(p.name for p in target.iterdir())
    with pytest.raises(WorkspaceUnavailable, match=kwr.HALF_BUILT_HELD):
        kbw._ensure_git_worktree(repo, target, "wt/t_y2")
    assert sorted(p.name for p in target.iterdir()) == files
    assert admin.is_dir() and kwr.lock_reason(admin) == kwr.INIT_LOCK and not (admin / "index").exists()
    assert git(repo, "branch", "--list", "wt/t_y2") != ""


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


def test_half_built_names_only_an_initializing_tree_without_an_index(repo):
    target = repo / ".worktrees" / "t_keep"
    _half_built(repo, target, "wt/t_keep", reason="operator: keep this", age_s=10_000)
    assert kwr.half_built(repo, target) is None                       # another lock reason: not this module's
    hb = repo / ".worktrees" / "t_hb"
    _half_built(repo, hb, "wt/t_hb", age_s=10_000)
    got = kwr.half_built(repo, hb)
    assert got is not None and got["branch"] == "wt/t_hb" and got["age_s"] >= 9_000



def _snapshot_repo(repo) -> tuple:
    """Everything a probe could change: registrations + their lock files, refs, the tree files."""
    wts = git(repo, "worktree", "list", "--porcelain")
    refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    files = sorted(str(p.relative_to(repo)) for p in (repo / ".worktrees").rglob("*")) \
        if (repo / ".worktrees").exists() else []
    return wts, refs, files



# --- ruling:t_f609a5a0-no-automatic-deletion: nothing in the spawn, assess() or this module deletes ---------------


_DESTRUCTIVE = ("rmtree", "rmdir", "os.remove", ".unlink(", '"-d"', '"-D"', '"prune"', '"remove"', '"unlock"')


def _source_of(func) -> str:
    import inspect
    import textwrap
    return textwrap.dedent(inspect.getsource(func))


def _calls_in(src: str) -> set:
    import ast
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return names


def test_no_code_path_in_the_spawn_assess_or_the_residue_module_removes_anything():
    """Static: the add path, assess() + its half-built probe and the residue module carry no worktree remove /
    prune / unlock, rmtree / rmdir / unlink, update-ref -d or branch -D, and the residue functions the spawn and
    assess() reach are only the read-only ones (ruling:t_f609a5a0-spawn-fix-only)."""
    from hermes_cli import kanban_worktree_port as kwt
    srcs = {
        "kanban_worktree_residue.py": Path(kwr.__file__).read_text(encoding="utf-8-sig"),
        "_ensure_git_worktree": _source_of(kbw._ensure_git_worktree),
        "_hold_half_built": _source_of(kbw._hold_half_built),
        "_half_built_held": _source_of(kwt._half_built_held),
    }
    for name, src in srcs.items():
        for bad in _DESTRUCTIVE:
            assert bad not in src, f"{name} contains {bad!r}"
    for gone in ("remove_failed_add", "clear_half_built", "drain_half_built", "_drop_tree", "_delete_branch_at",
                 "holds_work", "differs_from_head", "detached_unreachable", "release_stale_add_lock"):
        assert not hasattr(kwr, gone), gone
    allowed = {"half_built", "run_git_group", "WorktreeAddTimeout", "HALF_BUILT_HELD",
               "ADD_TIMEOUT_S"}
    for fn in (kbw._ensure_git_worktree, kbw._hold_half_built, kwt._half_built_held, kwt.assess):
        src = _source_of(fn)
        used = set(re.findall(r"_kwr\.(\w+)", src))
        assert used <= allowed, (fn.__name__, used - allowed)
    assert "_half_built_held" in _calls_in(_source_of(kwt.assess))


def test_assess_leaves_a_half_built_tree_byte_for_byte(repo):
    """Prism #265 P2 c-30fd064da15bbe38: the placement probe has no destructive side effect in any mode."""
    from hermes_cli import kanban_worktree_port as kwt
    target = repo / ".worktrees" / "t_ass"
    admin = _half_built(repo, target, "wt/t_ass", age_s=10_000)
    (target / "late.txt").write_text("someone's\n", encoding="utf-8")
    before = _snapshot_repo(repo)
    for _ in range(2):
        assert kwt._half_built_held(repo, target, "wt/t_ass") == kwt.WHY_HALF_BUILT
    assert _snapshot_repo(repo) == before and kwr.lock_reason(admin) == kwr.INIT_LOCK


# --- review r5 (Prism #265 @9cdae381) ---------------------------------------------------------------------------


def test_resolve_holds_a_half_built_requested_checkout_instead_of_returning_it(repo):
    """Prism #265 P1 c-53aef5301a38d84d: the existing-linked-checkout early return in _resolve_worktree_workspace
    (immediate-spawn and review lanes) holds a half-built tree like _ensure_git_worktree does, and touches nothing."""
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable
    target = repo / ".worktrees" / "t_early"
    admin = _half_built(repo, target, "wt/t_early", age_s=10_000)
    before = _snapshot_repo(repo)

    class _Task:
        id = "t_early"
        workspace_kind = "worktree"
        workspace_path = str(target)
        branch_name = "wt/t_early"

    with pytest.raises(WorkspaceUnavailable, match=kwr.HALF_BUILT_HELD):
        kbw._resolve_worktree_workspace(_Task())
    assert _snapshot_repo(repo) == before and kwr.lock_reason(admin) == kwr.INIT_LOCK

    git(repo, "worktree", "add", "-q", "-b", "wt/t_fine", str(repo / ".worktrees" / "t_fine"), "HEAD")
    _Task.id, _Task.workspace_path, _Task.branch_name = "t_fine", str(repo / ".worktrees" / "t_fine"), "wt/t_fine"
    got, branch = kbw._resolve_worktree_workspace(_Task())               # a finished checkout is still reused
    assert branch == "wt/t_fine" and Path(got) == (repo / ".worktrees" / "t_fine").resolve()


# --- review r6 (Prism #265 @6496c8e4) ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_lock_is_held_never_read_as_a_finished_checkout(repo):
    """Prism #265 P1 c-bf64c62a812cc1fc: lock_reason() kept "no lock" and "lock unreadable" apart; an unreadable
    lock fails closed in both callers (_hold_half_built, _half_built_held), even when the tree has an index."""
    from hermes_cli import kanban_worktree_port as kwt
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable
    target = repo / ".worktrees" / "t_unread"
    git(repo, "worktree", "add", "-q", "-b", "wt/t_unread", str(target), "HEAD")   # a finished tree: has an index
    git(repo, "worktree", "lock", "--reason", kwr.INIT_LOCK, str(target))
    admin = Path(git(target, "rev-parse", "--absolute-git-dir"))
    assert kwr.lock_reason(admin / "nope") is None                                # no lock file: not locked
    os.chmod(admin / "locked", 0)
    try:
        assert kwr.lock_reason(admin) is kwr.LOCK_UNREADABLE
        hb = kwr.half_built(repo, target)
        assert hb is not None and hb["lock"] == "unreadable"
        def snap():  # `git worktree list` itself refuses an unreadable lock: refs + tree files + admin dir
            return (git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
                    sorted(str(p.relative_to(repo)) for p in (repo / ".worktrees").rglob("*")),
                    sorted(p.name for p in admin.iterdir()))
        before = snap()
        with pytest.raises(WorkspaceUnavailable, match=kwr.HALF_BUILT_HELD):
            kbw._ensure_git_worktree(repo, target, "wt/t_unread")

        class _Task:
            id = "t_unread"
            workspace_kind = "worktree"
            workspace_path = str(target)
            branch_name = "wt/t_unread"

        with pytest.raises(WorkspaceUnavailable, match="cannot be read"):
            kbw._resolve_worktree_workspace(_Task())
        assert kwt._half_built_held(repo, target, "wt/t_unread") == kwt.WHY_HALF_BUILT
        assert snap() == before
    finally:
        os.chmod(admin / "locked", 0o644)
    assert kwr.lock_reason(admin) == kwr.INIT_LOCK and kwr.half_built(repo, target) is None   # readable again


def test_alive_treats_a_zombie_as_terminated(tmp_path):
    """Prism #265 P2 c-658240d2a3610b9e: an unreaped child is dead for the group-kill assertions."""
    p = subprocess.Popen(["sh", "-c", "exit 0"])
    assert _wait_for(lambda: not alive(p.pid), 10.0)                  # zombie until wait(): already not alive
    p.wait()
    assert not alive(p.pid)


# --- review r7 (prism-direct #265 @7acad872): could-not-assess holds; the hold runs only for a tree this card uses -


def _task(tid, path, branch):
    return type("_Task", (), {"id": tid, "workspace_kind": "worktree", "workspace_path": str(path),
                              "branch_name": branch})()


def test_a_failed_common_dir_probe_holds_a_half_built_tree_in_both_callers(repo, monkeypatch):
    """P1 residue.py:173: a common-dir probe that fails or times out is "could not assess", never "not half-built";
    _hold_half_built (spawn and the resolve early return) and _half_built_held (placement) both hold."""
    from hermes_cli import kanban_worktree_port as kwt
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable
    target = repo / ".worktrees" / "t_probe"
    admin = _half_built(repo, target, "wt/t_probe", age_s=10_000)
    fine = repo / ".worktrees" / "t_probe_fine"
    git(repo, "worktree", "add", "-q", "-b", "wt/t_probe_fine", str(fine), "HEAD")
    before = _snapshot_repo(repo)
    monkeypatch.setattr(kwr, "common_dir", lambda _repo: None)          # the probe failed / timed out
    hb = kwr.half_built(repo, target)
    assert hb is not None and hb["lock"] == kwr.UNASSESSED and "common-dir" in hb["why"]
    with pytest.raises(WorkspaceUnavailable, match="could not be assessed"):
        kbw._ensure_git_worktree(repo, target, "wt/t_probe")
    with pytest.raises(WorkspaceUnavailable, match="could not be assessed"):
        kbw._resolve_worktree_workspace(_task("t_probe", target, "wt/t_probe"))
    assert kwt._half_built_held(repo, target, "wt/t_probe") == kwt.WHY_HALF_BUILT
    assert kwr.half_built(repo, fine) is None                            # a finished tree never needs the probe
    got, _ = kbw._resolve_worktree_workspace(_task("t_probe_fine", fine, "wt/t_probe_fine"))
    assert Path(got) == fine.resolve()
    monkeypatch.undo()
    assert _snapshot_repo(repo) == before and kwr.lock_reason(admin) == kwr.INIT_LOCK


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root lists a search-only dir")
def test_an_unlistable_worktree_registry_holds_a_half_built_tree(repo):
    """P1 residue.py:126: a search-only <common>/worktrees resolves the known checkout but cannot be listed; that is
    "could not enumerate", never an empty registry, so the half-built tree is held."""
    from hermes_cli import kanban_worktree_port as kwt
    target = repo / ".worktrees" / "t_list"
    _half_built(repo, target, "wt/t_list", age_s=10_000)
    fine = repo / ".worktrees" / "t_list_fine"
    git(repo, "worktree", "add", "-q", "-b", "wt/t_list_fine", str(fine), "HEAD")
    reg = repo / ".git" / "worktrees"
    os.chmod(reg, 0o111)
    try:
        assert kwr.admin_dirs_for(repo / ".git", target) is None
        hb = kwr.half_built(repo, target)
        assert hb is not None and hb["lock"] == kwr.UNASSESSED and "registry" in hb["why"]
        assert kwt._half_built_held(repo, target, "wt/t_list") == kwt.WHY_HALF_BUILT
        assert kwr.half_built(repo, fine) is None                        # finished: confirmed by its own lock read
    finally:
        os.chmod(reg, 0o755)
    assert kwr.half_built(repo, target)["lock"] == kwr.INIT_LOCK       # listable again: the real state


def test_a_plain_directory_is_not_held_when_the_probe_fails(repo, monkeypatch):
    """Unknown holds only a tree that claims to be a linked checkout; a plain dir stays with its own guards."""
    plain = repo / ".worktrees" / "t_plain"
    plain.mkdir(parents=True)
    (plain / "x").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(kwr, "common_dir", lambda _repo: None)
    assert kwr.half_built(repo, plain) is None


def test_decompose_siblings_of_a_half_built_root_fall_back_to_their_own_trees(repo):
    """P2 kanban_db_workspace.py:1012: children inherit the root card's workspace_path. When that is the root's
    half-built tree, a child on its own branch falls back to .worktrees/<child> as before; only the root (whose
    branch the tree carries) is held. The root tree is left as it is."""
    from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable
    root = repo / ".worktrees" / "t_root"
    admin = _half_built(repo, root, "wt/t_root", age_s=10_000)
    root_files = sorted(p.name for p in root.iterdir())
    for child in ("t_c1", "t_c2"):
        got, branch = kbw._resolve_worktree_workspace(_task(child, root, f"wt/{child}"))
        assert Path(got) == (repo / ".worktrees" / child).resolve() and branch == f"wt/{child}"
        assert (Path(got) / "f0.slow").is_file()
    with pytest.raises(WorkspaceUnavailable, match=kwr.HALF_BUILT_HELD):
        kbw._resolve_worktree_workspace(_task("t_root", root, "wt/t_root"))
    assert sorted(p.name for p in root.iterdir()) == root_files
    assert kwr.lock_reason(admin) == kwr.INIT_LOCK and not (admin / "index").exists()
