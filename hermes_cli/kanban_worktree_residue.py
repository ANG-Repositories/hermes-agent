"""A timed-out ``git worktree add`` is killed whole and named, never cleaned up by deletion (t_f609a5a0).

2026-11-11: card t_33172156's spawn ran ``git worktree add --no-track -b wt/<id> <repo>/.worktrees/<id> origin/main``
and the 60 s ``subprocess.run`` timeout killed only git's parent. Its checkout child lived on: in 150 of the 189
``initializing``-locked trees on the Studio it finished and wrote the index, and nothing dropped the lock; in the
rest it died too and left a half-built tree (no index). Placement read the card as ``own_tree_not_a_checkout`` and
parked it silently for 2 h 20 min.

This module:

* runs the add in its own process group and SIGKILLs the whole group on timeout or any exceptional exit
  (:func:`run_git_group`), so no git child outlives the spawn and git's own SIGTERM cleanup never runs;
* names a card's own half-built tree (:func:`half_built`, read-only) so placement can say what holds the card;
* releases a stale ``initializing`` lock on a FINISHED tree (:func:`release_stale_add_lock`; it removes nothing).

It deletes nothing: no tree, registration, admin dir or branch (operator ruling
``ruling:t_f609a5a0-no-automatic-deletion``). A tree a killed add left is left as git left it, named and paged once;
an operator inspects it before removing it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional, Sequence

INIT_LOCK = "initializing"
HALF_BUILT_HELD = "half_built_worktree_held"
# Studio home tree 2026-11-11 (30,578 files, 1,150 registrations, load1 36): two timed adds took 32.0 s and 106.1 s,
# and 83 of 6,540 spawns in 7 days failed at the old 60 s. 300 s is ~3x the slowest measured add; a run past it is
# killed whole; what it left is named, so the longer wait costs a slower failure, not a silent park.
ADD_TIMEOUT_S = 300
_SLACK_S = 120


class WorktreeAddTimeout(RuntimeError):
    """``git worktree add`` ran past its timeout; its whole process group was killed."""


_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the process group ``proc`` leads, then reap ``proc``. POSIX; elsewhere ``proc.kill()``.

    SIGKILL, never SIGTERM first (Prism #265 P1 c-f6873eb7b7d1e8fb): on SIGTERM ``git worktree add`` runs its own
    cleanup and deletes the incomplete tree, files someone wrote into it included. A killed add's tree is left as
    git left it (ruling:t_f609a5a0-no-automatic-deletion)."""
    if not hasattr(os, "killpg"):
        proc.kill()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        os.killpg(proc.pid, _SIGKILL)  # windows-footgun: ok — guarded by hasattr(os, "killpg") above
    except OSError:
        pass  # ESRCH: the group is already gone
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def run_git_group(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    """``argv`` in its own session; on timeout every process of that group is killed, then
    :class:`WorktreeAddTimeout` is raised. Never raises on a non-zero exit."""
    proc = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=1.0)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        raise WorktreeAddTimeout(f"`{' '.join(argv[3:6])} ...` timed out after {timeout:g} seconds "
                                 f"(process group {proc.pid} killed)") from None
    except BaseException:
        # KeyboardInterrupt / SystemExit / anything else mid-wait (Prism #265 P2 c-038d5f46d9069eb3): no git child
        # outlives the caller either.
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(list(argv), proc.returncode, out, err)


def _git(repo: Path, *args: str, timeout: float = 30) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def common_dir(repo: Path) -> Optional[Path]:
    p = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if p is None or p.returncode != 0 or not p.stdout.strip():
        return None
    return Path(p.stdout.strip()).resolve()


def rev(repo: Path, ref: str) -> Optional[str]:
    """The commit ``ref`` names; ``""`` = no such ref (``rev-parse -q --verify`` exit 1, nothing on stderr);
    None = unknown (git failed, timed out or could not run). Prism #265 P1 c-5f2cb908f57d60a2."""
    p = _git(repo, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}")
    if p is None:
        return None
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip()
    if p.returncode == 1 and not (p.stderr or "").strip():
        return ""
    return None


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def admin_dirs_for(common: Path, target: Path) -> list:
    """Admin dirs under ``<common>/worktrees`` whose ``gitdir`` names ``<target>/.git`` (resolved)."""
    want = os.path.realpath(os.path.join(str(target), ".git"))
    root = common / "worktrees"
    out = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for admin in entries:
        pointed = _read(admin / "gitdir")
        if pointed is None:
            continue
        if os.path.realpath(os.path.join(str(admin), pointed.strip())) == want:
            out.append(admin)
    return out


def lock_reason(admin: Path) -> Optional[str]:
    """The admin dir's lock reason (``""`` = locked with no reason), None = not locked."""
    text = _read(admin / "locked")
    return None if text is None else text.strip()


def _gitfile_admin(target: Path) -> Optional[Path]:
    """The admin dir ``<target>/.git`` names, when that admin dir's ``gitdir`` names it back; else None."""
    text = _read(target / ".git")
    if not text or not text.strip().startswith("gitdir:"):
        return None
    admin = Path(os.path.realpath(os.path.join(str(target), text.strip()[len("gitdir:"):].strip())))
    back = _read(admin / "gitdir")
    if back is None or os.path.realpath(os.path.join(str(admin), back.strip())) != \
            os.path.realpath(os.path.join(str(target), ".git")):
        return None
    return admin


def half_built(repo_root: Path, target: Path, now: Optional[float] = None) -> Optional[dict]:
    """``{admin, age_s, branch}`` when ``target`` is a registered worktree of ``repo_root`` that an add left
    behind: locked ``initializing`` and no index. None otherwise (a finished tree, another lock, unreadable)."""
    admin = _gitfile_admin(target)
    if admin is not None and lock_reason(admin) != INIT_LOCK:
        return None  # the common case on every tick: a finished tree, one file read, no git, no scan
    common = common_dir(repo_root)
    if common is None:
        return None
    admins = admin_dirs_for(common, target)
    if len(admins) != 1:
        return None
    admin = admins[0]
    if lock_reason(admin) != INIT_LOCK or (admin / "index").exists():
        return None
    try:
        age = (now if now is not None else time.time()) - (admin / "locked").stat().st_mtime
    except OSError:
        return None
    head = (_read(admin / "HEAD") or "").strip()
    branch = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""
    return {"admin": str(admin), "age_s": int(age), "branch": branch}


def release_stale_add_lock(repo_root: Path, target: Path, now: Optional[float] = None) -> bool:
    """``git worktree unlock`` a FINISHED tree whose ``initializing`` lock outlived its add; True when released.

    Before this module the spawn's ``subprocess.run`` timeout killed only git's parent: the checkout child ran on,
    wrote the index, and the parent that would have dropped the lock was dead. The tree is a complete checkout
    (reused and worked in by the next spawn) that stays locked ``initializing`` forever, and card-close cleanup's
    no-force ``worktree remove`` (``-f`` too) refuses a locked tree, so the tree outlives its card. Released only
    when it has an index (the checkout finished) and the lock is older than an add can run: nothing is removed,
    and the caller's ``remove`` keeps git's own dirty check. Never raises."""
    try:
        admin = _gitfile_admin(target)
        if admin is None or lock_reason(admin) != INIT_LOCK or not (admin / "index").exists():
            return False
        if (now if now is not None else time.time()) - (admin / "locked").stat().st_mtime \
                <= ADD_TIMEOUT_S + _SLACK_S:
            return False
        u = _git(repo_root, "worktree", "unlock", str(target))
        return u is not None and u.returncode == 0
    except Exception:
        return False


def sha_has_own_commits(repo_root: Path, tip: Optional[str]) -> Optional[bool]:
    """True when ``tip`` is a commit no remote-tracking ref has; False for no tip (``""``); None = unknown (a
    ``None`` tip, or git failed). Prism #265 P1 c-3bed16991a4b3217: unknown is never reported as False."""
    if tip is None:
        return None
    if not tip:
        return False
    p = _git(repo_root, "for-each-ref", "--contains", tip, "--count=1", "--format=%(refname)", "refs/remotes/",
             timeout=60)
    if p is None or p.returncode != 0:
        return None
    return p.stdout.strip() == ""


def branch_has_own_commits(repo_root: Path, branch: str) -> Optional[bool]:
    """True when ``refs/heads/<branch>`` holds a commit no remote-tracking ref has; None = unknown."""
    return sha_has_own_commits(repo_root, rev(repo_root, f"refs/heads/{branch}"))
