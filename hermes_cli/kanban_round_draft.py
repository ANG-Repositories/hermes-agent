"""Round draft (t_c418eebc): the WORKER owns its PR's draft bit for a whole fix round.

Goal: one CI run per round, not one per push. The draft lane skips CI on draft PRs, so:

* Round start (:func:`draft_at_round_start`, called once at worker boot, before the first turn and so before
  any push): for the ONE PR whose head branch is the branch this run's workspace checks out, that this card's
  earlier handback flipped ready (``handoff_draft_flipped`` events = the receipt naming card + PR) and that is
  still OPEN and ready, persist the mark on the card FIRST (``round_draft_marked`` event: ``{"o/r#n": {"run",
  "head", "at"}}``, run id from the worker's own env), then run ``gh pr ready --undo`` and post ``round-draft:
  drafted by worker run <id> at <head>`` as a human-readable receipt only. No mark written = no draft
  (t_d3da1a4e: a draft without its record strands the PR as somebody else's hold). A failed draft voids the
  mark. A failure is logged and the run continues. Never retried.
* Round end: the handback (:mod:`kanban_pr_freshness`) un-drafts once through ``ace_gate.py ready``. A draft at
  the recorded head is normally an operator hold; :func:`round_marked` lets the handback tell this worker-round
  draft from that hold, from the PERSISTED mark only (Prism 852b8897186d: a PR comment anyone can post is not
  provenance), and only at the head the mark was written for.
* A run that dies mid-round leaves the marked draft; the next run of the same card sees a draft and does
  nothing (no second mark), and its handback adopts it through the same mark.

Nothing else drafts for a round: the Prism findings-route only sends the card back. OFF by default
(t_d3da1a4e: 0 completed cycles in 2 days, one stranded draft per send-back round); ``KANBAN_ROUND_DRAFT=1``
re-enables. Off = no draft, no mark, the handback treats a same-head draft as a hold.
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
    return os.environ.get("KANBAN_ROUND_DRAFT", "0").strip() == "1"


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
    """An unconsumed mark from an own run, written for THIS head, not older than the last flip.
    Missing/malformed mark or record, or the switch off -> False (the draft stays an operator hold)."""
    if not enabled() or not head or not isinstance(mark, dict) or not isinstance(record, dict):
        return False
    if own_runs is None or str(mark.get("run") or "") not in {str(r) for r in own_runs}:
        return False
    if mark.get("consumed") or str(mark.get("head") or "") != head:
        return False
    try:
        return float(mark.get("at") or 0) >= float(record.get("at") or 0)
    except (TypeError, ValueError):
        return False


def gh_snapshot(path: str):
    """Pinned, floor-bypassing REST read (GH_SHIM_FRESH: a 30 s cached copy is not the PR's state)."""
    try:
        proc = subprocess.run(["gh", "api", "--hostname", "github.com", path], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                              check=False, env=dict(os.environ, GH_HOST="github.com", GH_SHIM_FRESH="1"))
        if proc.returncode:
            return None
        return json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


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


def draft_at_round_start(task_id: str, run_id, records: dict, *, branch: str = "", gh=None,
                         draft: Optional[DraftFn] = None, comment: Optional[CommentFn] = None,
                         persist: Optional[Callable[[str, dict], None]] = None) -> dict:
    """Draft the one PR on ``branch`` (the branch this run checks out). Returns ``{"o/r#n": outcome}``.

    ``persist(key, {"run", "head", "at"})`` records the mark on the card BEFORE the draft (the only provenance
    :func:`round_marked` accepts); no ``persist``, or a raising one, means no draft. Never raises."""
    from hermes_cli import kanban_pr_freshness as fr
    from hermes_cli.kanban_open_pr import branch_names_card, extract_pr_refs, is_fleet_ref
    report: dict = {}
    if (not enabled() or not fr.enabled() or not fr.worker_draft_flip_enabled() or not task_id or not run_id
            or not records or not branch or persist is None):
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
        head_ref = str(((pr.get("head") or {}).get("ref")) or "")
        if pr.get("state") != "open" or not head:
            report[key] = "skip: not open"
            continue
        if head_ref != branch:
            report[key] = "skip: not this run's branch"   # a sibling PR of the card is never touched
            continue
        if not branch_names_card(head_ref, task_id):
            report[key] = "skip: branch does not name this card"
            continue
        if pr.get("draft") is True:
            report[key] = "skip: already draft"   # a dead run's marked draft (adopted) or someone else's hold
            continue
        mark = {"run": str(run_id), "head": head, "at": time.time()}
        try:
            persist(key, mark)
        except Exception:  # noqa: BLE001 -- no record, no draft (t_d3da1a4e)
            report[key] = "skip: mark not persisted; not drafted"
            _log.warning("round draft: persisting mark for %s failed; not drafting", key, exc_info=True)
            continue
        if not draft(ref.repo, ref.number):
            report[key] = "draft failed (logged; round runs CI per push)"
            try:
                persist(key, dict(mark, consumed=True))   # void: the mark must never adopt a later draft
            except Exception:  # noqa: BLE001 -- an unvoided mark still needs this head + an own-run draft
                _log.warning("round draft: voiding mark for %s failed", key, exc_info=True)
            continue
        posted = comment(ref.repo, ref.number, mark_line(run_id, head))
        report[key] = "drafted + marked" if posted else "drafted + marked; receipt comment failed"
        _log.info("round draft: %s %s at %s by run %s", key, report[key], head[:12], run_id)
    return report


def _workspace_branch() -> str:
    ws = (os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if not ws:
        return ""
    try:
        proc = subprocess.run(["git", "-C", ws, "rev-parse", "--abbrev-ref", "HEAD"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    out = (proc.stdout or "").strip()
    return "" if proc.returncode or out == "HEAD" else out


def run_from_env() -> dict:
    """Worker boot entry: the dispatcher-owned worker drafts its card's PR. Never raises."""
    try:
        if not enabled():
            return {}
        from agent.delegation_context import owned_kanban_task
        task_id = owned_kanban_task()
        run_id = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
        branch = _workspace_branch()
        if not task_id or not run_id.isdigit() or not branch:
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

        return draft_at_round_start(task_id, run_id, records, branch=branch, persist=persist)
    except Exception:  # noqa: BLE001 -- a round-start draft must never stop a worker
        _log.warning("round draft at worker start failed", exc_info=True)
        return {}


def finalize_on_review(conn, task_id: str, metadata) -> dict:
    """Round end for a DIRECT ``request_review`` handback (Prism 1c13b5bb5159).

    Under ``review_policy=all``, and for milestone cards, the handback never reaches ``complete_task``'s
    freshness gate, so its round draft would stay draft with only draft-skipped CI. Flip the card's own
    handoff PRs that carry an unconsumed round mark, through the same :func:`kanban_pr_freshness.check`
    ownership proof and one-shot consumption. Never arms. Fail-open: a flip it cannot prove leaves the
    draft for an operator ready and records ``round_draft_finalize_failed``; the review still proceeds.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_pr_freshness as _fresh
    if not enabled():
        return {}
    marks = mark_records(conn, task_id)
    live = {str(k).lower() for k, v in marks.items() if isinstance(v, dict) and not v.get("consumed")}
    refs = [r for r in _fresh.handoff_refs(metadata) if f"{r.repo}#{r.number}".lower() in live]
    if not refs:
        return {}

    def _record(flipped):
        if flipped:
            with _kb.write_txn(conn):
                _kb._append_event(conn, task_id, "handoff_draft_flipped", {"flipped": flipped})

    def _consume(key):
        with _kb.write_txn(conn):
            current = mark_records(conn, task_id).get(key)
            if current != marks.get(key) or not current or current.get("consumed"):
                raise ValueError("round draft mark already consumed or replaced")
            consumed = dict(current, consumed=True)
            _kb._append_event(conn, task_id, "round_draft_marked", {"marked": {key: consumed}})
            marks[key] = consumed

    try:
        out = _fresh.check(refs, task_id=task_id, allow_arm=False,
                           armable={f"{r.repo}#{r.number}" for r in refs}, flipped=flip_records(conn, task_id),
                           round_runs=card_run_ids(conn, task_id), marked=dict(marks), consume=_consume)
    except _fresh.DraftPrError as err:
        _record(err.flipped)
        with _kb.write_txn(conn):
            _kb._append_event(conn, task_id, "round_draft_finalize_failed", {"prs": err.prs})
        _log.warning("round draft: review handback left %s draft for operator ready", err.prs)
        return {"failed": err.prs}
    _record(out.get("draft_flipped_at"))
    return out
