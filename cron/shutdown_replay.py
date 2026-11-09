"""Replay a one-shot cron run that a gateway stop interrupted, once, on the next boot (t_b5c315bb).

The decision is made at stop time, by the process that knows the run did not finish. A reader
after a crash cannot know whether the work survived, so it never replays.

1. Mark at stop. ``mark_replay_pending`` runs from the gateway's shutdown sweep
   (``scheduler.mark_running_jobs_interrupted``) once the cron drain budget expired with the run
   still in flight. One fenced ``save_jobs`` writes the ``replay_pending`` runtime row
   (job_id, run_id, reason, boot_id, marked_at), releases this process's fire/run claims and
   gives back the ``repeat.completed`` slot the dispatch consumed.
2. Fire once on boot. ``consume_replay_markers`` (after execution recovery) pops every marker under ``_jobs_lock`` in one save
   and stamps a run-now (``next_run_at == manual_run_at``) for the fresh ones, so one marker
   gives at most one fire. A marker whose execution reached a real terminal state is recorded
   and cleared instead. A stale marker (older than the one-shot catch-up window, capped at 6 h)
   is cleared and paged once.
3. Only gateway-owned agent work is marked. Script-lane (``no_agent``) jobs are not: the script
   runner owns their lifecycle and the child may outlive the gateway.
4. A hard kill (SIGKILL, OOM, power loss) has no drain and no marker. At boot a claim whose owner
   process on this host is gone, and which has no marker, is released and paged once. It is
   never replayed (``release_orphaned_claims``, before execution recovery).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

REPLAY_PENDING_KEY = "replay_pending"
REASON = "shutdown_interrupt"
# Freshness cap: a marker older than this is never replayed (Ace ruling C, item 3).
MAX_AGE_CAP_S = 6 * 3600
# The error ``scheduler._finish_interrupted_run`` writes for a run that was still going when the
# shutdown sweep marked it. A failed row with this error is the interruption itself, not a
# terminal outcome of the run, so the marker still replays.
INTERRUPTED_ERROR = "Interrupted by gateway shutdown before terminal completion."
# Same sweep, but the run had already delivered its result: a terminal outcome, never replayed.
INTERRUPTED_AFTER_DELIVERY_ERROR = "Interrupted by gateway shutdown after its result was delivered."
PRODUCER = "cron-shutdown-replay"


def _boot_id() -> str:
    try:
        from gateway.status import get_current_boot_id

        return str(get_current_boot_id())
    except Exception:
        return f"{os.getpid()}:"


def _is_once(job: Dict[str, Any]) -> bool:
    schedule = job.get("schedule")
    return isinstance(schedule, dict) and schedule.get("kind") == "once"


def _is_script_lane(job: Dict[str, Any]) -> bool:
    from cron.lanes import job_lane

    return job_lane(job) == "script"


def _rerun_command(job: Dict[str, Any]) -> str:
    return f"hermes cron resume {job.get('id')} --run-now"


def mark_replay_pending(
    job_id: str, *, fire_owner: str, run_id: Optional[str], lock_timeout: Optional[float] = None,
) -> bool:
    """Mark one interrupted one-shot run for a single replay on the next boot.

    True when the marker was written. False (caller falls back to the plain interrupted mark)
    for a recurring or script-lane job, a run whose fire claim is no longer ``fire_owner``'s
    (it already recorded its outcome, or another owner took it), a job whose one replay is
    already spent, or a fence that could not be taken within ``lock_timeout``.
    """
    from cron import jobs as J

    if not fire_owner:
        return False

    def apply(jobs, _i, job):
        if not _is_once(job) or _is_script_lane(job):
            return False
        claim = job.get("fire_claim")
        if not isinstance(claim, dict) or claim.get("by") != fire_owner:
            return False  # mark_job_run already ran for this run, or the claim moved on
        if int(job.get(J.RESTART_REQUEUE_COUNT_KEY) or 0) >= J.RESTART_REQUEUE_MAX:
            logger.warning(
                "cron.shutdown_replay.spent job='%s' id=%s: already replayed once without a "
                "clean run; not marking again", job.get("name", job_id), job_id)
            return False
        job[REPLAY_PENDING_KEY] = {
            "job_id": job_id,
            "run_id": str(run_id) if run_id else None,
            "reason": REASON,
            "boot_id": _boot_id(),
            "marked_at": J._hermes_now().isoformat(),
        }
        job["fire_claim"] = None
        if job.get("run_claim") is not None:
            job["run_claim"] = None
        job.pop("pending_slot", None)
        repeat = job.get("repeat")
        if isinstance(repeat, dict):
            times = repeat.get("times")
            completed = int(repeat.get("completed") or 0)
            if isinstance(times, int) and times > 0 and completed > 0:
                repeat["completed"] = completed - 1
        J.save_jobs(jobs)
        return True

    def locked():
        return J._with_job(job_id, apply, False)

    marked = bool(J._under_fire_fence(job_id, locked, timeout=lock_timeout))
    if marked:
        logger.warning(
            "cron.shutdown_replay.marked id=%s run_id=%s: one-shot interrupted by gateway "
            "shutdown; replays once on the next boot", job_id, run_id)
    return marked


def _parse(ts: Any) -> Optional[datetime]:
    from cron.jobs import _ensure_aware

    try:
        return _ensure_aware(datetime.fromisoformat(str(ts)))
    except (TypeError, ValueError):
        return None


def _max_age_s() -> float:
    from cron.jobs import _oneshot_catchup_seconds

    try:
        grace = float(_oneshot_catchup_seconds())
    except Exception:
        grace = float(MAX_AGE_CAP_S)
    return min(grace, float(MAX_AGE_CAP_S))


def _terminal_outcome(run_id: Optional[str]) -> Optional[Tuple[bool, Optional[str]]]:
    """``(success, error)`` when the marked run reached its own terminal state, else None.

    ``completed`` and a ``failed`` row with any error other than the shutdown interruption are
    the run's real outcome. ``unknown`` (startup recovery of the dead owner's row), a ``failed``
    row carrying the interruption error, and a missing or still-open row are not.
    """
    if not run_id:
        return None
    from cron.executions import get_execution

    try:
        row = get_execution(run_id)
    except Exception:
        logger.warning("cron.shutdown_replay: execution %s unreadable", run_id, exc_info=True)
        return None
    if not row:
        return None
    status = row.get("status")
    if status == "completed":
        return True, None
    if status == "failed" and (row.get("error") or "") != INTERRUPTED_ERROR:
        return False, row.get("error") or "unknown failure"
    return None


def _all_claims_dead(job: Dict[str, Any]) -> bool:
    from cron.jobs import _claim_owner_is_dead

    claims = [c for c in (job.get("fire_claim"), job.get("run_claim")) if c is not None]
    if not claims:
        return False
    return all(isinstance(c, dict) and _claim_owner_is_dead(c) for c in claims)


def _execution_live(job_id: str) -> bool:
    """True when the job's newest attempt is still claimed/running under a live owner (a
    restart-safe worker can outlive the gateway whose pid is on the claim). An unreadable
    ledger counts as live: inability to prove death must not release anything."""
    from cron.executions import _owner_is_live, latest_execution

    try:
        row = latest_execution(job_id)
    except Exception:
        return True
    if not row or row.get("status") not in ("claimed", "running"):
        return False
    try:
        return _owner_is_live(int(row.get("pid")), row.get("process_started_at"))
    except (TypeError, ValueError):
        return True


def release_orphaned_claims() -> List[str]:
    """Boot pass BEFORE execution recovery: release hard-kill claims and page once each.

    A claim whose every owner is a process on this host that no longer exists, on an agent-lane
    job with no ``replay_pending`` marker, was left by a stop that never drained (SIGKILL, OOM,
    power). Nothing can tell whether that run's work survived, so the claim is released and
    paged, never replayed. Runs before recovery because recovery's reconcile clears these claims
    itself (``mark_job_run``) and would hide the episode.
    """
    from cron import jobs as J

    released: List[str] = []
    pages: List[Tuple[str, str]] = []
    with J._jobs_lock():
        jobs = J.load_jobs()
        for job in jobs:
            job_id = str(job.get("id") or "")
            if job.get(REPLAY_PENDING_KEY) or _is_script_lane(job) or not _all_claims_dead(job):
                continue
            if _execution_live(job_id):
                continue
            name = job.get("name") or job_id
            owner = (job.get("fire_claim") or job.get("run_claim") or {}).get("by")
            job["fire_claim"] = None
            if job.get("run_claim") is not None:
                job["run_claim"] = None
            job.pop("pending_slot", None)
            released.append(job_id)
            logger.warning(
                "cron.shutdown_replay.orphaned_claim job='%s' id=%s owner=%s: claim owner is "
                "gone and left no replay marker (hard kill); released, not replayed",
                name, job_id, owner)
            pages.append((f"orphaned-{job_id}", (
                f"cron job '{name}' ({job_id}) was claimed by {owner}, which died without a "
                f"shutdown drain (hard kill). Its claim was released; the run was NOT replayed "
                f"because nothing can tell whether it finished. If it did not, re-run it: "
                f"{_rerun_command(job)}")))
        if released:
            J.save_jobs(jobs)
    for key, text in pages:
        page_alerts(key, text)
    return released


def consume_replay_markers() -> Dict[str, List[str]]:
    """Boot pass AFTER execution recovery: consume every ``replay_pending`` marker once.

    Returns ``{"replayed", "terminal", "stale"}`` job-id lists. Every marker is popped in one
    ``save_jobs`` under ``_jobs_lock``, so two concurrent boots cannot both stamp the same
    replay. Pages go out after the write.
    """
    from cron import jobs as J

    out: Dict[str, List[str]] = {"replayed": [], "terminal": [], "stale": []}
    pages: List[Tuple[str, str]] = []
    with J._jobs_lock():
        jobs = J.load_jobs()
        now = J._hermes_now()
        changed = False
        for job in jobs:
            marker = job.get(REPLAY_PENDING_KEY)
            if not marker:
                continue
            changed = True
            job.pop(REPLAY_PENDING_KEY, None)
            job_id = str(job.get("id") or "")
            name = job.get("name") or job_id
            run_id = marker.get("run_id") if isinstance(marker, dict) else None
            outcome = _terminal_outcome(run_id)
            if outcome is not None:
                success, error = outcome
                stamp = now.isoformat()
                J._record_run_outcome(job, success, error, None, None, stamp)
                J._advance_after_run(job, stamp)
                out["terminal"].append(job_id)
                logger.warning(
                    "cron.shutdown_replay.terminal job='%s' id=%s run_id=%s: run reached a "
                    "terminal state (%s); recorded, not replayed",
                    name, job_id, run_id, "ok" if success else "failed")
                continue
            marked_at = _parse(marker.get("marked_at")) if isinstance(marker, dict) else None
            age = (now - marked_at).total_seconds() if marked_at else None
            if age is None or age < 0 or age > _max_age_s():
                out["stale"].append(job_id)
                logger.warning(
                    "cron.shutdown_replay.stale job='%s' id=%s run_id=%s age=%s: marker past "
                    "its freshness window; cleared, not replayed", name, job_id, run_id,
                    None if age is None else int(age))
                pages.append((f"stale-{job_id}", (
                    f"cron one-shot '{name}' ({job_id}) was interrupted by a gateway shutdown, "
                    f"but the next boot came after its replay window "
                    f"({int(_max_age_s() // 60)} min), so it was not re-run. "
                    f"Re-run it by hand: {_rerun_command(job)}")))
                continue
            stamp = now.isoformat()
            job["next_run_at"] = stamp
            job["manual_run_at"] = stamp
            job[J.RESTART_REQUEUE_COUNT_KEY] = int(job.get(J.RESTART_REQUEUE_COUNT_KEY) or 0) + 1
            out["replayed"].append(job_id)
            logger.warning(
                "cron.shutdown_replay.fire job='%s' id=%s run_id=%s: run interrupted by "
                "gateway shutdown; firing once now", name, job_id, run_id)
        if changed:
            J.save_jobs(jobs)
    for key, text in pages:
        page_alerts(key, text)
    return out


def page_alerts(key: str, text: str) -> bool:
    """Page #alerts once for this episode via scripts/lib/episode_pager.py (detached)."""
    from cron.script_runner_rearm import _detach

    return _detach(["page", "--producer", PRODUCER, "--key", key, "--text", text],
                   "scripts/lib/episode_pager.py", "#alerts page")
