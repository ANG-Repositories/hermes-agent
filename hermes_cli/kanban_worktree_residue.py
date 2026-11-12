"""A timed-out ``git worktree add`` is killed whole and named, never cleaned up by deletion (t_f609a5a0).

2026-11-11: card t_33172156's spawn ran ``git worktree add --no-track -b wt/<id> <repo>/.worktrees/<id> origin/main``
and the 60 s ``subprocess.run`` timeout killed only git's parent. Its checkout child lived on: in 150 of the 189
``initializing``-locked trees on the Studio it finished and wrote the index, and nothing dropped the lock; in the
rest it died too and left a half-built tree (no index). Placement read the card as ``own_tree_not_a_checkout`` and
parked it silently for 2 h 20 min.

This module:

* runs the add in its own process group and SIGKILLs the whole group on timeout or any exceptional exit
  (:func:`run_git_group`), so no git child outlives the spawn and git's own SIGTERM cleanup never runs;
* names a card's own half-built tree (:func:`half_built`, read-only) so placement can say what holds the card.

It deletes, unlocks and writes nothing: no tree, registration, lock, admin dir or branch (operator rulings
``ruling:t_f609a5a0-no-automatic-deletion``, ``ruling:t_f609a5a0-spawn-fix-only``). A tree a killed add left
stays as git left it, named and paged once; an operator inspects it before removing it.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional, Sequence

INIT_LOCK = "initializing"
UNASSESSED = "unassessed"  # half_built() could not finish its read: held, never reused
HALF_BUILT_HELD = "half_built_worktree_held"
# Studio home tree 2026-11-11 (30,578 files, 1,150 registrations, load1 36): two timed adds took 32.0 s and 106.1 s,
# and 83 of 6,540 spawns in 7 days failed at the old 60 s. 300 s is ~3x the slowest measured add; a run past it is
# killed whole; what it left is named, so the longer wait costs a slower failure, not a silent park.
ADD_TIMEOUT_S = 300


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


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


class _Unreadable:
    """A lock file that exists but could not be read: never the same as "not locked", never equal to a reason."""

    def __repr__(self) -> str:
        return "LOCK_UNREADABLE"


LOCK_UNREADABLE = _Unreadable()


def admin_dirs_for(common: Path, target: Path) -> Optional[list]:
    """Admin dirs under ``<common>/worktrees`` whose ``gitdir`` names ``<target>/.git`` (resolved); ``[]`` when
    none does or there is no ``worktrees`` dir; None when the registry exists but cannot be listed (Prism #265 P1:
    a search-only ``worktrees`` dir is \"could not enumerate\", never \"not registered\")."""
    want = os.path.realpath(os.path.join(str(target), ".git"))
    root = common / "worktrees"
    out = []
    try:
        entries = sorted(root.iterdir())
    except FileNotFoundError:
        return out
    except OSError:
        return None
    for admin in entries:
        pointed = _read(admin / "gitdir")
        if pointed is None:
            continue
        if os.path.realpath(os.path.join(str(admin), pointed.strip())) == want:
            out.append(admin)
    return out


def lock_reason(admin: Path) -> "Optional[str] | _Unreadable":
    """The admin dir's lock reason (``""`` = locked with no reason); None = not locked (no lock file);
    :data:`LOCK_UNREADABLE` = a lock that exists but could not be read (permission, I/O error). Prism #265 P1
    c-bf64c62a812cc1fc: an unreadable lock is never reported as "not locked"."""
    try:
        return (admin / "locked").read_text(encoding="utf-8-sig", errors="replace").strip()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return LOCK_UNREADABLE


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


def _is_gitfile(target: Path) -> bool:
    """``<target>/.git`` is a ``gitdir:`` file: the tree claims to be a linked checkout a caller might reuse."""
    text = _read(target / ".git")
    return bool(text) and text.strip().startswith("gitdir:")


def half_built(repo_root: Path, target: Path, now: Optional[float] = None) -> Optional[dict]:
    """``{admin, age_s, branch, lock}`` when ``target`` must be held, None only when it is CONFIRMED not to be a
    tree an add left behind (a finished tree, another lock reason, not registered here). Three states, unknown
    holds (Prism #265 @7acad872 P1s, same rule as the unreadable lock):

    * held: registered here, locked ``initializing`` with no index (``lock`` = ``"initializing"``), or locked with
      a lock file that cannot be read (``lock`` = ``"unreadable"``);
    * could not assess (``lock`` = ``"unassessed"``, ``why`` names the failed step): the tree is a linked checkout
      (``.git`` is a ``gitdir:`` file) not already confirmed finished, and the common-dir probe failed, the
      worktree registry could not be listed, or the lock could not be dated. Held like the others, never reused;
    * None: confirmed not half-built.
    """
    admin = _gitfile_admin(target)
    fast = lock_reason(admin) if admin is not None else None
    if admin is not None and fast is not LOCK_UNREADABLE and fast != INIT_LOCK:
        return None  # the common case on every tick: a finished tree, one file read, no git, no scan
    linked = admin is not None or _is_gitfile(target)

    def unassessed(why: str) -> Optional[dict]:
        if not linked:
            return None  # not a linked checkout: no caller reuses it as one; other guards own a plain dir
        return {"admin": str(admin) if admin is not None else "", "age_s": None, "branch": "",
                "lock": UNASSESSED, "why": why}

    common = common_dir(repo_root)
    if common is None:
        return unassessed("common-dir probe failed")
    admins = admin_dirs_for(common, target)
    if admins is None:
        return unassessed("worktree registry could not be listed")
    if len(admins) > 1:
        return unassessed(f"{len(admins)} registrations name this tree")
    if not admins:
        return None  # listed, and not registered here
    admin = admins[0]
    reason = lock_reason(admin)
    unreadable = reason is LOCK_UNREADABLE
    if not unreadable and (reason != INIT_LOCK or (admin / "index").exists()):
        return None
    try:
        age: Optional[int] = int((now if now is not None else time.time()) - (admin / "locked").stat().st_mtime)
    except OSError:
        if not unreadable:
            return unassessed("lock could not be dated")
        age = None
    head = (_read(admin / "HEAD") or "").strip()
    branch = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""
    return {"admin": str(admin), "age_s": age, "branch": branch, "lock": "unreadable" if unreadable else INIT_LOCK}
