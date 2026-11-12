"""Workspace carry with no size cap, off the dispatcher tick (t_f3a411c2; Ace 2026-11-12 09:25 "no cap").

Before this, ``kanban_worker_hosts.carry_workspace`` copied a local_files card's Studio scratch dir to the pool host
INSIDE the dispatcher tick, so ``CARRY_MAX_BYTES`` / ``CARRY_MAX_ENTRIES`` bounded the tick and a bigger dir made the
card wait. Here the tick only STARTS the carry (a background thread) and moves on; the card waits
``carry_pending:<host>`` and is claimed on a later tick once the copy has finished. Nothing is capped by size.

The carry, in order (all in the background thread):

1. census: bytes and entries of the source (no cap, no wall bound), and the git-dir check a file copy needs
   (``carried_gitdir_loss``): a checkout whose git dir would not travel is REFUSED here and the card keeps the capped
   rule (split carry / wait) as before;
2. the destination is prepared (the same mkdir every placed run gets) and its free space read; ``need > free`` is the
   one size refusal left: ``No room on <host>: need N, free M``;
3. ``rsync -a --partial --delete`` over fleet ssh into a STAGING dir next to the workspace (``.carry-<leaf>``), never
   into the workspace itself. An interrupted copy leaves only the staging dir, which the next attempt on that host
   resumes; a later carry to another host removes it first.

At the claim the staging dir is moved onto the workspace path in one ssh call (``finalize``), and the existing
``workspace_carried`` event is written, so the card binds to that host as before. The Studio copy is not touched.

Events on the card: ``workspace_carry_started`` / ``workspace_carry_progress`` (at most one a minute) /
``workspace_carry_failed`` (``state``: clean | resumable | refused) / ``workspace_carried`` (with bytes, entries,
seconds and the CPU the Studio paid).

Kill switch: ROOT ``kanban.placement.carry_caps: true`` restores the in-tick carry and its two caps exactly.
"""

from __future__ import annotations

import json
import logging
import os
import resource
import shlex
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from hermes_cli.kanban_worker_pool import PoolHost, _ssh_argv
from hermes_cli.kanban_workspace_policy import WorkspaceUnavailable

_log = logging.getLogger(__name__)

CAPS_KNOB = "carry_caps"
BACKGROUND = "background"          # the dispatcher's carry_out value for a card carried in the background
STARTED_EVENT = "workspace_carry_started"
PROGRESS_EVENT = "workspace_carry_progress"
FAILED_EVENT = "workspace_carry_failed"
PENDING = "carry_pending"
NO_ROOM = "carry_no_room"
FAILED = "carry_failed"
RSYNC_IO_TIMEOUT_S = 600           # rsync --timeout: an I/O stall, never a total-time bound
FINALIZE_TIMEOUT_S = 120
RETRY_S = 120.0                    # a failed carry waits this long, named, before the next attempt
PROGRESS_EVERY_S = 60.0
WORKERS = 2
_run = subprocess.run              # the subprocess runner, read at call time (tests replace it)


def caps_on(kcfg: Optional[Mapping] = None) -> bool:
    """``kanban.placement.carry_caps`` in the ROOT config (default off). On = the rule before t_f3a411c2: the carry
    runs in the tick, bounded by CARRY_MAX_BYTES / CARRY_MAX_ENTRIES. Unreadable config reads as the default."""
    if kcfg is None:
        try:
            from hermes_cli import kanban_db as _kdb
            kcfg = _kdb._board_home_kanban_cfg()
        except Exception:
            kcfg = {}
    placement = kcfg.get("placement") if isinstance(kcfg, Mapping) else None
    val = placement.get(CAPS_KNOB, False) if isinstance(placement, Mapping) else False
    if isinstance(val, str):
        return val.strip().lower() not in ("off", "false", "no", "0", "")
    return bool(val)


def staging_path(workspace: str) -> str:
    w = workspace.rstrip("/")
    return f"{os.path.dirname(w)}/.carry-{os.path.basename(w)}"


class CarryNoRoom(WorkspaceUnavailable, RuntimeError):
    """The pool host has less free space than the carry needs. Not the card's fault: the claim is released
    uncharged (a WorkspaceUnavailable), the card waits and retries."""

    def __init__(self, host: str, need: int, free: int, staged: int):
        super().__init__(f"No room on {host}: need {need}, free {free}")
        self.host, self.need, self.free, self.staged = host, need, free, staged


class CarryRefused(RuntimeError):
    """The files cannot travel as files (a git dir outside the workspace); the capped rule decides the card."""


def census(path: str) -> Tuple[int, int]:
    """``(bytes, entries)`` of ``path``: every entry below it, symlinks counted and never followed. No cap."""
    total = entries = 0
    pending = [path]
    while pending:
        current = pending.pop()
        if current != path and os.path.islink(current):
            continue
        with os.scandir(current) as scan:
            for entry in scan:
                entries += 1
                info = entry.stat(follow_symlinks=False)
                total += info.st_size
                if stat.S_ISDIR(info.st_mode):
                    pending.append(entry.path)
    return total, entries


def _ssh(host: PoolHost, script: str, runner: Optional[Callable], timeout: float):
    return (runner or _run)(_ssh_argv(host, script), capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)


def remote_space(host: PoolHost, dest: str, runner: Optional[Callable] = None) -> Tuple[int, int]:
    """``(free bytes on the filesystem holding dest's parent, bytes already in its staging dir)``."""
    p, s = shlex.quote(os.path.dirname(dest.rstrip("/")) or "/"), shlex.quote(staging_path(dest))
    proc = _ssh(host, f"f=$(df -Pk -- {p} | awk 'NR==2{{print $4}}') && [ -n \"$f\" ] || exit 1; u=0; "
                      f"if [ -d {s} ]; then u=$(du -sk -- {s} | awk '{{print $1}}'); fi; echo \"$f $u\"",
                runner, 30)
    out = (getattr(proc, "stdout", "") or "").split()
    if getattr(proc, "returncode", 1) != 0 or len(out) != 2 or not all(x.isdigit() for x in out):
        raise RuntimeError(f"carry to {host.name}: free space unreadable: "
                           f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")
    return int(out[0]) * 1024, int(out[1]) * 1024


def remove_staging(host: PoolHost, workspace: str, runner: Optional[Callable] = None) -> bool:
    proc = _ssh(host, f"rm -rf -- {shlex.quote(staging_path(workspace.rstrip('/')))}", runner, 300)
    return getattr(proc, "returncode", 1) == 0


class Job:
    """One background carry. ``phase``: queued, census, space, copy, copied. ``stats`` once copied."""

    def __init__(self, task_id: str, host: PoolHost, workspace: str, stale: Optional[PoolHost] = None):
        self.task_id, self.host, self.workspace, self.stale = task_id, host, workspace, stale
        self.phase = "queued"
        self.bytes: Optional[int] = None
        self.entries: Optional[int] = None
        self.t0 = time.monotonic()
        self.last_progress = self.t0
        self.staged_before = 0
        self.stats: Dict[str, object] = {}
        self.future = None
        self.stale_cleaned: Optional[bool] = None

    def elapsed(self) -> float:
        return time.monotonic() - self.t0


def run(job: Job, runner: Optional[Callable] = None) -> Dict[str, object]:
    """The whole carry up to (not including) the finalize; raises CarryRefused, CarryNoRoom or RuntimeError."""
    from hermes_cli import kanban_worker_hosts as _kwh
    runner = runner or _run
    t0, cpu0 = time.monotonic(), time.thread_time()
    ch0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    src = job.workspace.rstrip("/")
    if not _kwh.carry_path_safe(src):
        raise CarryRefused(f"{src!r} is not a shell-inert path")
    dest = src
    job.phase = "census"
    job.bytes, job.entries = census(src)
    with os.scandir(src) as scan:
        tops = sorted(e.name for e in scan if e.is_dir(follow_symlinks=False))
    lost = _kwh.carried_gitdir_loss(src, tops, ())
    if lost is not None:
        raise CarryRefused(lost)
    if job.stale is not None and job.stale.name != job.host.name:
        # a resumable staging dir an earlier attempt left on another host: that host is cleaned first
        job.stale_cleaned = remove_staging(job.stale, src, runner)
    job.phase = "space"
    _kwh.prepare_remote_workspace(job.host, dest, runner)
    free, staged = remote_space(job.host, dest, runner)
    job.staged_before = staged
    need = max(0, job.bytes - staged)
    if need > free:
        raise CarryNoRoom(job.host.name, need, free, staged)
    job.phase = "copy"
    ssh = _ssh_argv(job.host, "")[:-2]
    proc = runner(["rsync", "-a", "--partial", "--delete", f"--timeout={RSYNC_IO_TIMEOUT_S}",
                   "-e", " ".join(shlex.quote(a) for a in ssh), "--", f"{src}/",
                   f"{job.host.target}:{staging_path(dest)}/"],
                  capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"rsync rc={getattr(proc, 'returncode', '?')}: "
                           f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")
    ch1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    job.phase = "copied"
    job.stats = {"bytes": job.bytes, "entries": job.entries, "seconds": round(time.monotonic() - t0, 2),
                 "cpu_s": round((time.thread_time() - cpu0) + (ch1.ru_utime - ch0.ru_utime)
                                + (ch1.ru_stime - ch0.ru_stime), 2),
                 "staging": staging_path(dest)}
    return job.stats


def finalize(host: PoolHost, workspace: str, runner: Optional[Callable] = None) -> None:
    """Move the finished staging dir onto the workspace path on ``host`` (one ssh call). An empty or absent
    workspace is replaced whole; one that already holds files gets the staging copied over it (never deleted
    from, as before). Raises when the staging dir is missing or the move fails (the staging dir stays)."""
    dest = workspace.rstrip("/")
    s, w = shlex.quote(staging_path(dest)), shlex.quote(dest)
    proc = _ssh(host, f"[ -d {s} ] || exit 4; if rmdir -- {w} 2>/dev/null || [ ! -e {w} ]; then mv -- {s} {w}; "
                      f"else cp -a -- {s}/. {w}/ && rm -rf -- {s}; fi", runner, FINALIZE_TIMEOUT_S)
    if getattr(proc, "returncode", 1) != 0:
        raise RuntimeError(f"carry to {host.name}: finalize rc={getattr(proc, 'returncode', '?')}: "
                           f"{(getattr(proc, 'stderr', '') or '').strip()[:200]}")


# -- registry: one job per card, visible to every tick of this process; a flock per (host, path) for the others --

_lock = threading.Lock()
_jobs: Dict[str, Tuple[Job, Optional[int]]] = {}       # task id -> (job, lock fd)
_failed: Dict[str, Tuple[float, str]] = {}             # task id -> (monotonic, wait reason) for RETRY_S
_pool: Optional[ThreadPoolExecutor] = None


def lock_path(lock_dir: Path, host: str, workspace: str) -> Path:
    import hashlib
    return Path(lock_dir) / (hashlib.sha1(f"{host}\0{workspace}".encode()).hexdigest()[:24] + ".lock")


def _try_lock(path: Path) -> Optional[int]:
    from hermes_cli.kanban_worktree_port import _try_lock as _tl
    return _tl(path)


def _unlock(fd: Optional[int]) -> None:
    from hermes_cli.kanban_worktree_port import _unlock as _ul
    _ul(fd)


def wait_reason(exc: BaseException, host: str) -> str:
    if isinstance(exc, CarryNoRoom):
        return f"{NO_ROOM}:{host}: {exc}"
    return f"{FAILED}:{host}: {exc}"


def failure_state(job: Job, exc: BaseException) -> str:
    """What the failure left on the pool host: ``refused`` (nothing copied, the capped rule decides), ``clean``
    (nothing of this card on the host) or ``resumable`` (a staging dir the next attempt there resumes)."""
    if isinstance(exc, CarryRefused):
        return "refused"
    if job.phase in ("queued", "census"):
        return "clean"
    if job.phase == "space" and not job.staged_before:
        return "clean"
    return "resumable"


def poll(task_id: str, host: PoolHost, workspace: str, lock_dir: Path, *, start: bool = True,
         stale: Optional[PoolHost] = None, runner: Optional[Callable] = None) -> Tuple[str, object]:
    """One tick's look at the card's carry: ``(state, detail)``.

    * ``started``: no job was running; one was submitted now (detail: the Job);
    * ``running``: the job is still copying (detail: the Job);
    * ``done``: the copy finished (detail: the Job; :func:`take` collects it at the claim);
    * ``failed``: the job raised (detail: ``(job, exc)``); the card backs off for RETRY_S;
    * ``backoff``: an earlier failure is younger than RETRY_S (detail: its wait reason);
    * ``busy``: another dispatcher holds this carry's lock, or this card's job runs to another host;
    * ``none``: nothing recorded and ``start`` is False."""
    global _pool
    now = time.monotonic()
    with _lock:
        hit = _jobs.get(task_id)
        if hit is not None and (hit[0].host.name != host.name or hit[0].workspace != workspace):
            if not hit[0].future.done():
                return "busy", hit[0]
            # placed elsewhere since: that host's staging dir (finished or not) is removed by the next carry
            stale = stale or hit[0].host
            del _jobs[task_id]
            hit = None
        if hit is not None:
            job, fd = hit
            if not job.future.done():
                return "running", job
            exc = job.future.exception()
            if exc is None:
                return "done", job
            del _jobs[task_id]
            _failed[task_id] = (now, wait_reason(exc, host.name))
            return "failed", (job, exc)
        seen = _failed.get(task_id)
        if seen is not None:
            if now - seen[0] < RETRY_S:
                return "backoff", seen[1]
            del _failed[task_id]
        if not start:
            return "none", None
        fd = _try_lock(lock_path(lock_dir, host.name, workspace))
        if fd is None:
            return "busy", None
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="kanban-carry")
        job = Job(task_id, host, workspace, stale)
        try:
            job.future = _pool.submit(run, job, runner)
        except BaseException:
            _unlock(fd)
            raise
        job.future.add_done_callback(lambda _f, _fd=fd: _unlock(_fd))
        _jobs[task_id] = (job, fd)
        return "started", job


def take(task_id: str, host: PoolHost, workspace: str) -> Optional[Job]:
    """At the claim: the finished job for this card (removed from the registry), or None. Re-raises its error."""
    with _lock:
        hit = _jobs.get(task_id)
        if hit is None or hit[0].host.name != host.name or hit[0].workspace != workspace \
                or not hit[0].future.done():
            return None
        del _jobs[task_id]
    hit[0].future.result()
    return hit[0]


def run_sync(job: Job, lock_dir: Path, runner: Optional[Callable] = None) -> bool:
    """A one-shot (CLI) tick: the same carry, run here under the same lock. False (nothing run) while another
    dispatcher holds it; raises what :func:`run` raises."""
    fd = _try_lock(lock_path(lock_dir, job.host.name, job.workspace))
    if fd is None:
        return False
    try:
        run(job, runner)
    finally:
        _unlock(fd)
    return True


def progress_due(job: Job) -> bool:
    now = time.monotonic()
    if now - job.last_progress >= PROGRESS_EVERY_S:
        job.last_progress = now
        return True
    return False


def progress_payload(job: Job) -> dict:
    return {"host": job.host.name, "path": job.workspace, "phase": job.phase, "bytes": job.bytes,
            "entries": job.entries, "elapsed_s": round(job.elapsed(), 1)}


# -- the card's carry history (DB): what binds the card to a host while a carry is in flight --------------------

_KINDS = (STARTED_EVENT, FAILED_EVENT)


def _last(conn, task_id: str, workspace: Optional[str]) -> Optional[Tuple[str, dict]]:
    """The newest started / failed / carried event of this card for ``workspace``, as (kind, payload)."""
    from hermes_cli.kanban_worker_hosts import CARRIED_EVENT
    row = conn.execute("SELECT kind, payload FROM task_events WHERE task_id=? AND kind IN (?,?,?) "
                       "ORDER BY id DESC LIMIT 1", (task_id, *_KINDS, CARRIED_EVENT)).fetchone()
    if row is None:
        return None
    try:
        doc = json.loads(row[1] or "{}")
    except (TypeError, ValueError):
        return None
    if workspace is None or doc.get("path") != workspace:
        return None
    return row[0], doc


def inflight_host(task_id: str, workspace: Optional[str]) -> Optional[str]:
    """The host this process's carry of the card is copying to or has finished copying to: the card is pinned
    there until it is claimed. None when no such job (a restarted dispatcher places afresh; :func:`stale_host`
    then names the host whose staging dir the next carry removes)."""
    with _lock:
        hit = _jobs.get(task_id)
    if hit is None or hit[0].workspace != workspace:
        return None
    return hit[0].host.name


def refused(conn, task_id: str, workspace: Optional[str]) -> bool:
    """True when the newest carry attempt of this card refused to carry its files (``state: refused``)."""
    last = _last(conn, task_id, workspace)
    return last is not None and last[0] == FAILED_EVENT and last[1].get("state") == "refused"


def stale_host(conn, task_id: str, workspace: Optional[str], hosts: Mapping[str, PoolHost],
               host: str) -> Optional[PoolHost]:
    """The pool host that may hold a staging dir of this card from an earlier attempt (one that failed
    ``resumable``, or one started and never finished: a dispatcher restart), when that is not ``host``. The next
    carry removes it there first."""
    last = _last(conn, task_id, workspace)
    if last is None or not (last[0] == STARTED_EVENT
                            or (last[0] == FAILED_EVENT and last[1].get("state") == "resumable")):
        return None
    name = last[1].get("host")
    if not isinstance(name, str) or name.lower() == host.lower():
        return None
    return next((h for n, h in hosts.items() if n.lower() == name.lower()), None)


def reset() -> None:
    """Tests: forget every job and backoff."""
    with _lock:
        _jobs.clear()
        _failed.clear()
