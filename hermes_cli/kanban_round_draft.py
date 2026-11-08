"""Round draft (t_c418eebc): the WORKER owns its PR's draft bit for a whole fix round.

Goal: one CI run per round, not one per push. The draft lane skips CI on draft PRs, so:

* Round start (:func:`draft_at_round_start`, called once at worker boot, before the first turn and so before
  any push): for each PR this card's earlier handback flipped ready (``handoff_draft_flipped`` events = the
  receipt naming card + PR) that is still OPEN, ready and on a branch naming this card, run
  ``gh pr ready --undo``, persist the mark on the card (``round_draft_marked`` event: ``{"o/r#n": {"run",
  "head", "at"}}``, run id from the worker's own env) and post ``round-draft: drafted by worker run <id> at
  <head>`` as a human-readable receipt only. A failure is logged and the run continues. Never retried.
* Round end: the handback (:mod:`kanban_pr_freshness`) un-drafts once through ``ace_gate.py ready``. A draft at
  the recorded head is normally an operator hold; :func:`round_marked` lets the handback tell this worker-round
  draft from that hold, from the PERSISTED mark only (Prism 852b8897186d: a PR comment anyone can post is not
  provenance).
* A run that dies mid-round leaves the marked draft; the next run of the same card sees a draft and does
  nothing (no second mark), and its handback adopts it through the same mark.

Nothing else drafts for a round: the Prism findings-route only sends the card back. Kill switch
``KANBAN_ROUND_DRAFT=0`` (no draft, no mark, the handback treats a same-head draft as a hold, as before).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Callable, Iterable, Optional

_log = logging.getLogger(__name__)

MARK_PREFIX = "round-draft: drafted by worker run "

DraftFn = Callable[[str, int], bool]          # (repo, number) -> drafted
CommentFn = Callable[[str, int, str], bool]   # (repo, number, body) -> posted


def enabled() -> bool:
    return os.environ.get("KANBAN_ROUND_DRAFT", "1").strip() != "0"


def mark_line(run_id, head: str) -> str:
    return f"{MARK_PREFIX}{run_id} at {head}"


def flip_records(conn, task_id: str) -> dict:
    """This card's persisted ready flips ``{"o/r#n": {"head", "at"}}`` (written only by the kernel)."""
    out: dict = {}
    for (payload,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'handoff_draft_flipped' ORDER BY id",
        (task_id,),
    ).fetchall():
        try:
            out.update((json.loads(payload) or {}).get("flipped") or {})
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def card_run_ids(conn, task_id: str) -> set:
    return {str(r[0]) for r in conn.execute("SELECT id FROM task_runs WHERE task_id = ?", (task_id,)).fetchall()}


def mark_records(conn, task_id: str) -> dict:
    """This card's persisted round-draft marks ``{"o/r#n": {"run", "head", "at"}}`` (latest per PR)."""
    out: dict = {}
    for (payload,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'round_draft_marked' ORDER BY id",
        (task_id,),
    ).fetchall():
        try:
            out.update((json.loads(payload) or {}).get("marked") or {})
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def round_marked(mark, head: str, record, own_runs: Optional[Iterable] = None) -> bool:
    """An unconsumed mark from an own run, not older than the last flip.
    The authenticated transition (checked separately) survives WIP head changes; timestamps alone prove nothing.
    Missing/malformed mark or record, or the kill switch -> False (the draft stays an operator hold)."""
    if not enabled() or not head or not isinstance(mark, dict) or not isinstance(record, dict):
        return False
    if own_runs is None or str(mark.get("run") or "") not in {str(r) for r in own_runs}:
        return False
    try:
        return not mark.get("consumed") and float(mark.get("at") or 0) >= float(record.get("at") or 0)
    except (TypeError, ValueError):
        return False


def gh_snapshot(path: str):
    """Pinned REST reads; timeline pagination is complete before any ownership decision."""
    timeline = "/timeline?" in path
    argv = ["gh", "api", "--hostname", "github.com"]
    if timeline:
        argv += ["--paginate", "--slurp"]
    try:
        proc = subprocess.run(argv + [path], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60,
                              check=False, env=dict(os.environ, GH_HOST="github.com"))
        if proc.returncode:
            return None
        data = json.loads(proc.stdout)
        return {"pages": data} if timeline else data
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def latest_transition(gh, repo: str, number: int):
    data = gh(f"repos/{repo}/issues/{number}/timeline?per_page=100")
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list) or not all(isinstance(p, list) for p in pages):
        return None
    events = [e for page in pages for e in page if isinstance(e, dict)
              and e.get("event") in {"convert_to_draft", "ready_for_review"}]
    return events[-1] if events else None


def current_transition(mark, gh, repo: str, number: int) -> bool:
    if not isinstance(mark, dict) or mark.get("consumed") or not mark.get("transition_id") or not mark.get("actor_id"):
        return False
    event = latest_transition(gh, repo, number)
    return bool(event and event.get("event") == "convert_to_draft"
                and event.get("id") == mark["transition_id"]
                and (event.get("actor") or {}).get("id") == mark["actor_id"])


def gh_draft(repo: str, number: int) -> bool:
    try:
        proc = subprocess.run(["gh", "pr", "ready", str(number), "--repo", f"https://github.com/{repo}", "--undo"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, check=False, env=dict(os.environ, GH_HOST="github.com"))
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("round draft: gh pr ready --undo %s#%s failed: %s", repo, number, exc)
        return False
    if proc.returncode:
        _log.warning("round draft: gh pr ready --undo %s#%s rc=%s: %s", repo, number, proc.returncode,
                     (proc.stderr or "").strip()[:300])
    return proc.returncode == 0


def gh_comment(repo: str, number: int, body: str) -> bool:
    try:
        proc = subprocess.run(["gh", "api", "--hostname", "github.com", "-X", "POST", f"repos/{repo}/issues/{number}/comments",
                               "-f", f"body={body}"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, check=False, stdin=subprocess.DEVNULL,
                              env=dict(os.environ, GH_HOST="github.com"))
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("round draft: mark on %s#%s failed: %s", repo, number, exc)
        return False
    if proc.returncode:
        _log.warning("round draft: mark on %s#%s rc=%s: %s", repo, number, proc.returncode,
                     (proc.stderr or "").strip()[:300])
    return proc.returncode == 0


def draft_at_round_start(task_id: str, run_id, records: dict, *, gh=None, draft: Optional[DraftFn] = None,
                         comment: Optional[CommentFn] = None,
                         persist: Optional[Callable[[str, dict], None]] = None) -> dict:
    """Draft this card's own ready PRs before the round's first push. Returns ``{"o/r#n": outcome}``.

    ``persist(key, {"run", "head", "at"})`` records the mark on the card right after a successful draft (the only
    provenance :func:`round_marked` accepts). Never raises: every failure is logged and the run continues."""
    from hermes_cli import kanban_pr_freshness as fr
    from hermes_cli.kanban_open_pr import branch_names_card, extract_pr_refs, is_fleet_ref
    report: dict = {}
    if not enabled() or not fr.enabled() or not fr.worker_draft_flip_enabled() or not task_id or not run_id or not records:
        return report
    if gh is None:
        gh = None if os.environ.get("PYTEST_CURRENT_TEST") else gh_snapshot
        if gh is None:
            return report
    draft = draft or gh_draft
    comment = comment or gh_comment
    repos = fr.worker_draft_repos()
    for key in sorted(records):
        refs = [r for r in extract_pr_refs(key) if is_fleet_ref(r)]
        if not refs:
            continue
        ref = refs[0]
        if ref.repo.casefold() not in repos:
            report[key] = "skip: not a worker-draft repo"
            continue
        pr = gh(f"repos/{ref.repo}/pulls/{ref.number}")
        if not isinstance(pr, dict):
            report[key] = "skip: PR unreadable"
            _log.warning("round draft: %s unreadable; round runs CI per push", key)
            continue
        head = str(((pr.get("head") or {}).get("sha")) or "")
        if pr.get("state") != "open" or not head:
            report[key] = "skip: not open"
            continue
        if not branch_names_card(str(((pr.get("head") or {}).get("ref")) or ""), task_id):
            report[key] = "skip: branch does not name this card"
            continue
        if pr.get("draft") is True:
            report[key] = "skip: already draft"   # a dead run's marked draft (adopted) or someone else's hold
            continue
        actor = gh("user")
        before = latest_transition(gh, ref.repo, ref.number)
        if not isinstance(actor, dict) or not actor.get("id"):
            report[key] = "skip: authenticated actor unavailable; operator handback required"
            continue
        if not draft(ref.repo, ref.number):
            report[key] = "draft failed (logged; round runs CI per push)"
            continue
        transition = latest_transition(gh, ref.repo, ref.number)
        if (not transition or transition.get("event") != "convert_to_draft" or not transition.get("id")
                or (before and transition.get("id") == before.get("id"))
                or (transition.get("actor") or {}).get("id") != actor["id"]):
            report[key] = "drafted; transition ownership unproved; operator handback required"
            _log.warning("round draft: %s %s", key, report[key])
            continue
        if persist is not None:
            try:
                persist(key, {"run": str(run_id), "head": head, "at": time.time(),
                              "transition_id": transition["id"], "actor_id": actor["id"]})
            except Exception:  # noqa: BLE001 -- unpersisted mark = the handback treats the draft as a hold
                _log.warning("round draft: persisting mark for %s failed", key, exc_info=True)
        posted = comment(ref.repo, ref.number, mark_line(run_id, head))
        report[key] = "drafted + marked" if posted else "drafted; mark failed"
        _log.info("round draft: %s %s at %s by run %s", key, report[key], head[:12], run_id)
    return report


def run_from_env() -> dict:
    """Worker boot entry: the dispatcher-owned worker drafts its card's PRs. Never raises."""
    try:
        from agent.delegation_context import owned_kanban_task
        task_id = owned_kanban_task()
        run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
        if not task_id or not run_id.isdigit() or not enabled():
            return {}
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db as _kb
        with _kbc.connect_closing() as conn:
            task = _kb.get_task(conn, task_id)
            claim = _kb._latest_event(conn, task_id, "claimed", int(run_id))
            if (task is None or task.status != "running" or str(task.current_run_id) != run_id or claim is None
                    or _kb._retry_status_for_run(conn, task_id, int(run_id)) == "review"):
                return {}
            records = flip_records(conn, task_id)

        def persist(key: str, mark: dict) -> None:
            from hermes_cli import kanban_db as _kb
            with _kbc.connect_closing() as conn:
                with _kb.write_txn(conn):
                    _kb._append_event(conn, task_id, "round_draft_marked", {"marked": {key: mark}},
                                      run_id=int(run_id))

        return draft_at_round_start(task_id, run_id, records, persist=persist)
    except Exception:  # noqa: BLE001 -- a round-start draft must never stop a worker
        _log.warning("round draft at worker start failed", exc_info=True)
        return {}
