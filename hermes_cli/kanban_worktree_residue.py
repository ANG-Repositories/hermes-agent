"""A timed-out ``git worktree add`` leaves nothing behind (t_f609a5a0).

2026-11-11: card t_33172156's spawn ran ``git worktree add --no-track -b wt/<id> <repo>/.worktrees/<id> origin/main``
and the 60 s ``subprocess.run`` timeout killed git mid-checkout. ``subprocess.run`` kills only the direct child (its
``git reset --hard`` child lives on), and nothing cleaned up: the tree stayed half checked out, without an index,
locked by git's own reason ``initializing``, with the new branch. Placement then read the card as
``own_tree_not_a_checkout`` and parked it silently for 2 h 20 min. The home tree held 183 such registrations.

This module is the one place that:

* runs the add in its own process group and kills the whole group on timeout (:func:`run_git_group`);
* removes the tree THIS spawn tried to create, and only that one (:func:`remove_failed_add`): ``worktree remove -f -f``
  of its own path, else the ONE admin dir whose ``gitdir`` names that path (never a bare ``worktree prune``), and the
  branch only when this spawn created it and it still points at the base it was created from (no commits of its own);
* names a leftover half-built tree of a card (:func:`half_built`) so placement can say what holds the card and clear
  it under the same ownership rule (:func:`clear_half_built`).

A registration locked for any reason other than ``initializing`` is someone's deliberate lock: never touched.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional, Sequence

INIT_LOCK = "initializing"
HALF_BUILT_HELD = "half_built_worktree_held"
ADD_TIMEOUT_S = 60
# A half-built tree younger than this may belong to an add that is still running (another dispatcher process):
# placement only names it. Two add timeouts plus slack.
HALF_BUILT_MIN_AGE_S = 600
_KILL_GRACE_S = 2.0


class WorktreeAddTimeout(RuntimeError):
    """``git worktree add`` ran past its timeout; its whole process group was killed."""


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the process group ``proc`` leads; reap ``proc``."""
    for sig, wait in ((signal.SIGTERM, _KILL_GRACE_S), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        except OSError:
            pass
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
        # The leader is gone; a child may still be alive in the group: SIGKILL the rest (ESRCH = none left).
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        break
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


def rev(repo: Path, ref: str) -> str:
    p = _git(repo, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}")
    return p.stdout.strip() if p is not None and p.returncode == 0 else ""


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


def remove_failed_add(repo_root: Path, target: Path, *, branch: str, created_branch: bool,
                      base_sha: str) -> dict:
    """Undo a failed or timed-out ``worktree add`` of ``target``. The caller proved ``target`` was absent or empty
    before the add, so whatever is there now is this add's. ``{"removed", "admin_dropped", "branch_deleted",
    "kept"}``; never raises."""
    report = {"removed": False, "admin_dropped": [], "branch_deleted": False, "kept": []}
    try:
        common = common_dir(repo_root)
        admins = admin_dirs_for(common, target) if common is not None else []
        foreign = [a for a in admins if lock_reason(a) not in (None, INIT_LOCK)]
        if foreign:
            report["kept"] = [f"{a}: locked {lock_reason(a)!r}" for a in foreign]
            return report
        if admins:
            rm = _git(repo_root, "worktree", "remove", "-f", "-f", str(target), timeout=120)
            report["removed"] = rm is not None and rm.returncode == 0
            if not report["removed"]:
                # A tree killed before its .git file was written fails `remove` with "validation failed":
                # drop the ONE admin dir that names this path, never a repo-wide prune.
                for admin in admin_dirs_for(common, target):
                    shutil.rmtree(admin, ignore_errors=True)
                    report["admin_dropped"].append(str(admin))
        if target.exists() and common is not None and not admin_dirs_for(common, target):
            shutil.rmtree(target, ignore_errors=True)
            report["removed"] = report["removed"] or not target.exists()
        if created_branch and base_sha and branch:
            # Only when it still points at the base this spawn created it from: then it has no commits of its own.
            # update-ref -d with the old value is atomic, and refuses a branch checked out in a live worktree.
            if rev(repo_root, f"refs/heads/{branch}") == base_sha and not _checked_out(common, branch):
                d = _git(repo_root, "update-ref", "-d", f"refs/heads/{branch}", base_sha)
                report["branch_deleted"] = d is not None and d.returncode == 0
    except Exception as exc:  # cleanup is best effort: the add's own error is what the caller raises
        report["kept"].append(f"cleanup error: {exc}")
    return report


def _checked_out(common: Optional[Path], branch: str) -> bool:
    """``branch`` is HEAD of some registered worktree (main tree included)."""
    if common is None:
        return True
    want = f"ref: refs/heads/{branch}"
    heads = [common / "HEAD"] + [a / "HEAD" for a in (sorted((common / "worktrees").glob("*"))
                                                      if (common / "worktrees").is_dir() else [])]
    return any((_read(h) or "").strip() == want for h in heads)


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


def branch_has_own_commits(repo_root: Path, branch: str) -> Optional[bool]:
    """True when ``refs/heads/<branch>`` holds a commit no remote-tracking ref has; None = unknown."""
    tip = rev(repo_root, f"refs/heads/{branch}")
    if not tip:
        return False
    p = _git(repo_root, "for-each-ref", "--contains", tip, "--count=1", "--format=%(refname)", "refs/remotes/",
             timeout=60)
    if p is None or p.returncode != 0:
        return None
    return p.stdout.strip() == ""


def clear_half_built(repo_root: Path, target: Path, *, branch: str, min_age_s: int = HALF_BUILT_MIN_AGE_S,
                     now: Optional[float] = None) -> dict:
    """Clear a card's own leftover half-built tree (placement, t_f609a5a0). ``{"cleared", "why", ...}``.

    Ownership: the caller passes the card's canonical ``<repo>/.worktrees/<id>`` and its branch. Refused when the
    tree is not half-built (:func:`half_built`), younger than ``min_age_s`` (an add may still be running), on another
    branch, or the branch has commits no remote has (then the tree is removed and the branch kept)."""
    hb = half_built(repo_root, target, now=now)
    if hb is None:
        return {"cleared": False, "why": "not a half-built tree"}
    if hb["age_s"] < min_age_s:
        return {"cleared": False, "why": f"initializing lock is {hb['age_s']}s old (< {min_age_s}s)", **hb}
    if hb["branch"] and hb["branch"] != branch:
        return {"cleared": False, "why": f"tree is on {hb['branch']}, not {branch}", **hb}
    own = branch_has_own_commits(repo_root, branch)
    tip = rev(repo_root, f"refs/heads/{branch}")
    rep = remove_failed_add(repo_root, target, branch=branch, created_branch=own is False and bool(tip),
                            base_sha=tip)
    cleared = not target.exists() and not admin_dirs_for(common_dir(repo_root) or Path("/nonexistent"), target)
    return {"cleared": cleared, "why": "cleared" if cleared else f"cleanup incomplete: {rep}",
            "branch_kept": not rep["branch_deleted"], **hb, **rep}
