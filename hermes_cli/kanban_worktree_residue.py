"""A timed-out ``git worktree add`` leaves nothing behind (t_f609a5a0).

2026-11-11: card t_33172156's spawn ran ``git worktree add --no-track -b wt/<id> <repo>/.worktrees/<id> origin/main``
and the 60 s ``subprocess.run`` timeout killed git mid-checkout. ``subprocess.run`` kills only the direct child (its
``git reset --hard`` child lives on), and nothing cleaned up: the tree stayed half checked out, without an index,
locked by git's own reason ``initializing``, with the new branch. Placement then read the card as
``own_tree_not_a_checkout`` and parked it silently for 2 h 20 min. The home tree held 183 such registrations.

This module is the one place that:

* runs the add in its own process group and kills the whole group on timeout (:func:`run_git_group`);
* removes what THIS spawn's add provably created, and only that (:func:`remove_failed_add`, against a
  :func:`snapshot` taken before the add): the one registration that appeared since, still locked ``initializing``
  with no index, via ``worktree remove -f -f`` else that ONE admin dir (never a bare ``worktree prune``); nothing on
  an "already exists" failure; the branch only when this spawn created it and it still points at its base;
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
# Studio home tree 2026-11-11 (30,578 files, 1,150 registrations, load1 36): two timed adds took 32.0 s and 106.1 s,
# and 83 of 6,540 spawns in 7 days failed at the old 60 s. 300 s is ~3x the slowest measured add; a run past it is
# killed whole and cleaned up, so the longer wait costs a slower failure, not a leftover.
ADD_TIMEOUT_S = 300
_SLACK_S = 120
# A half-built tree younger than this may belong to an add that is still running (another dispatcher process):
# placement only names it. Two add timeouts plus slack.
HALF_BUILT_MIN_AGE_S = 2 * ADD_TIMEOUT_S + _SLACK_S
_KILL_GRACE_S = 2.0


class WorktreeAddTimeout(RuntimeError):
    """``git worktree add`` ran past its timeout; its whole process group was killed."""


_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the process group ``proc`` leads; reap ``proc``. POSIX; elsewhere ``proc.kill()``."""
    if not hasattr(os, "killpg"):
        proc.kill()
        proc.wait(timeout=5.0)
        return
    for sig, wait in ((signal.SIGTERM, _KILL_GRACE_S), (_SIGKILL, 5.0)):
        try:
            os.killpg(proc.pid, sig)  # windows-footgun: ok — guarded by hasattr(os, "killpg") above
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
            os.killpg(proc.pid, _SIGKILL)  # windows-footgun: ok — guarded by hasattr(os, "killpg") above
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


def snapshot(repo_root: Path, target: Path) -> dict:
    """What sits at ``target`` BEFORE an add: ``{"absent", "admins"}``. :func:`remove_failed_add` removes only what
    appeared after it, so a tree another process made in the check-then-add gap is never this add's to delete."""
    common = common_dir(repo_root)
    return {"absent": not os.path.lexists(str(target)),
            "admins": sorted(str(a) for a in admin_dirs_for(common, target)) if common is not None else None}


def _init_residue(admin: Path) -> bool:
    """``admin`` is a registration a killed add left: locked ``initializing`` and no index (checkout unfinished)."""
    return lock_reason(admin) == INIT_LOCK and not (admin / "index").exists()


def _drop_tree(repo_root: Path, common: Path, target: Path, admin: Path, *, rmtree_path: bool, report: dict) -> None:
    """Remove the tree registered by ``admin`` (the ONLY admin dir naming ``target``; the caller proved it is
    add residue). ``worktree remove -f -f``, else drop that one admin dir (never a repo-wide prune); the path itself
    is ``rmtree``'d only when ``rmtree_path`` (the caller saw it absent before the add, or it is a proven half-built
    tree)."""
    rm = _git(repo_root, "worktree", "remove", "-f", "-f", str(target), timeout=120)
    report["removed"] = rm is not None and rm.returncode == 0
    if not report["removed"]:
        # A tree killed before its .git file was written fails `remove` with "validation failed".
        if admin.is_dir() and admin_dirs_for(common, target) == [admin]:
            shutil.rmtree(admin, ignore_errors=True)
            report["admin_dropped"].append(str(admin))
        if rmtree_path and target.exists() and not admin_dirs_for(common, target):
            shutil.rmtree(target, ignore_errors=True)
        report["removed"] = not target.exists() or (not rmtree_path and not admin.exists())


def _delete_branch_at(repo_root: Path, common: Optional[Path], branch: str, sha: str) -> bool:
    """Delete ``refs/heads/<branch>`` only while it still points at ``sha`` and no worktree has it checked out.
    ``update-ref -d <ref> <old>`` is atomic: a branch that moved meanwhile is kept."""
    if not (branch and sha) or rev(repo_root, f"refs/heads/{branch}") != sha or _checked_out(common, branch):
        return False
    d = _git(repo_root, "update-ref", "-d", f"refs/heads/{branch}", sha)
    return d is not None and d.returncode == 0


def remove_failed_add(repo_root: Path, target: Path, *, pre: dict, branch: str, created_branch: bool,
                      base_sha: str, timed_out: bool, already_exists: bool = False) -> dict:
    """Undo a failed or timed-out ``worktree add`` of ``target``, removing only what THIS add provably created.

    ``pre`` is :func:`snapshot` taken just before the add. Removed: the one registration naming ``target`` that did
    not exist in ``pre``, and only while it is add residue (locked ``initializing``, no index). The path is
    ``rmtree``'d only when ``pre`` saw it absent. Only a TIMED-OUT add leaves a tree: on any other failure git
    removes its own junk tree and registration before it exits, so a registration still there is someone else's and
    only the branch is considered. Kept: everything on an "already exists" failure, a tree with an index, any other lock, a registration that predates the add, two new registrations (a concurrent add: whose is
    which is unknown). The branch goes only when this add created it and it still sits at ``base_sha``.
    ``{"removed", "admin_dropped", "branch_deleted", "kept"}``; never raises."""
    report = {"removed": False, "admin_dropped": [], "branch_deleted": False, "kept": []}
    if already_exists:
        report["kept"].append("add refused 'already exists': the path or branch was not this add's")
        return report
    try:
        common = common_dir(repo_root)
        if common is None or pre.get("admins") is None:
            report["kept"].append("repo or pre-add snapshot unreadable")
            return report
        before = set(pre["admins"])
        admins = admin_dirs_for(common, target)
        old = [a for a in admins if str(a) in before]
        new = [a for a in admins if str(a) not in before]
        if old:
            report["kept"].append(f"registration predates this add: {[str(a) for a in old]}")
        elif len(new) > 1:
            report["kept"].append(f"{len(new)} new registrations name the path (a concurrent add): none removed")
        elif new and not timed_out:
            report["kept"].append(f"{new[0]}: git cleans its own tree on a non-timeout failure: not this add's")
        elif new and not _init_residue(new[0]):
            report["kept"].append(f"{new[0]}: not add residue (lock {lock_reason(new[0])!r}, "
                                  f"index {(new[0] / 'index').exists()})")
        elif new:
            _drop_tree(repo_root, common, target, new[0], rmtree_path=bool(pre.get("absent")), report=report)
        elif os.path.lexists(str(target)):
            # A kill between git's mkdir of the tree and its `gitdir` write leaves an EMPTY unregistered dir: only
            # that, and only at a path this call saw absent, is removed (rmdir refuses anything non-empty).
            if timed_out and pre.get("absent") and target.is_dir() and not target.is_symlink():
                try:
                    os.rmdir(target)
                    report["removed"] = True
                except OSError:
                    report["kept"].append(f"{target}: unregistered and not empty: not this add's")
            else:
                report["kept"].append(f"{target}: exists with no registration: not this add's")
        if created_branch and not old and len(new) <= 1:
            report["branch_deleted"] = _delete_branch_at(repo_root, common, branch, base_sha)
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


def written_after_add(target: Path, admin: Path, slack_s: float = _SLACK_S) -> Optional[str]:
    """A file under ``target`` modified after the add that made it could have been running, else None.

    The add writes its checkout between taking the ``initializing`` lock and its kill (at most its timeout), so a
    file newer than ``lock + ADD_TIMEOUT_S + slack`` was written by someone else into the dead tree: that tree is
    work, never residue. Unreadable = treated as work (returned)."""
    try:
        limit = (admin / "locked").stat().st_mtime + ADD_TIMEOUT_S + slack_s
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in files:
                if name == ".git" and root == str(target):
                    continue  # the gitfile is git's, not work
                p = os.path.join(root, name)
                if os.lstat(p).st_mtime > limit:
                    return p
    except OSError as exc:
        return f"unreadable: {exc}"
    return None


def _admin_head_sha(repo_root: Path, admin: Path) -> tuple:
    """``(sha, branch)`` the admin dir's HEAD names: a branch ref resolved, or the detached sha. ``("", "")`` =
    unreadable / unresolvable."""
    head = (_read(admin / "HEAD") or "").strip()
    if head.startswith("ref: refs/heads/"):
        b = head[len("ref: refs/heads/"):]
        return rev(repo_root, f"refs/heads/{b}"), b
    if head.startswith("ref: "):
        return rev(repo_root, head[len("ref: "):]), ""
    return (rev(repo_root, head) if head else ""), ""


def _blob_sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def differs_from_head(repo_root: Path, target: Path, admin: Path) -> Optional[str]:
    """A file under ``target`` that is NOT the content of the tree's HEAD commit, else None (Prism #265 P1 2).

    An early mtime does not prove git wrote a file; content does. Every file must be a path of HEAD's tree with
    the same type and the same blob id (``git hash-object`` with the path's filters, so an eol / smudge filter
    reads as the checkout wrote it); an untracked file, a changed file, or a type change is work. A half-built
    add has written a SUBSET of HEAD's files, so a missing file is not work. Unknown (HEAD unresolvable, git
    error) is treated as work (returned)."""
    sha, _ = _admin_head_sha(repo_root, admin)
    if not sha:
        return f"HEAD of {admin} unresolvable"
    lt = _git(repo_root, "ls-tree", "-r", "-z", "--full-tree", sha, timeout=300)
    if lt is None or lt.returncode != 0:
        return f"ls-tree {sha} failed"
    want = {}
    for ent in lt.stdout.split("\0"):
        if not ent:
            continue
        meta, _, path = ent.partition("\t")
        parts = meta.split()
        if len(parts) == 3:
            want[path] = (parts[0], parts[2])
    regular = []
    try:
        for root, dirs, files in os.walk(target):
            if root == str(target):
                dirs[:] = [d for d in dirs if d != ".git"]
            for name in list(files) + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
                if name == ".git" and root == str(target):
                    continue  # the gitfile is git's, not work
                full = os.path.join(root, name)
                rel = os.path.relpath(full, str(target)).replace(os.sep, "/")
                if rel not in want:
                    return f"untracked: {full}"
                mode, blob = want[rel]
                if os.path.islink(full):
                    if mode != "120000" or _blob_sha(os.fsencode(os.readlink(full))) != blob:
                        return f"differs from HEAD: {full}"
                elif mode not in ("100644", "100755") or not os.path.isfile(full):
                    return f"differs from HEAD (type): {full}"
                else:
                    regular.append((rel, blob, full))
    except OSError as exc:
        return f"unreadable: {exc}"
    if not regular:
        return None
    ho = subprocess.run(["git", "--git-dir", str(admin), "--work-tree", str(target), "hash-object", "--stdin-paths"],
                        cwd=str(target), input="\n".join(r for r, _, _ in regular) + "\n", capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=600, check=False) \
        if all("\n" not in r for r, _, _ in regular) else None
    if ho is None or ho.returncode != 0:
        return "hash-object failed"
    got = ho.stdout.split()
    if len(got) != len(regular):
        return "hash-object output short"
    for (rel, blob, full), h in zip(regular, got):
        if h != blob:
            return f"differs from HEAD: {full}"
    return None


def holds_work(repo_root: Path, target: Path, admin: Path) -> Optional[str]:
    """Why ``target`` is work, never residue, else None: a file written after the add died
    (:func:`written_after_add`, cheap, first) or a file that is not HEAD's content (:func:`differs_from_head`).
    The gate every destructive half-built cleanup passes (clear, drain)."""
    late = written_after_add(target, admin)
    if late is not None:
        return f"a file was written after the add died ({late})"
    diff = differs_from_head(repo_root, target, admin)
    if diff is not None:
        return f"the tree holds what its HEAD does not ({diff})"
    return None


def detached_unreachable(repo_root: Path, admin: Path) -> Optional[str]:
    """The detached HEAD sha of ``admin`` when NO branch, tag or remote-tracking ref contains it (removing the
    admin dir would lose that commit with its HEAD and reflog), else None. A HEAD on a branch is None (the branch
    ref keeps it). Unresolvable detached HEAD = returned as unknown (Prism #265 P1 4/5)."""
    head = (_read(admin / "HEAD") or "").strip()
    if not head or head.startswith("ref: "):
        return None
    sha = rev(repo_root, head)
    if not sha:
        return head or "unresolvable"
    p = _git(repo_root, "for-each-ref", "--contains", sha, "--count=1", "--format=%(refname)",
             "refs/heads/", "refs/tags/", "refs/remotes/", timeout=60)
    if p is None or p.returncode != 0 or p.stdout.strip() == "":
        return sha
    return None


def sha_has_own_commits(repo_root: Path, tip: str) -> Optional[bool]:
    """True when ``tip`` is a commit no remote-tracking ref has; None = unknown."""
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


def drain_half_built(repo_root: Path, target: Path, *, branch: str, branch_sha: str = "") -> dict:
    """Operator drain (``scripts/kanban_worktree_residue_audit.py --drain --execute``) of ONE tree the audit already
    attributed: removed only while exactly one registration names it, that one is still add residue (locked
    ``initializing``, no index), the tree holds no work (:func:`holds_work`) and its HEAD is not an unreachable
    detached commit (:func:`detached_unreachable`), all re-checked here. The branch is deleted only at
    ``branch_sha``, the tip the audit approved (atomic ``update-ref -d <ref> <sha>``): a branch that moved since is
    kept. Empty ``branch_sha`` = keep the branch."""
    rep = {"removed": False, "admin_dropped": [], "branch_deleted": False, "kept": []}
    try:
        common = common_dir(repo_root)
        admins = admin_dirs_for(common, target) if common is not None else []
        if len(admins) != 1 or not _init_residue(admins[0]):
            rep["kept"].append(f"{len(admins)} registrations / not add residue any more")
            return rep
        work = holds_work(repo_root, target, admins[0])
        if work is not None:
            rep["kept"].append(work)
            return rep
        det = detached_unreachable(repo_root, admins[0])
        if det is not None:
            rep["kept"].append(f"detached HEAD {det} is on no branch, tag or remote ref")
            return rep
        _drop_tree(repo_root, common, target, admins[0], rmtree_path=True, report=rep)
        if branch and branch_sha and rep["removed"]:
            rep["branch_deleted"] = _delete_branch_at(repo_root, common, branch, branch_sha)
            if not rep["branch_deleted"]:
                rep["kept"].append(f"branch {branch} no longer at the approved {branch_sha[:12]}: kept")
    except Exception as exc:
        rep["kept"].append(f"cleanup error: {exc}")
    return rep


def _trunk_contains(repo_root: Path, sha: str) -> bool:
    """``sha`` is reachable from the checkout's remote trunk ref (no fetch): a branch at ``sha`` holds nothing a
    fresh ``-b <branch> <trunk>`` would lose."""
    try:
        from hermes_cli.kanban_branch_base import fresh_trunk_ref
        trunk = fresh_trunk_ref(repo_root, fetch=False)
    except Exception:
        return False
    if not trunk or not sha:
        return False
    p = _git(repo_root, "merge-base", "--is-ancestor", sha, trunk)
    return p is not None and p.returncode == 0


def clear_half_built(repo_root: Path, target: Path, *, branch: str, min_age_s: int = HALF_BUILT_MIN_AGE_S,
                     now: Optional[float] = None) -> dict:
    """Clear a card's own leftover half-built tree (placement, t_f609a5a0). ``{"cleared", "why", ...}``.

    Ownership: the caller passes the card's canonical ``<repo>/.worktrees/<id>`` and its branch. Refused when the
    tree is not half-built (:func:`half_built`), younger than ``min_age_s`` (an add may still be running), holds
    work (:func:`holds_work`: a late write, an untracked file, content that is not HEAD's), has a detached HEAD no
    ref contains (:func:`detached_unreachable`), or is on another branch. ``holds_work: True`` marks the two
    refusals no forced remove may override.

    The LOCAL branch is deleted only when the remote trunk already contains its tip (it has no commits of its own,
    so the next spawn's ``-b <branch> <trunk>`` loses nothing). Otherwise it is kept, including a branch whose work
    a worker pushed (a remote has the tip, the trunk does not): the next spawn's ``worktree add <path> <branch>``
    then checks out that same branch at that same tip, so the card's recorded ``branch_name`` still resolves to its
    work. ``branch_kept`` says which."""
    hb = half_built(repo_root, target, now=now)
    if hb is None:
        return {"cleared": False, "why": "not a half-built tree"}
    if hb["age_s"] < min_age_s:
        return {"cleared": False, "why": f"initializing lock is {hb['age_s']}s old (< {min_age_s}s)", **hb}
    admin = Path(hb["admin"])
    work = holds_work(repo_root, target, admin)
    if work is not None:
        return {"cleared": False, "holds_work": True, "why": work, **hb}
    det = detached_unreachable(repo_root, admin)
    if det is not None:
        return {"cleared": False, "holds_work": True,
                "why": f"detached HEAD {det} is on no branch, tag or remote ref", **hb}
    if hb["branch"] and hb["branch"] != branch:
        return {"cleared": False, "why": f"tree is on {hb['branch']}, not {branch}", **hb}
    common = common_dir(repo_root)
    rep = {"removed": False, "admin_dropped": [], "branch_deleted": False, "kept": []}
    try:
        if common is None or admin_dirs_for(common, target) != [admin] or not _init_residue(admin):
            return {"cleared": False, "why": "tree changed while being assessed", **hb}
        _drop_tree(repo_root, common, target, admin, rmtree_path=True, report=rep)
        tip = rev(repo_root, f"refs/heads/{branch}") if branch else ""
        if tip and _trunk_contains(repo_root, tip):
            rep["branch_deleted"] = _delete_branch_at(repo_root, common, branch, tip)
    except Exception as exc:
        rep["kept"].append(f"cleanup error: {exc}")
    cleared = not target.exists() and not admin_dirs_for(common or Path("/nonexistent"), target)
    return {"cleared": cleared, "why": "cleared" if cleared else f"cleanup incomplete: {rep}",
            "branch_kept": not rep["branch_deleted"], **hb, **rep}
