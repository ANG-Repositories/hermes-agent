"""Worktree cards on pool hosts: clone from origin + the host's git mirror.

A ``--workspace worktree:`` card used to be Studio-only (``portable()`` rule
``workspace_kind``): its checkout is a linked worktree of a Studio repo. When
the repo has a mirror on a pool host (``/srv/ci/git-mirrors/*.git`` on
ci-box / ace-ai / ace-media), the card can run there instead (t_6d9e7d90):
the dispatcher clones origin into the card's scratch path on that host with
the mirror as ``--reference --dissociate`` (~13 s for hermes-home on
ci-box), checks out the card's branch (``wt/<id>`` unless set), and converts
the card to ``scratch`` at that path. Every rerun binds to that host
(:func:`ported_host`, independent of ``placement.continuation``). Remote GC
(``kanban_survivor.remove_workspace_dir`` remote path) deletes the clone
only when it is clean and every local commit is on a remote, so unpushed
work is held, never collected.

Which repos: ``<fleet>/kanban-worktree-mirrors.json`` (schema 1)::

    {"schema": 1, "repos": {"ANG-Ventures/hermes-home":
        {"mirror": "/srv/ci/git-mirrors/hermes-home-mirror.git",
         "hosts": ["ci-box", "ace-ai", "ace-media"]}}}

Absent / unreadable / malformed file = no repo ports (fail closed; the card
stays Studio-local as before). That file is the kill switch: drop a row.

Only an ANCHOR worktree ports (``workspace_path`` unset, or naming a repo
root): a concrete target path is a place on the Studio someone chose. A card
whose ``.worktrees/<id>`` already exists on the Studio keeps it (that tree
may hold a prior run's work).

DERIVED mode (t_1d2c5939, default; ``KANBAN_MIRROR_DERIVED=0`` or a file
``"derived": false`` restores the hand list above): every GitHub repo a card
anchors on ports to EVERY pool host; there is no per-repo allowlist. The
file is only an override: schema 2 ``{"schema": 2, "exclude": [slug],
"mirror_paths": {slug: path | {"*": path, host: path}}}`` (schema 1 rows are
still read as mirror paths; their ``hosts`` are ignored). A repo with no
mirror path uses ``~/.hermes/mirrors/<owner>/<repo>.git`` on the host; the
clone is ``--reference-if-able``, so an absent mirror is a slower clone,
never a refusal. A card branch with commits origin lacks is PUSHED at spawn
(``--force-with-lease``) and then ported (:func:`push_card_branch`); only a
card whose own Studio tree has uncommitted or untracked work stays local,
named ``unpushed_dirty_tree``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: the in-process cache is the only pending marker
    fcntl = None

MIRRORS_FILE = "kanban-worktree-mirrors.json"
DERIVED_ENV = "KANBAN_MIRROR_DERIVED"
# Per-host default mirror of a derived repo; "~/" expands on the host (clone_script).
DERIVED_MIRROR_ROOT = "~/.hermes/mirrors"
PUSH_TIMEOUT_S = 120
# Named local-only reasons (``why`` of :func:`assess`), the tail of not_portable:workspace_kind:<why>.
WHY_DIRTY = "unpushed_dirty_tree"
WHY_OFF_BRANCH = "own_tree_off_branch"
WHY_CONCRETE = "concrete_target_path"
WHY_NOT_REPO = "not_a_repo"
WHY_NOT_GITHUB = "origin_not_github"
WHY_EXCLUDED = "excluded"
WHY_NO_POOL = "no_pool_host"
WHY_BAD_OVERRIDES = "overrides_file_malformed"
PORTED_EVENT = "worktree_ported"
# portable() bind rule of a ported card: it runs where its clone is.
PORT_BIND_RULE = "worktree_port"
CLONE_TIMEOUT_S = 300
# Exactly github.com, in the three shapes git accepts (Prism afc85da13a79):
# a host merely ENDING in github.com, or a path containing it, is no match.
_SLUG_RE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")
_ORIGIN_TTL_S = 300.0
_origin_cache: Dict[str, Tuple[float, Optional[str]]] = {}
_log = logging.getLogger(__name__)


def read_mirrors(fleet_dir: Path) -> Dict[str, Tuple[str, Tuple[str, ...], Dict[str, str]]]:
    """``{owner/repo (lower): (mirror path, hosts, {host: mirror path})}``; {} on any doubt.

    ``mirrors`` (optional, beside ``mirror``) overrides the path per host: macOS
    hosts have a sealed root and cannot hold ``/srv`` (mbp-2026 keeps them under
    ``/Users/Shared/git-mirrors``). A malformed ``mirrors`` drops the whole row."""
    try:
        doc = json.loads((Path(fleet_dir) / MIRRORS_FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    repos = doc.get("repos") if isinstance(doc, dict) and doc.get("schema") == 1 else None
    if isinstance(doc, dict) and doc.get("schema") == 2 and "repos" not in doc:
        # Frozen rollback snapshot, not an editable allowlist.
        repos = {
            slug: {"mirror": mirror, "hosts": ["ci-box", "ace-ai", "ace-media", "nas-fleet"]}
            for slug, mirror in (
                ("ANG-Ventures/hermes-home", "/srv/ci/git-mirrors/hermes-home-mirror.git"),
                ("ANG-Ventures/hermes-agent", "/srv/ci/git-mirrors/hermes-mirror.git"),
                ("ANG-Ventures/prism-router", "/home/kanbanw/.hermes/mirrors/ANG-Ventures/prism-router.git"),
                ("ANG-Ventures/fleetreview-router", "/home/kanbanw/.hermes/mirrors/ANG-Ventures/prism-router.git"))}
    out: Dict[str, Tuple[str, Tuple[str, ...], Dict[str, str]]] = {}
    if not isinstance(repos, dict):
        return {}
    for slug, row in repos.items():
        if not isinstance(slug, str) or "/" not in slug or not isinstance(row, dict):
            continue
        mirror, hosts, per_host = row.get("mirror"), row.get("hosts"), row.get("mirrors", {})
        if not (isinstance(mirror, str) and mirror.startswith("/")) or not isinstance(hosts, list):
            continue
        if not isinstance(per_host, dict) or not all(
                isinstance(h, str) and h and isinstance(p, str) and p.startswith("/")
                for h, p in per_host.items()):
            continue
        hs = tuple(h.lower() for h in hosts if isinstance(h, str) and h)
        if hs:
            out[slug.lower()] = (mirror, hs, {h.lower(): p for h, p in per_host.items()})
    return out


def _read_doc(fleet_dir: Path):
    """The parsed override file; ``None`` when absent, ``False`` when unreadable/malformed."""
    path = Path(fleet_dir) / MIRRORS_FILE
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError:
        return False
    try:
        doc = json.loads(text)
    except ValueError:
        return False
    return doc if isinstance(doc, dict) else False


def derived_enabled(fleet_dir: Path) -> bool:
    """Derived mode (t_1d2c5939) unless ``KANBAN_MIRROR_DERIVED=0`` or the file says ``"derived": false``."""
    if os.environ.get(DERIVED_ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return False
    doc = _read_doc(fleet_dir)
    return not (isinstance(doc, dict) and doc.get("derived") is False)


def read_overrides(fleet_dir: Path) -> Optional[Tuple[frozenset, Dict[str, Tuple[Optional[str], Dict[str, str]]]]]:
    """Derived-mode overrides: ``(excluded slugs (lower), {slug (lower): (default mirror | None, {host: mirror})})``.

    Absent file = no overrides. A file that exists but cannot be read, or a malformed schema 2 entry, is None
    (fail closed: nothing ports, the same as a broken hand list). Schema 1 rows count as mirror paths only."""
    doc = _read_doc(fleet_dir)
    if doc is None:
        return frozenset(), {}
    if doc is False:
        return None
    if doc.get("schema") == 1:
        return frozenset(), {s: (row[0], row[2]) for s, row in read_mirrors(fleet_dir).items()}
    if doc.get("schema") != 2:
        return None
    exclude, paths = doc.get("exclude", []), doc.get("mirror_paths", {})
    if not isinstance(exclude, list) or not all(isinstance(s, str) and "/" in s for s in exclude):
        return None
    if not isinstance(paths, dict):
        return None
    out: Dict[str, Tuple[Optional[str], Dict[str, str]]] = {}
    for slug, val in paths.items():
        if not isinstance(slug, str) or "/" not in slug:
            return None
        if isinstance(val, str):
            val = {"*": val}
        if not isinstance(val, dict) or not val or not all(
                isinstance(h, str) and h and isinstance(p, str) and (p.startswith("/") or p.startswith("~/"))
                for h, p in val.items()):
            return None
        out[slug.lower()] = (val.get("*"), {h.lower(): p for h, p in val.items() if h != "*"})
    return frozenset(s.lower() for s in exclude), out


def derived_route(slug: str, fleet_dir: Path, pool_hosts, why: Optional[List[str]] = None
                  ) -> Optional[Tuple[str, Tuple[str, ...], Dict[str, str]]]:
    """``(mirror, hosts, {host: mirror})`` for ``slug`` in derived mode: every pool host, the override path or
    ``~/.hermes/mirrors/<slug>.git``. None (reason appended to ``why``) when excluded / no pool / bad file."""
    ov = read_overrides(fleet_dir)
    if ov is None:
        _why(why, WHY_BAD_OVERRIDES)
        return None
    exclude, paths = ov
    if slug.lower() in exclude:
        _why(why, WHY_EXCLUDED)
        return None
    hosts = tuple(dict.fromkeys(str(h).lower() for h in (pool_hosts or ()) if h))
    if not hosts:
        _why(why, WHY_NO_POOL)
        return None
    default, per_host = paths.get(slug.lower(), (None, {}))
    return (default or f"{DERIVED_MIRROR_ROOT}/{slug}.git"), hosts, dict(per_host)


def _why(why: Optional[List[str]], reason: str) -> None:
    if why is not None and not why:
        why.append(reason)


def mirror_for(port: tuple, host: str) -> str:
    """The mirror path ``host`` holds for a :func:`port_for` result."""
    overrides = port[3] if len(port) > 3 else {}
    return overrides.get(str(host).lower(), port[1])


def origin_slug(repo: Path, runner: Callable = subprocess.run) -> Optional[str]:
    """``owner/repo`` of ``repo``'s ``origin`` (GitHub URLs only), cached."""
    key = str(repo)
    hit = _origin_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _ORIGIN_TTL_S:
        return hit[1]
    slug = None
    try:
        proc = runner(["git", "-C", key, "remote", "get-url", "origin"], capture_output=True,
                      text=True, encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
        if getattr(proc, "returncode", 1) == 0:
            m = _SLUG_RE.match((proc.stdout or "").strip())
            slug = m.group(1) if m else None
    except (OSError, subprocess.SubprocessError):
        slug = None
    _origin_cache[key] = (time.monotonic(), slug)
    return slug


def _clean_on_branch(tree: Path, branch: str, runner: Callable) -> bool:
    """``tree`` is checked out on ``branch`` with nothing uncommitted or
    untracked (ignored files excepted): a clone of the pushed branch loses
    nothing that only this tree holds."""
    def _out(*args):
        try:
            proc = runner(["git", "-C", str(tree), *args], capture_output=True, text=True, encoding="utf-8",
                          timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if getattr(proc, "returncode", 1) == 0 else None
    cur = _out("branch", "--show-current")
    if cur is None or cur.strip() != branch:
        return False
    status = _out("status", "--porcelain", "--untracked-files=all")
    return status is not None and status.strip() == ""


def worktree_target_anchor(path: Path, name: Optional[str] = None) -> Optional[Path]:
    """The repo root a NOT-YET-CREATED ``<repo>/.worktrees/<name>`` target names, else None (t_e604c0f9). An
    existing target is never re-anchored: it may hold a prior run's work. ``name`` None accepts any leaf (the
    ``kanban create`` rewrite, before the card has an id); else the leaf must be that card id."""
    try:
        if not path.is_absolute() or path.exists() or path.parent.name != ".worktrees":
            return None
    except OSError:
        return None
    if name is not None and path.name != name:
        return None
    return path.parent.parent


def anchor_repo(task_id: str, workspace_path: Optional[str],
                board_default: Optional[str], branch: Optional[str] = None,
                runner: Callable = subprocess.run, *,
                allow_studio_tree: bool = False) -> Optional[Path]:
    """The repo root a worktree card would anchor on, or None (not portable).

    None for: a concrete target path; a LINKED worktree (a prior Studio run
    persisted ``<repo>/.worktrees/<id>``, whose toplevel is itself, Prism
    4a24c6d61776); an existing ``<repo>/.worktrees/<id>``; and a card branch
    that exists locally with commits origin does not have (or no origin ref
    at all), since the clone could not start from them (Prism 044612a3554e).

    ``allow_studio_tree`` (the Studio is a ``host_preference`` last-resort host
    for kanban in ``fleet/placement-policy.json``, t_c2fce87e): the
    card's OWN ``<repo>/.worktrees/<id>`` (persisted path or found under the
    anchor) no longer pins it to the Studio when that tree is on the card's
    branch, clean, and the branch is fully on origin (the check below). Any
    other concrete path still keeps the card local.

    The hand-list semantics: committed-but-unpushed work keeps the card local.
    :func:`assess` with ``push_ok=True`` is the derived mode's push-then-port."""
    top, _push = assess(task_id, workspace_path, board_default, branch, runner,
                        allow_studio_tree=allow_studio_tree, push_ok=False)
    return top


def assess(task_id: str, workspace_path: Optional[str], board_default: Optional[str],
           branch: Optional[str] = None, runner: Callable = subprocess.run, *,
           allow_studio_tree: bool = False, push_ok: bool = False,
           why: Optional[List[str]] = None) -> Tuple[Optional[Path], Optional[Tuple[str, str, str, str]]]:
    """``(anchor repo | None, push)``: :func:`anchor_repo`'s checks, with the reason a card stays local
    appended to ``why``. ``push_ok`` (derived mode, t_1d2c5939): a card branch whose commits origin lacks is
    no longer a refusal; ``push`` = ``(repo, branch, expected origin sha or "", assessed local sha)`` and the spawn pushes it
    (:func:`push_card_branch`) before the clone. Still local: an own tree with uncommitted/untracked work
    (``unpushed_dirty_tree``), and a branch that diverged from origin (a push would drop origin's commits)."""
    from hermes_cli.kanban_db_workspace import _git_toplevel, _is_linked_worktree_checkout
    raw = (workspace_path or "").strip() or (board_default or "").strip()
    if not raw:
        _why(why, WHY_NOT_REPO)
        return None, None
    p = Path(raw).expanduser()
    # <repo>/.worktrees/<id> not made yet: the repo is the anchor (t_e604c0f9). Only under the same switch as
    # the own-tree port: with host_preference off a concrete target keeps today's Studio pin (Prism 7eba47409a75).
    target = worktree_target_anchor(p, task_id) if allow_studio_tree else None
    if target is not None:
        p = target
    if not p.is_absolute() or not p.is_dir():
        _why(why, WHY_NOT_REPO)
        return None, None
    top = _git_toplevel(p)
    if top is None:
        _why(why, WHY_NOT_REPO)
        return None, None
    b = (branch or "").strip() or f"wt/{task_id}"
    own_tree = None
    if top != p.resolve() or _is_linked_worktree_checkout(top):
        # A concrete target or a linked worktree: local, unless it is this
        # card's own pushed .worktrees/<id> and the knob allows the port.
        if not (allow_studio_tree and top == p.resolve() and top.name == task_id
                and top.parent.name == ".worktrees" and _is_linked_worktree_checkout(top)):
            _why(why, WHY_CONCRETE)
            return None, None
        own_tree, top = top, top.parent.parent.resolve()
        if _git_toplevel(top) != top or _is_linked_worktree_checkout(top):
            _why(why, WHY_CONCRETE)
            return None, None
    elif (top / ".worktrees" / task_id).exists():
        own_tree = top / ".worktrees" / task_id
        if not (allow_studio_tree and _is_linked_worktree_checkout(own_tree)
                and _git_toplevel(own_tree) == own_tree.resolve()):
            _why(why, WHY_CONCRETE)
            return None, None  # a prior run's local worktree: its work stays where it is
    if own_tree is not None and not _clean_on_branch(own_tree, b, runner):
        # uncommitted or untracked work only the Studio has
        _why(why, WHY_DIRTY if _on_branch(own_tree, b, runner) else WHY_OFF_BRANCH)
        return None, None

    def _git(*args):
        try:
            return runner(["git", "-C", str(top), *args], capture_output=True, text=True, encoding="utf-8",
                          timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None

    def _sha(ref):
        proc = _git("rev-parse", "-q", "--verify", ref)
        if proc is None:
            return None
        return (proc.stdout or "").strip() if getattr(proc, "returncode", 1) == 0 else ""

    remote = _sha(f"refs/remotes/origin/{b}")
    local = _sha(f"refs/heads/{b}")
    if remote is None or local is None:
        _why(why, WHY_NOT_REPO)
        return None, None
    if own_tree is not None and not remote and not push_ok:
        _why(why, "unpushed_commits")
        return None, None  # the tree's commits must be on origin: no origin ref = local only
    if not local:
        return top, None  # no local branch at all: the clone makes it
    ahead = _git("rev-list", "--count", f"{remote}..{local}") if remote else None
    if remote and (ahead is None or getattr(ahead, "returncode", 1) != 0):
        _why(why, WHY_NOT_REPO)
        return None, None
    if remote and (ahead.stdout or "").strip() == "0":
        return top, None  # in step with origin
    if not push_ok:
        _why(why, "unpushed_commits")
        return None, None  # local commits (or no origin ref): only the Studio has the baseline
    if remote:
        anc = _git("merge-base", "--is-ancestor", remote, local)
        if anc is None or getattr(anc, "returncode", 1) != 0:
            _why(why, "diverged_from_origin")
            return None, None  # origin has commits the Studio lacks: a push would drop them
    return top, (str(top), b, remote, local)


def _on_branch(tree: Path, branch: str, runner: Callable) -> bool:
    try:
        proc = runner(["git", "-C", str(tree), "branch", "--show-current"], capture_output=True, text=True,
                      encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(proc, "returncode", 1) == 0 and (proc.stdout or "").strip() == branch


def push_card_branch(push: Tuple[str, str, str, str], runner: Callable = subprocess.run) -> None:
    """Push the card branch to origin before the port (t_1d2c5939): ``--force-with-lease`` pinned to the
    origin sha the routing saw ("" = the branch must not exist on origin), so a branch someone moved since
    is refused, never overwritten. Raises on failure (a spawn failure; the card stays where it is)."""
    repo, branch, expected, source = push
    # Push to the fetch URL explicitly: a remote's pushurl may name another repo.
    dest = runner(["git", "-C", repo, "remote", "get-url", "origin"], capture_output=True,
                  text=True, encoding="utf-8", timeout=10, stdin=subprocess.DEVNULL)
    if dest.returncode != 0 or not dest.stdout.strip():
        raise RuntimeError("cannot resolve clone source before push")
    # An explicit remote pushurl suppresses pushInsteadOf; passing the URL
    # directly does not. Command-local configuration leaves the checkout intact.
    destination = dest.stdout.strip()
    proc = runner(["git", "-C", repo,
                   "-c", f"remote.kanban-port-destination.url={destination}",
                   "-c", f"remote.kanban-port-destination.pushurl={destination}",
                   "push", "--porcelain",
                   f"--force-with-lease=refs/heads/{branch}:{expected}",
                   "kanban-port-destination", f"{source}:refs/heads/{branch}"],
                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=PUSH_TIMEOUT_S, stdin=subprocess.DEVNULL)
    rc = getattr(proc, "returncode", 1)
    if rc != 0:
        raise RuntimeError(f"push of {branch} from {repo} before the port failed rc={rc}: "
                           f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")


def port_for(task_id: str, workspace_path: Optional[str], board_default: Optional[str],
             fleet_dir: Path, runner: Callable = subprocess.run, *,
             branch: Optional[str] = None,
             allow_studio_tree: bool = False,
             pool_hosts=None, why: Optional[List[str]] = None) -> Optional[tuple]:
    """``(owner/repo, mirror, hosts, {host: mirror}, push)`` when this worktree card
    may run on a pool host, else None (:func:`mirror_for` picks the path).

    Derived mode (:func:`derived_enabled`): any GitHub repo, every host in ``pool_hosts``, ``push`` set when
    the card branch must be pushed first. Hand-list mode: the file's rows only, ``push`` always None."""
    if pool_hosts is not None and derived_enabled(fleet_dir):
        repo, push = assess(task_id, workspace_path, board_default, branch, runner,
                            allow_studio_tree=allow_studio_tree, push_ok=True, why=why)
        if repo is None:
            return None
        slug = origin_slug(repo, runner)
        if not slug:
            _why(why, WHY_NOT_GITHUB)
            return None
        route = derived_route(slug, fleet_dir, pool_hosts, why)
        return (slug, route[0], route[1], route[2], push) if route else None
    mirrors = read_mirrors(fleet_dir)
    if not mirrors:
        return None
    repo = anchor_repo(task_id, workspace_path, board_default, branch, runner,
                       allow_studio_tree=allow_studio_tree)
    slug = origin_slug(repo, runner) if repo is not None else None
    row = mirrors.get(slug.lower()) if slug else None
    return (slug, row[0], row[1], row[2], None) if row else None


def mint_rewrite(workspace_kind: Optional[str], workspace_path: Optional[str], fleet_dir: Path,
                 runner: Callable = subprocess.run) -> Tuple[Optional[str], Optional[str]]:
    """``kanban create`` half of t_e604c0f9: ``(workspace_path, note)``. A ``worktree:<repo>/.worktrees/<x>``
    target that does not exist yet, on a repo with a row in ``kanban-worktree-mirrors.json``, is rewritten to the
    anchor form ``worktree:<repo>`` (the dispatcher can then port it: FWA places it and the worktree is built on
    the chosen host). ``note`` says what happened, or why the card stays Studio-local; None for non-worktree cards
    and whenever the host_preference switch (t_c2fce87e) does not make the Studio a last resort: then ``kanban
    create`` stores and prints exactly what it did before (Prism 7eba47409a75)."""
    if workspace_kind != "worktree" or not workspace_path:
        return workspace_path, None
    try:
        from hermes_cli import placement_policy as _ppol
        from hermes_cli.kanban_worker_pool import STUDIO_HOST
        if not _ppol.load(Path(fleet_dir)).is_last_resort(STUDIO_HOST):
            return workspace_path, None
    except Exception:
        return workspace_path, None
    from hermes_cli.kanban_db_workspace import _git_toplevel
    p = Path(workspace_path).expanduser()
    target = worktree_target_anchor(p)
    repo = target if target is not None else p
    top = _git_toplevel(repo) if repo.is_dir() else None
    slug = origin_slug(top, runner) if top is not None else None
    if slug and derived_enabled(fleet_dir):
        # t_1d2c5939: every GitHub repo ports to every pool host (minus the file's exclusions).
        excl: List[str] = []
        row = derived_route(slug, fleet_dir, ("every pool host",), excl)
        if row is None:
            return workspace_path, f"worktree card stays Studio-local: {slug} {excl[0] if excl else 'not portable'}"
    else:
        row = read_mirrors(fleet_dir).get(slug.lower()) if slug else None
    if row is None:
        why = (f"{slug} has no row in fleet/{MIRRORS_FILE}" if slug
               else f"{workspace_path} is not a GitHub repo checkout")
        return workspace_path, f"worktree card stays Studio-local: {why}"
    hosts = ", ".join(row[1])
    if target is not None and top == target.resolve():
        return str(top), (f"workspace rewritten: worktree:{workspace_path} -> worktree:{top} ({slug} has a "
                          f"pool clone on {hosts}; FWA places the card and the worktree is built on that host)")
    if top == p.resolve():
        return workspace_path, f"worktree card is portable ({slug} has a pool clone on {hosts}; FWA places it)"
    return workspace_path, ("worktree card stays Studio-local: a concrete existing target path "
                            f"({workspace_path}) is kept where it is")


def clone_script(workspace: str, slug: str, mirror: str, branch: str, *,
                 url: Optional[str] = None, sha: Optional[str] = None) -> str:
    """Remote sh: clone origin into the (empty) workspace, then put HEAD on
    ``branch`` and PROVE it (rc 4 otherwise). The mirror only accelerates the
    clone: ``--dissociate`` copies what was borrowed, so a later mirror prune
    cannot take objects from a running card (Prism 02dfd9094b97). The branch
    baseline is the clone's own ``refs/remotes/origin/<branch>`` (fetched by
    the clone), never a second network call whose failure would read as
    "absent" (Prism 9b397aa5183d); else a new branch off origin/HEAD,
    --no-track. Re-run safe: an existing checkout of THIS origin finishes
    the checkout instead of returning early (Prism f43e48b43fc3). rc 3 = the
    workspace holds something else (never overwritten). ``sha`` (the pushed
    tip, Prism 3e2f8506c8ff): a reused checkout behind it is fetched and
    fast-forwarded, and HEAD is PROVED to be it (rc 5 otherwise)."""
    w, m, b = shlex.quote(workspace), shlex.quote(mirror), shlex.quote(branch)
    if mirror.startswith("~/"):
        # A derived per-host default (t_1d2c5939): the host's own $HOME, whatever user/OS it runs.
        m = '"$HOME"/' + shlex.quote(mirror[2:])
    url = shlex.quote(url or f"https://github.com/{slug}.git")
    from hermes_cli.kanban_worker_hosts import WORKSPACE_MODE  # t_69e47673: pool workspace, never world-searchable
    return (
        f"set -e; w={w}; mkdir -p -m {WORKSPACE_MODE} -- \"$w\"; "
        # the mirror is owned by the runner user: trust that one path only
        f"export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0={m}; "
        "if [ -d \"$w/.git\" ]; then "
        f"[ \"$(git -C \"$w\" remote get-url origin)\" = {url} ] || exit 3; "
        "elif [ -n \"$(ls -A -- \"$w\")\" ]; then exit 3; "
        f"else if [ -d {m}/objects ]; then "
        f"git -c gc.auto=0 -c maintenance.auto=false -c fetch.prune=false --git-dir={m} "
        "fetch -q --no-write-fetch-head origin || true; fi; "
        f"git clone -q --reference-if-able {m} --dissociate {url} \"$w\"; fi; "
        "cd \"$w\"; "
        f"if [ \"$(git branch --show-current)\" != {b} ]; then "
        f"if git rev-parse -q --verify refs/heads/{b} >/dev/null; then git checkout -q {b}; "
        f"elif git rev-parse -q --verify refs/remotes/origin/{b} >/dev/null; then "
        f"git checkout -q -b {b} --track origin/{b}; "
        f"else git checkout -q --no-track -b {b} origin/HEAD; fi; fi; "
        f"[ \"$(git branch --show-current)\" = {b} ] || exit 4"
        + (f"; if [ \"$(git rev-parse HEAD)\" != {shlex.quote(sha)} ]; then "
           f"{{ git fetch -q origin {b} && git merge -q --ff-only {shlex.quote(sha)}; }} || exit 5; fi; "
           f"[ \"$(git rev-parse HEAD)\" = {shlex.quote(sha)} ] || exit 5" if sha else "")
    )


def clone_on_host(host, workspace: str, slug: str, mirror: str, branch: str,
                  runner: Callable = subprocess.run, sha: Optional[str] = None) -> None:
    """Run :func:`clone_script` on ``host`` (a ``PoolHost``); raise on failure."""
    from hermes_cli.kanban_worker_pool import _ssh_argv
    proc = runner(_ssh_argv(host, clone_script(workspace, slug, mirror, branch, sha=sha)),
                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                  timeout=CLONE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    rc = getattr(proc, "returncode", 1)
    if rc != 0:
        raise RuntimeError(f"worktree clone of {slug} on {host.name} failed rc={rc}: "
                           f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")


# Pool-host clone off the dispatcher tick (t_915b8d2d). A gateway tick measured 5 of 6 py-spy samples in
# clone_on_host: the ssh fetch + clone ran inside the per-row loop and every other ready row waited on it.
# The gateway tick now STARTS the push+clone here and moves on. The card waits ``clone_pending`` and is claimed
# and launched on a later tick, once the clone has finished. A failed clone is raised at the old call site, so
# the claim release, spillover refund and failure accounting stay the existing exception path. Only the
# long-lived gateway tick uses this (``defer_pool_clone``): a one-shot CLI process would exit with the clone
# thread still running.
#
# Two guards (Prism 3e2f8506c8ff, d4847ef87c43). The key carries the push plan, i.e. the source tip sha: a tip
# that moved after the prefetch is a new key, the stale result is dropped and never collected. The pending
# clone is visible to EVERY dispatcher: a per-workspace flock (``clone_lock_path``) is taken in the tick before
# the job is submitted and held until it finishes. Any tick, deferred or not, in any process, that finds it held
# skips the card (:func:`clone_busy`), and the synchronous clone refuses to start under it
# (:func:`run_clone`). The lock dies with its process, so a crashed dispatcher never strands the card.
CLONE_PENDING = "clone_pending"
CLONE_PREFETCH_WORKERS = 4
CLONE_PREFETCH_TTL_S = 3600.0
_prefetch_lock = threading.Lock()
_prefetch: Dict[tuple, tuple] = {}   # key -> (Future, started monotonic)
_prefetch_pool: Optional[ThreadPoolExecutor] = None


def clone_key(task_id: str, host_name: str, workspace: str, slug: str, mirror: str, branch: str,
              push: Optional[tuple] = None) -> tuple:
    """``push`` = :func:`assess`'s push plan (repo, branch, origin sha, source tip sha) or None."""
    return (task_id, host_name, str(workspace), slug, mirror, branch, tuple(push) if push else None)


def clone_lock_path(lock_dir: Path, key: tuple) -> Path:
    """The per-workspace pending marker: one file per (host, workspace), whatever the tip."""
    return Path(lock_dir) / (hashlib.sha1(f"{key[1]}\0{key[2]}".encode()).hexdigest()[:24] + ".lock")


def _try_lock(path: Path) -> Optional[int]:
    """An fd holding ``path``'s exclusive flock, or None when another holder has it."""
    if fcntl is None:
        return -1
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _unlock(fd: Optional[int]) -> None:
    if fd is not None and fd >= 0:
        try:
            os.close(fd)   # closing the only fd releases the flock
        except OSError:
            pass


def _same_workspace(a: tuple, b: tuple) -> bool:
    return a[:3] == b[:3]


def _drop_stale_locked(key: tuple) -> bool:
    """Under ``_prefetch_lock``: forget finished entries of ``key``'s workspace under another key (the tip moved:
    never collected). True while one of them still runs (a second writer must not start)."""
    running = False
    for k, (fut, _t0) in list(_prefetch.items()):
        if k != key and _same_workspace(k, key):
            if fut.done():
                del _prefetch[k]
            else:
                running = True
    return running


def clone_ready(key: tuple, job: Callable[[], None], lock_path: Path) -> bool:
    """True once ``job`` (push + clone for ``key``) has finished, ok or failed. On the first call it takes the
    workspace lock, starts the job on a small background pool and returns False. While it runs, or while any
    other clone of the same workspace runs (this process or another), this returns False. The caller collects
    the outcome with :func:`run_clone`."""
    global _prefetch_pool
    now = time.monotonic()
    with _prefetch_lock:
        for k, (fut, t0) in list(_prefetch.items()):
            if fut.done() and now - t0 > CLONE_PREFETCH_TTL_S:
                del _prefetch[k]    # finished, never collected (the card moved on): forget it
        entry = _prefetch.get(key)
        if entry is not None:
            return entry[0].done()
        if _drop_stale_locked(key):
            return False
        fd = _try_lock(lock_path)
        if fd is None:
            return False            # another dispatcher's clone of this workspace is live
        if _prefetch_pool is None:
            _prefetch_pool = ThreadPoolExecutor(max_workers=CLONE_PREFETCH_WORKERS,
                                                thread_name_prefix="kanban-clone")
        try:
            fut = _prefetch_pool.submit(job)
        except BaseException:
            _unlock(fd)
            raise
        fut.add_done_callback(lambda _f, _fd=fd: _unlock(_fd))
        _prefetch[key] = (fut, now)
        return False


def clone_busy(key: tuple, lock_path: Path) -> bool:
    """For a tick that clones synchronously: True while a clone of ``key``'s workspace is pending anywhere
    (this process's cache, or the workspace lock held by another dispatcher)."""
    with _prefetch_lock:
        if any(_same_workspace(k, key) and not fut.done() for k, (fut, _t0) in _prefetch.items()):
            return True
    fd = _try_lock(lock_path)
    if fd is None:
        return True
    _unlock(fd)
    return False


def take_clone(key: tuple) -> bool:
    """Collect a finished prefetched clone: True when it succeeded, False when none is recorded for ``key``
    (the caller clones synchronously). Re-raises the clone's own exception when it failed. A finished clone of
    the same workspace under another key (an older tip) is dropped, never collected."""
    with _prefetch_lock:
        _drop_stale_locked(key)
        entry = _prefetch.get(key)
        if entry is None or not entry[0].done():
            return False
        del _prefetch[key]
    entry[0].result()
    return True


def run_clone(key: tuple, job: Callable[[], None], lock_path: Path) -> None:
    """The claim-time clone: collect the finished prefetch for ``key``, else run ``job`` under the workspace
    lock. Refuses (raises) rather than start a second writer while another clone of the workspace is live."""
    if take_clone(key):
        return
    with _prefetch_lock:
        if any(_same_workspace(k, key) and not fut.done() for k, (fut, _t0) in _prefetch.items()):
            raise RuntimeError(f"{CLONE_PENDING}: a clone of {key[2]} on {key[1]} is still running")
    fd = _try_lock(lock_path)
    if fd is None:
        raise RuntimeError(f"{CLONE_PENDING}: another dispatcher holds the clone of {key[2]} on {key[1]}")
    try:
        job()
    finally:
        _unlock(fd)


def convert(conn, task, workspace: str, branch: str, *, host: str, slug: str,
            mirror: str) -> None:
    """Persist the port: the card is now a scratch card at ``workspace``."""
    from hermes_cli import kanban_db as _kb
    with _kb.write_txn(conn):
        # Fenced to the launching run: the tick has the board lock handed back here (t_f60f502f).
        cur = conn.execute("UPDATE tasks SET workspace_kind='scratch', workspace_path=?, branch_name=? "
                           "WHERE id=? AND status='running' AND current_run_id IS ?",
                           (workspace, branch, task.id, task.current_run_id))
        if cur.rowcount == 0:
            raise _kb.ClaimLost(f"{task.id}: run {task.current_run_id} no longer owns the card")
        _kb._append_event(conn, task.id, PORTED_EVENT,
                          {"host": host, "repo": slug, "mirror": mirror, "branch": branch,
                           "workspace": workspace}, run_id=task.current_run_id)
    task.workspace_kind, task.workspace_path, task.branch_name = "scratch", workspace, branch


def board_default_workdir(board: Optional[str]) -> Optional[str]:
    from hermes_cli import kanban_db as _kb
    try:
        slug = board if board else _kb.get_current_board()
        return (_kb.read_board_metadata(slug).get("default_workdir") or "").strip() or None
    except Exception:
        return None


def ported_host(conn, task_id: str, workspace_path: Optional[str]) -> Optional[str]:
    """The pool host holding this card's ported clone, or None. Only the
    newest port event counts, and only while the card still points at that
    workspace (an operator who re-pointed it has moved it on)."""
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? "
                       "ORDER BY id DESC LIMIT 1", (task_id, PORTED_EVENT)).fetchone()
    if row is None:
        return None
    try:
        doc = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return None
    host = doc.get("host")
    if not isinstance(host, str) or not host or doc.get("workspace") != workspace_path:
        return None
    return host.lower()
