"""Dead linked-worktree stubs at a kanban worktree target (t_42e59999).

A stub is a non-empty path where ``git worktree add`` must create a worktree,
but which is not a live worktree of the repo. The incident case: a ``.git``
gitfile pointing at a ``.git/worktrees/<name>`` admin dir that a prune removed
(t_5c6c67c5). ``worktree add`` refuses "already exists" on every retry, so the
card used to burn its whole retry budget and end ``gave_up``.

A stub whose files all match the tree of the ref the worktree would check out
holds nothing unique. It is MOVED (never removed) to
``<parent>/.aside/<name>-stub-<UTC stamp>`` on the same volume. Any admin dir
still registered to the path moves to ``<common>/worktrees.aside/``, so the add
can go ahead. That is a targeted prune: a repo-wide ``worktree prune`` would
also drop registrations of live worktrees on a volume that is unmounted now.
Anything else (an untracked or modified file, or a path we cannot read) may
hold work. That raises :class:`WorkspaceUnavailable`, and the dispatcher's
refusal path releases the claim without charging a failure: a placement hold,
paged once per episode, that can never reach ``gave_up``.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable

DEAD_STUB_HELD = "dead_worktree_stub_held"


def _git(repo: Path, *args: str, input: Optional[bytes] = None, timeout: int = 120):
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=input,
        stdin=None if input is not None else subprocess.DEVNULL,
        capture_output=True, timeout=timeout,
    )


def _tree_blobs(repo_root: Path, ref: str) -> Optional[dict[str, str]]:
    out = _git(repo_root, "ls-tree", "-r", "-z", "--full-tree", ref)
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


def _first_difference(repo_root: Path, stub: Path, ref: str) -> Optional[str]:
    """``None`` when every file in *stub* matches *ref*'s tree, else a reason."""
    if stub.is_symlink() or not stub.is_dir():
        return "not a directory"
    blobs = _tree_blobs(repo_root, ref)
    if blobs is None:
        return f"cannot read the tree of {ref}"
    files: list[str] = []
    links: list[str] = []
    errors: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(stub, onerror=errors.append):
        rel_dir = os.path.relpath(dirpath, stub)
        if rel_dir == ".":
            # The stub's own gitfile (or a gitdir) is the stub, not work.
            dirnames[:] = [d for d in dirnames if d != ".git"]
            filenames = [f for f in filenames if f != ".git"]
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if rel not in blobs:
                return f"untracked file {rel}"
            (links if os.path.islink(os.path.join(dirpath, name)) else files).append(rel)
    if errors:
        return f"unreadable: {errors[0]}"
    if files:
        hashed = _git(
            repo_root, "hash-object", "--stdin-paths",
            input="\n".join(str(stub / rel) for rel in files).encode() + b"\n",
            timeout=600,
        )
        shas = hashed.stdout.decode().split()
        if hashed.returncode != 0 or len(shas) != len(files):
            return "cannot hash the stub's files"
        for rel, sha in zip(files, shas):
            if not blobs[rel].endswith(f":{sha}") or blobs[rel].startswith("120000:"):
                return f"modified file {rel}"
    for rel in links:
        hashed = _git(repo_root, "hash-object", "--stdin",
                      input=os.fsencode(os.readlink(stub / rel)))
        if hashed.returncode != 0 or blobs[rel] != f"120000:{hashed.stdout.decode().strip()}":
            return f"modified symlink {rel}"
    return None


def _registered_admin_dirs(repo_root: Path, stub: Path) -> list[Path]:
    """Admin dirs under ``<common>/worktrees`` that still point at *stub*."""
    out = _git(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if out.returncode != 0:
        return []
    admin_root = Path(out.stdout.decode().strip()) / "worktrees"
    want = os.path.realpath(stub / ".git")
    found = []
    for admin in sorted(admin_root.glob("*")) if admin_root.is_dir() else []:
        try:
            pointed = (admin / "gitdir").read_text().strip()
        except OSError:
            continue
        if os.path.realpath(pointed) == want:
            found.append(admin)
    return found


def clear_dead_stub(repo_root: Path, stub: Path, ref: str) -> Path:
    """Move a stub holding nothing unique aside and return where it went.

    Raises :class:`WorkspaceUnavailable` (no retry charged) when the stub may
    hold work; the stub is left exactly as it was.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    aside = stub.parent / ".aside" / f"{stub.name}-stub-{stamp}"
    try:
        difference = _first_difference(repo_root, stub, ref)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        difference = f"unreadable: {exc}"
    if difference is not None:
        raise WorkspaceUnavailable(
            f"{DEAD_STUB_HELD}: {stub} is not a live worktree of {repo_root} and "
            f"may hold work ({difference}); nothing was moved and no retry was "
            f"charged. Inspect it, then: mv '{stub}' '{aside}' && "
            f"git -C '{repo_root}' worktree prune"
        )
    aside.parent.mkdir(parents=True, exist_ok=True)
    admins = _registered_admin_dirs(repo_root, stub)
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
