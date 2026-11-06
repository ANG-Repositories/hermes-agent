"""Round draft (t_c418eebc): the WORKER owns its PR's draft bit for a whole fix round.

Goal: one CI run per round, not one per push. The draft lane skips CI on draft PRs, so:

* Round start (:func:`draft_at_round_start`, called once at worker boot, before the first turn and so before
  any push): for each PR this card's earlier handback flipped ready (``handoff_draft_flipped`` events = the
  receipt naming card + PR) that is still OPEN, ready and on a branch naming this card, run
  ``gh pr ready --undo`` and post the provenance mark ``round-draft: drafted by worker run <id> at <head>``.
  A failure is logged and the run continues (worst case: today's CI per push). Never retried.
* Round end: the handback (:mod:`kanban_pr_freshness`) un-drafts once through ``ace_gate.py ready``. A draft at
  the recorded head is normally an operator hold; :func:`round_marked` lets the handback tell this worker-round
  draft (a mark from one of THIS card's runs, at this head, after the card's last flip) from that hold.
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


def round_marked(gh, ref, head: str, record, own_runs: Optional[Iterable] = None) -> bool:
    """True when a PR comment posted after this card's last flip carries ``mark_line(<run>, head)`` for one of
    ``own_runs`` (this card's run ids; None = any run). Unreadable comments, no flip record or the kill switch
    -> False, so the handback keeps treating the draft as an operator hold."""
    if not enabled() or not head or not isinstance(record, dict):
        return False
    try:
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(record.get("at") or 0)))
    except (TypeError, ValueError):
        return False
    suffix = f" at {head}"
    # gh_api returns objects only: --jq folds the comments into {"runs": [<run id of each mark at head>]}.
    jq = ('{runs: [.[] | (.body // "") | split("\\n")[] | rtrimstr("\\r") '
          f'| select(startswith({json.dumps(MARK_PREFIX)}) and endswith({json.dumps(suffix)})) '
          f'| ltrimstr({json.dumps(MARK_PREFIX)}) | rtrimstr({json.dumps(suffix)})]}}')
    out = gh(f"repos/{ref.repo}/issues/{ref.number}/comments?per_page=100&since={since}", "--jq", jq)
    runs = out.get("runs") if isinstance(out, dict) else None
    if not isinstance(runs, list):
        return False
    wanted = None if own_runs is None else {str(r) for r in own_runs}
    return any(str(r).isdigit() and (wanted is None or str(r) in wanted) for r in runs)


def gh_draft(repo: str, number: int) -> bool:
    try:
        proc = subprocess.run(["gh", "pr", "ready", str(number), "--repo", repo, "--undo"],
                              capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("round draft: gh pr ready --undo %s#%s failed: %s", repo, number, exc)
        return False
    if proc.returncode:
        _log.warning("round draft: gh pr ready --undo %s#%s rc=%s: %s", repo, number, proc.returncode,
                     (proc.stderr or "").strip()[:300])
    return proc.returncode == 0


def gh_comment(repo: str, number: int, body: str) -> bool:
    try:
        proc = subprocess.run(["gh", "api", "-X", "POST", f"repos/{repo}/issues/{number}/comments",
                               "-f", f"body={body}"], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("round draft: mark on %s#%s failed: %s", repo, number, exc)
        return False
    if proc.returncode:
        _log.warning("round draft: mark on %s#%s rc=%s: %s", repo, number, proc.returncode,
                     (proc.stderr or "").strip()[:300])
    return proc.returncode == 0


def draft_at_round_start(task_id: str, run_id, records: dict, *, gh=None, draft: Optional[DraftFn] = None,
                         comment: Optional[CommentFn] = None) -> dict:
    """Draft this card's own ready PRs before the round's first push. Returns ``{"o/r#n": outcome}``.

    Never raises: every failure is logged and the run continues."""
    from hermes_cli import kanban_pr_freshness as fr
    from hermes_cli.kanban_open_pr import branch_names_card, extract_pr_refs, is_fleet_ref
    report: dict = {}
    if not enabled() or not task_id or not run_id or not records:
        return report
    if gh is None:
        gh = fr._default_gh()
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
        if not draft(ref.repo, ref.number):
            report[key] = "draft failed (logged; round runs CI per push)"
            continue
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
        with _kbc.connect_closing() as conn:
            records = flip_records(conn, task_id)
        return draft_at_round_start(task_id, run_id, records)
    except Exception:  # noqa: BLE001 -- a round-start draft must never stop a worker
        _log.warning("round draft at worker start failed", exc_info=True)
        return {}
