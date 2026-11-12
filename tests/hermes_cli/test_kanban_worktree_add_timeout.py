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
    try:
        os.kill(pid, 0)  # windows-footgun: ok — the test drives POSIX process groups
    except ProcessLookupError:
        return False
    return True


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


def _audit(*args):
    import sys
    script = Path(__file__).resolve().parents[2] / "scripts" / "kanban_worktree_residue_audit.py"
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, encoding="utf-8")


def _snapshot_repo(repo) -> tuple:
    """Everything the audit could change: registrations + their lock files, refs, the tree files."""
    wts = git(repo, "worktree", "list", "--porcelain")
    refs = git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    files = sorted(str(p.relative_to(repo)) for p in (repo / ".worktrees").rglob("*")) \
        if (repo / ".worktrees").exists() else []
    return wts, refs, files


def test_audit_is_read_only_and_check_keys_on_no_index(repo, tmp_path):
    import sqlite3
    db = tmp_path / "kanban.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, branch_name TEXT)")
    c.execute("CREATE TABLE task_events (task_id TEXT, kind TEXT)")
    c.execute("INSERT INTO tasks VALUES ('t_0000d0e0', 'done', NULL)")
    c.execute("INSERT INTO task_events VALUES ('t_0000d0e0', 'spawned')")
    c.commit()
    c.close()
    wt = repo / ".worktrees"
    _half_built(repo, wt / "t_0000d0e0", "wt/t_0000d0e0", age_s=10_000)
    _half_built(repo, wt / "fold-x", "fold-x", age_s=10_000)               # another tool's tree
    _half_built(repo, wt / "t_0000feed", "wt/t_0000feed", reason="operator", age_s=10_000)
    finished = wt / "t_0000f1f1"                                          # a finished tree still carrying the lock
    git(repo, "worktree", "add", "-q", "-b", "wt/t_0000f1f1", str(finished), "HEAD")
    git(repo, "worktree", "lock", "--reason", kwr.INIT_LOCK, str(finished))
    _age_tree(finished, Path(git(finished, "rev-parse", "--absolute-git-dir")), 10_000)
    before = _snapshot_repo(repo)

    chk = _audit("--repo", str(repo), "--check")
    assert chk.returncode == 1 and chk.stdout.count("finding: half-built worktree") == 2, chk.stdout
    assert "t_0000f1f1" not in chk.stdout                                # keyed on "no index", not on the lock
    assert "worktree remove" not in chk.stdout and "reset" not in chk.stdout
    tab = _audit("--repo", str(repo), "--db", str(db))
    assert tab.returncode == 0, tab.stderr
    assert "t_0000d0e0\thalf-built" in tab.stdout and "finished-still-locked" in tab.stdout
    assert "\tdone\tTrue\t" in tab.stdout
    js = _audit("--repo", str(repo), "--db", str(db), "--json")
    assert js.returncode == 0 and '"state": "half-built"' in js.stdout
    for gone in ("--drain", "--execute"):
        assert _audit("--repo", str(repo), gone).returncode == 2         # argparse: no such option any more
    assert _snapshot_repo(repo) == before                                # nothing changed


def test_audit_check_is_silent_when_clean(repo):
    r = _audit("--repo", str(repo), "--check")
    assert r.returncode == 0 and r.stdout == ""


def test_audit_check_does_no_content_hashing(repo, monkeypatch):
    """Prism #265 P2 c-7d3a831f652c416d: the sweep reads admin-dir files only, never per-row git."""
    import importlib.util
    target = repo / ".worktrees" / "t_cheap"
    _half_built(repo, target, "wt/t_cheap", age_s=10_000)
    script = Path(__file__).resolve().parents[2] / "scripts" / "kanban_worktree_residue_audit.py"
    spec = importlib.util.spec_from_file_location("kwr_audit_cheap", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def boom(*a, **k):
        raise AssertionError("per-row git call in --check")
    monkeypatch.setattr(mod.kwr, "branch_has_own_commits", boom)
    monkeypatch.setattr(mod.kwr, "rev", boom)
    assert mod.main(["--repo", str(repo), "--check"]) == 1


# --- ruling:t_f609a5a0-no-automatic-deletion: nothing in the spawn, assess() or this module deletes ---------------


_DESTRUCTIVE = ("rmtree", "rmdir", "os.remove", ".unlink(", '"-d"', '"-D"', '"prune"', '"remove"')


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
    """Static: the add path, assess() + its half-built probe, the residue module and the audit script carry no
    worktree remove / prune, rmtree / rmdir / unlink, update-ref -d or branch -D, and the residue functions the
    spawn and assess() reach are only the read-only ones (plus the unlock of a finished tree)."""
    from hermes_cli import kanban_worktree_port as kwt
    srcs = {
        "kanban_worktree_residue.py": Path(kwr.__file__).read_text(encoding="utf-8-sig"),
        "kanban_worktree_residue_audit.py": (Path(__file__).resolve().parents[2] / "scripts" /
                                             "kanban_worktree_residue_audit.py").read_text(encoding="utf-8-sig"),
        "_ensure_git_worktree": _source_of(kbw._ensure_git_worktree),
        "_hold_half_built": _source_of(kbw._hold_half_built),
        "_half_built_held": _source_of(kwt._half_built_held),
    }
    for name, src in srcs.items():
        for bad in _DESTRUCTIVE:
            assert bad not in src, f"{name} contains {bad!r}"
    for gone in ("remove_failed_add", "clear_half_built", "drain_half_built", "_drop_tree", "_delete_branch_at",
                 "holds_work", "differs_from_head", "detached_unreachable"):
        assert not hasattr(kwr, gone), gone
    allowed = {"half_built", "release_stale_add_lock", "run_git_group", "WorktreeAddTimeout", "HALF_BUILT_HELD",
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


# --- review r2 (operator Studio audit 2026-11-11: 150 of 189 `initializing` locks sit on FINISHED trees) ---------


def test_a_parent_only_kill_finishes_the_checkout_but_leaves_the_lock(repo, tmp_path):
    """The root cause of the 150: the old spawn's subprocess.run timeout killed only git's parent; the checkout
    child finished (index written) and nobody dropped the lock. The group kill closes that path."""
    import signal as _sig
    started = tmp_path / "smudge started"
    script = f"echo $$ >> {shlex.quote(str(started))}; sleep 1; cat"
    git(repo, "config", "filter.slow.smudge", f"sh -c {shlex.quote(script)}")
    target = repo / ".worktrees" / "t_parent"
    p = subprocess.Popen(["git", "-C", str(repo), "worktree", "add", "--no-track", "-b", "wt/t_parent", str(target),
                          "HEAD"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # readiness marker, not a fixed sleep: the checkout child is running its first smudge
    assert _wait_for(lambda: started.exists() and started.read_text(encoding="utf-8-sig").strip(), 30.0)
    p.send_signal(_sig.SIGKILL)
    p.wait()
    admin = repo / ".git" / "worktrees" / "t_parent"
    deadline = time.monotonic() + 30
    while not (admin / "index").exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    time.sleep(0.5)
    assert (admin / "index").exists() and kwr.lock_reason(admin) == kwr.INIT_LOCK
    assert git(target, "status", "--porcelain") == ""                # a complete, clean checkout, still locked
    assert kwr.half_built(repo, target) is None                      # not residue: never cleared as one


def _finished_locked(repo, name, age_s):
    target = repo / ".worktrees" / name
    git(repo, "worktree", "add", "-q", "-b", f"wt/{name}", str(target), "HEAD")
    git(repo, "worktree", "lock", "--reason", kwr.INIT_LOCK, str(target))
    admin = Path(git(target, "rev-parse", "--absolute-git-dir"))
    if age_s:
        old = time.time() - age_s
        os.utime(admin / "locked", (old, old))
    return target, admin


def test_card_close_remove_refuses_a_finished_tree_locked_initializing_without_the_release(repo):
    """Measured: a no-force and a single -f `worktree remove` both refuse; -f -f is the only override."""
    target, _ = _finished_locked(repo, "t_close0", 10_000)
    for args in (("remove",), ("remove", "-f")):
        r = subprocess.run(["git", "-C", str(repo), "worktree", *args, str(target)], **_TXT)
        assert r.returncode != 0 and "locked" in r.stderr
    assert target.is_dir()


def test_release_stale_add_lock_unlocks_only_a_finished_tree_older_than_an_add(repo):
    target, admin = _finished_locked(repo, "t_rel", 10_000)
    assert kwr.release_stale_add_lock(repo, target) is True
    assert kwr.lock_reason(admin) is None
    r = subprocess.run(["git", "-C", str(repo), "worktree", "remove", str(target)], **_TXT)
    assert r.returncode == 0                                         # card close's no-force remove now succeeds

    young, yadmin = _finished_locked(repo, "t_young2", 0)            # an add may still be finishing
    assert kwr.release_stale_add_lock(repo, young) is False and kwr.lock_reason(yadmin) == kwr.INIT_LOCK
    hb = repo / ".worktrees" / "t_hb2"
    hadmin = _half_built(repo, hb, "wt/t_hb2", age_s=10_000)         # no index: residue, not a finished tree
    assert kwr.release_stale_add_lock(repo, hb) is False and kwr.lock_reason(hadmin) == kwr.INIT_LOCK
    other, oadmin = _finished_locked(repo, "t_op", 0)
    git(repo, "worktree", "unlock", str(other))
    git(repo, "worktree", "lock", "--reason", "operator: keep", str(other))
    os.utime(oadmin / "locked", (time.time() - 10_000,) * 2)
    assert kwr.release_stale_add_lock(repo, other) is False and kwr.lock_reason(oadmin) == "operator: keep"


def test_release_keeps_the_dirty_check_a_dirty_finished_tree_is_not_removed(repo):
    target, _ = _finished_locked(repo, "t_dirty", 10_000)
    (target / "f0.slow").write_text("edited\n", encoding="utf-8")
    assert kwr.release_stale_add_lock(repo, target) is True
    r = subprocess.run(["git", "-C", str(repo), "worktree", "remove", str(target)], **_TXT)
    assert r.returncode != 0 and (target / "f0.slow").read_text(encoding="utf-8-sig") == "edited\n"


def test_spawn_reusing_a_finished_locked_tree_releases_the_stale_lock(repo):
    target, admin = _finished_locked(repo, "t_reuse", 10_000)
    kbw._ensure_git_worktree(repo, target, "wt/t_reuse")
    assert kwr.lock_reason(admin) is None and git(target, "branch", "--show-current") == "wt/t_reuse"




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


def test_a_failed_rev_parse_is_unknown_not_no_own_commits(repo, monkeypatch):
    """Prism #265 P1 c-5f2cb908f57d60a2 / c-3bed16991a4b3217: git failing or timing out is None end to end."""
    git(repo, "branch", "wt/t_rev")
    assert kwr.rev(repo, "refs/heads/wt/t_rev") == git(repo, "rev-parse", "HEAD")
    assert kwr.rev(repo, "refs/heads/nope") == ""                       # absent ref: known
    assert kwr.branch_has_own_commits(repo, "nope") is False
    assert kwr.sha_has_own_commits(repo, None) is None

    monkeypatch.setattr(kwr, "_git", lambda *a, **k: None)              # timed out / could not run
    assert kwr.rev(repo, "refs/heads/wt/t_rev") is None
    assert kwr.branch_has_own_commits(repo, "wt/t_rev") is None

    def broken(*a, **k):
        return subprocess.CompletedProcess(a, 128, "", "fatal: not a git repository")
    monkeypatch.setattr(kwr, "_git", broken)                            # git ran and failed
    assert kwr.rev(repo, "refs/heads/wt/t_rev") is None
    assert kwr.branch_has_own_commits(repo, "wt/t_rev") is None


def test_audit_row_keeps_unknown_own_commits_as_none(repo, monkeypatch):
    import importlib.util
    _half_built(repo, repo / ".worktrees" / "t_0000aaaa", "wt/t_0000aaaa", age_s=10_000)
    script = Path(__file__).resolve().parents[2] / "scripts" / "kanban_worktree_residue_audit.py"
    spec = importlib.util.spec_from_file_location("kwr_audit_unknown", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.kwr, "rev", lambda *a, **k: None)
    rows = mod.attribute(repo, None, time.time())
    assert len(rows) == 1 and rows[0]["branch_own_commits"] is None


@pytest.mark.parametrize("name", ["t_é x", 't_q"z', "t_bs\\z", "t_n\nl"])
def test_audit_check_finds_a_half_built_tree_whose_path_git_would_quote(repo, name):
    """Prism #265 P1 c-3b354bf2730246e2: parsed from `worktree list --porcelain -z`, so non-ASCII, quote, backslash
    and newline paths are found (state half-built, not unknown) and --check exits 1."""
    target = repo / ".worktrees" / name
    _half_built(repo, target, "wt/odd", age_s=10_000)
    chk = _audit("--repo", str(repo), "--check")
    assert chk.returncode == 1 and chk.stdout.count("finding: half-built worktree") == 1, chk.stdout
    js = _audit("--repo", str(repo), "--json")
    import json as _json
    rows = _json.loads(js.stdout)
    assert [r["state"] for r in rows] == ["half-built"] and Path(rows[0]["path"]).name == name
