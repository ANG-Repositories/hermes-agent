"""Dead linked-worktree stubs at a kanban worktree target (t_42e59999).

A stub is a non-empty path where ``git worktree add`` must create a worktree,
but which is not a live worktree of the repo. The incident case: a ``.git``
gitfile pointing at a ``.git/worktrees/<name>`` admin dir that a prune removed
(t_5c6c67c5). ``worktree add`` refuses "already exists" on every retry, so the
card used to burn its whole retry budget and end ``gave_up``.

A stub holds nothing unique only when its working tree equals the tree of the
ref the worktree would check out (same path set, mode and blob for every path)
and any index still registered for it equals that tree too. It is MOVED (never
removed) to ``<parent>/.aside/<name>-stub-<UTC stamp>`` on the same volume. Any
admin dir still registered to the path moves to ``<common>/worktrees.aside/``,
so the add can go ahead. That is a targeted prune: a repo-wide ``worktree
prune`` would also drop registrations of live worktrees on a volume that is
unmounted now. Anything else (an untracked, modified, deleted or re-moded file,
a staged change, an initialised submodule, or a path we cannot read) may hold
work. That raises :class:`WorkspaceUnavailable`, and the dispatcher's
refusal path releases the claim without charging a failure: a placement hold,
paged once per episode, that can never reach ``gave_up``.
"""
from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Optional

from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable

DEAD_STUB_HELD = "dead_worktree_stub_held"
# One wall budget for the whole check (tree read, walk, hashing). It runs inline
# in the dispatcher's spawn path, and a stub can be a full checkout: past the
# budget the answer is UNKNOWN, so the stub is held, never moved or charged.
STUB_CHECK_BUDGET_S = 60.0
# Files per `hash-object` call, so the budget is checked between batches.
HASH_BATCH = 256


class _OverBudget(Exception):
    pass


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _OverBudget
    return left


def _git(repo: Path, *args: str, input: Optional[bytes] = None, timeout: float = 120):
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=input,
        stdin=None if input is not None else subprocess.DEVNULL,
        capture_output=True, timeout=timeout,
    )


def _tree_blobs(repo_root: Path, ref: str, deadline: float) -> Optional[dict[str, str]]:
    out = _git(repo_root, "ls-tree", "-r", "-z", "--full-tree", ref,
               timeout=_remaining(deadline))
    if out.returncode != 0:
        return None
    blobs: dict[str, str] = {}
    for entry in out.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        mode, _kind, sha = meta.decode().split(" ")
        blobs[os.fsdecode(path)] = f"{mode}:{sha}"
    return blobs


def _first_difference(repo_root: Path, stub: Path, ref: str, deadline: float) -> Optional[str]:
    """``None`` when *stub*'s working tree equals *ref*'s tree, else a reason.

    Equal means the same path set (a tracked path missing from the stub is a
    difference), the same git mode and the same blob for every path.
    Raises :class:`_OverBudget` once *deadline* (``time.monotonic``) passes.
    """
    if stub.is_symlink() or not stub.is_dir():
        return "not a directory"
    blobs = _tree_blobs(repo_root, ref, deadline)
    if blobs is None:
        return f"cannot read the tree of {ref}"
    seen: set[str] = set()
    files: list[str] = []
    links: list[str] = []
    errors: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(stub, onerror=errors.append):
        _remaining(deadline)
        rel_dir = os.path.relpath(dirpath, stub)
        if rel_dir == ".":
            # The stub's own gitfile (or a gitdir) is the stub, not work.
            dirnames[:] = [d for d in dirnames if d != ".git"]
            filenames = [f for f in filenames if f != ".git"]
        keep = []
        for d in dirnames:
            rel = d if rel_dir == "." else f"{rel_dir}/{d}"
            full = os.path.join(dirpath, d)
            if blobs.get(rel, "").startswith("160000:") and not os.path.islink(full):
                # A submodule: only its uninitialised (empty) checkout is known
                # to equal the gitlink; anything inside is held, by design.
                if os.listdir(full):
                    return f"initialised submodule {rel}"
                seen.add(rel)
            elif os.path.islink(full):
                filenames.append(d)
            else:
                keep.append(d)
        dirnames[:] = keep
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if rel not in blobs:
                return f"untracked file {rel}"
            seen.add(rel)
            st = os.lstat(os.path.join(dirpath, name))
            if stat.S_ISLNK(st.st_mode):
                mode = "120000"
                links.append(rel)
            elif stat.S_ISREG(st.st_mode):
                mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
                files.append(rel)
            else:
                return f"special file {rel}"
            if not blobs[rel].startswith(f"{mode}:"):
                return f"mode change {rel} ({blobs[rel].split(':')[0]} -> {mode})"
    if errors:
        return f"unreadable: {errors[0]}"
    missing = sorted(set(blobs) - seen)
    if missing:
        return f"deleted tracked file {missing[0]}"
    for start in range(0, len(files), HASH_BATCH):
        batch = files[start:start + HASH_BATCH]
        hashed = _git(
            repo_root, "hash-object", "--stdin-paths",
            input="\n".join(str(stub / rel) for rel in batch).encode() + b"\n",
            timeout=_remaining(deadline),
        )
        shas = hashed.stdout.decode().split()
        if hashed.returncode != 0 or len(shas) != len(batch):
            return "cannot hash the stub's files"
        for rel, sha in zip(batch, shas):
            if not blobs[rel].endswith(f":{sha}"):
                return f"modified file {rel}"
    for rel in links:
        hashed = _git(repo_root, "hash-object", "--stdin",
                      input=os.fsencode(os.readlink(stub / rel)),
                      timeout=_remaining(deadline))
        if hashed.returncode != 0 or blobs[rel] != f"120000:{hashed.stdout.decode().strip()}":
            return f"modified symlink {rel}"
    return None


def _registered_admin_dirs(repo_root: Path, stub: Path, deadline: float) -> list[Path]:
    """Admin dirs under ``<common>/worktrees`` that still point at *stub*.

    An admin dir whose ``gitdir`` cannot be read (other than absent) raises
    ``OSError``: it might be the stub's, so the caller holds.
    """
    out = _git(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir",
               timeout=_remaining(deadline))
    if out.returncode != 0:
        raise OSError(f"cannot resolve the common git dir of {repo_root}")
    admin_root = Path(out.stdout.decode().strip()) / "worktrees"
    want = os.path.realpath(stub / ".git")
    found = []
    for admin in sorted(admin_root.glob("*")) if admin_root.is_dir() else []:
        try:
            pointed = (admin / "gitdir").read_text().strip()
        except FileNotFoundError:
            continue
        if os.path.realpath(pointed) == want:
            found.append(admin)
    return found


def _admin_difference(repo_root: Path, admin: Path, ref: str, deadline: float) -> Optional[str]:
    """``None`` when moving *admin* out of ``worktrees`` cannot drop work.

    Its index and a detached HEAD are reachable only through the admin dir, so
    `git gc` may prune what they hold once it moves: the index must equal
    *ref*'s tree and a detached HEAD must be contained in *ref*.
    """
    index = admin / "index"
    if not index.is_file():
        return f"no readable index in {admin}"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    out = subprocess.run(
        ["git", "-C", str(repo_root), "diff-index", "--cached", "--quiet", ref, "--"],
        stdin=subprocess.DEVNULL, capture_output=True, env=env,
        timeout=_remaining(deadline),
    )
    if out.returncode == 1:
        return f"staged changes in {index}"
    if out.returncode != 0:
        return f"cannot read the index {index}"
    head = (admin / "HEAD").read_text().strip()
    if not head.startswith("ref: "):
        contained = _git(repo_root, "merge-base", "--is-ancestor", head, ref,
                         timeout=_remaining(deadline))
        if contained.returncode != 0:
            return f"detached HEAD {head[:12]} in {admin} is not in {ref}"
    return None


def _difference(repo_root: Path, stub: Path, ref: str, deadline: float) -> tuple[Optional[str], list[Path]]:
    difference = _first_difference(repo_root, stub, ref, deadline)
    if difference is not None:
        return difference, []
    admins = _registered_admin_dirs(repo_root, stub, deadline)
    for admin in admins:
        difference = _admin_difference(repo_root, admin, ref, deadline)
        if difference is not None:
            return difference, []
    return None, admins


def _held(repo_root: Path, stub: Path, aside: Path, difference: str) -> WorkspaceUnavailable:
    return WorkspaceUnavailable(
        f"{DEAD_STUB_HELD}: {stub} is not a live worktree of {repo_root} and "
        f"may hold work ({difference}); nothing was moved and no retry was "
        f"charged. Inspect it, then: mv '{stub}' '{aside}' && "
        f"git -C '{repo_root}' worktree prune"
    )


def _aside_path(stub: Path) -> tuple[Path, str]:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return stub.parent / ".aside" / f"{stub.name}-stub-{stamp}", stamp


def target_occupied(repo_root: Path, target: Path) -> bool:
    """True when something non-empty sits at *target*; an unreadable target holds."""
    try:
        return target.is_symlink() or (
            target.exists() and (not target.is_dir() or any(target.iterdir()))
        )
    except OSError as exc:
        raise _held(repo_root, target, _aside_path(target)[0], f"unreadable: {exc}") from exc


def clear_dead_stub(repo_root: Path, stub: Path, ref: str) -> Path:
    """Move a stub holding nothing unique aside and return where it went.

    Raises :class:`WorkspaceUnavailable` (no retry charged) when the stub, or
    an index still registered for it, may hold work; nothing is moved.
    """
    aside, stamp = _aside_path(stub)
    budget = STUB_CHECK_BUDGET_S
    try:
        difference, admins = _difference(repo_root, stub, ref, time.monotonic() + budget)
    except (_OverBudget, subprocess.TimeoutExpired):
        difference, admins = f"check exceeded {budget:g}s", []
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        difference, admins = f"unreadable: {exc}", []
    if difference is not None:
        raise _held(repo_root, stub, aside, difference)
    aside.parent.mkdir(parents=True, exist_ok=True)
    os.rename(stub, aside)  # same parent filesystem: a rename, never a copy
    for admin in admins:
        # Out of <common>/worktrees (so `worktree add` sees no registration),
        # kept beside it on the repo's own volume.
        kept = admin.parent.parent / "worktrees.aside" / f"{admin.name}-{stamp}"
        kept.parent.mkdir(parents=True, exist_ok=True)
        os.rename(admin, kept)
    from hermes_cli import kanban_db as _kb

    _kb._log.warning(
        "kanban worktree %s: dead stub held nothing unique against %s; moved to %s",
        stub, ref, aside,
    )
    return aside
