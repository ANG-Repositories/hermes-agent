"""Draft-flip gaps (t_5c6c67c5): a negative-result handback and a failed PR lookup never leave the worker's
own draft PR silently draft.

(1) ``review_negative_result`` handbacks went straight to the human lane through request_review and never ran
    the freshness gate (24 of 24 in 24 h carried no handoff_freshness): the own draft is now flipped via
    ace_gate (``handoff_draft_flipped``) or the handback is refused (``completion_blocked_draft_pr``).
(2) hermes-home#8231: ``lookup: failed (fail-open)`` routed a draft to review. The lookup is retried once;
    the card's own PR in a worker-draft repo that is still unreadable refuses (``DraftLookupError``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_open_pr as op
from hermes_cli import kanban_pr_freshness as fr

PR_URL = "https://github.com/o/r/pull/5"
HEAD = "a" * 40


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(op, "FLEET_OWNERS", op.FLEET_OWNERS | {"o", "u"})
    monkeypatch.setenv("KANBAN_MERGE_GATE_PROFILE", str(tmp_path / "absent-mgp.json"))
    cfg = tmp_path / "worker-pr-draft.json"
    cfg.write_text(json.dumps({"enabled": True, "repos": ["O/R"]}))
    monkeypatch.setenv("KANBAN_WORKER_PR_DRAFT", str(cfg))
    monkeypatch.setattr(fr, "LOOKUP_RETRY_DELAY_S", 0)


def _gh(head_ref, *, draft=True, fail_first=0, state="open"):
    """Fake ``gh api``: the pulls/N read returns None ``fail_first`` times, then the PR."""
    calls = []
    left = {"fail": fail_first}

    def gh(*args):
        calls.append(args)
        path = args[-1]
        if "/pulls/" in path and "/update-branch" not in path:
            if left["fail"]:
                left["fail"] -= 1
                return None
            return {"draft": draft, "state": state, "head": {"sha": HEAD, "ref": head_ref},
                    "base": {"ref": "main"}, "mergeable_state": "clean"}
        if "/compare/" in path:
            return {"behind_by": 0, "ahead_by": 1}
        if path.endswith("check-runs?per_page=100"):
            return {"check_runs": [{"status": "completed", "conclusion": "skipped"}]}
        if path.endswith("/status"):
            return {"state": "success", "total_count": 1}
        return None
    gh.calls = calls
    return gh


def _pr_reads(gh):
    return [c for c in gh.calls if c[-1].endswith("/pulls/5")]


# --- (2) lookup retry, unit ------------------------------------------------


def test_lookup_retried_once_then_succeeds():
    gh = _gh("daedalus/t_5c6c67c5-x", draft=False, fail_first=1)
    rep = fr.check(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", allow_arm=False, gh=gh,
                   armable={"o/r#5"})
    assert len(_pr_reads(gh)) == 2
    assert "lookup" not in rep["prs"]["o/r#5"]


def test_lookup_failing_twice_refuses_own_pr_in_worker_draft_repo():
    """Red on main: the first None was recorded `failed (fail-open)` and the handoff went to review."""
    gh = _gh("daedalus/t_5c6c67c5-x", fail_first=2)
    with pytest.raises(fr.DraftLookupError) as exc:
        fr.check(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", allow_arm=True, gh=gh,
                 armable={"o/r#5"}, arm=lambda *a: pytest.fail("armed"))
    assert exc.value.prs == ["o/r#5"]
    assert isinstance(exc.value, fr.DraftPrError)
    assert "failed twice" in str(exc.value) and "draft_ok" in str(exc.value)
    assert len(_pr_reads(gh)) == 2


def test_lookup_failure_on_unlisted_repo_still_fails_open():
    gh = _gh("daedalus/t_5c6c67c5-x", fail_first=2)
    url = "https://github.com/u/other/pull/5"
    rep = fr.check(op.extract_pr_refs(url), task_id="t_5c6c67c5", allow_arm=False, gh=gh,
                   armable={"u/other#5"})
    assert rep["prs"]["u/other#5"]["lookup"] == "failed (fail-open)"


def test_lookup_failure_on_mentioned_pr_fails_open():
    gh = _gh("daedalus/t_5c6c67c5-x", fail_first=2)
    rep = fr.check(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", allow_arm=False, gh=gh,
                   armable=set())
    assert rep["prs"]["o/r#5"]["lookup"] == "failed (fail-open)"


def test_lookup_failure_with_draft_ok_passes():
    gh = _gh("daedalus/t_5c6c67c5-x", fail_first=2)
    rep = fr.check(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", allow_arm=False, gh=gh,
                   armable={"o/r#5"}, draft_ok=True)
    assert rep["prs"]["o/r#5"]["lookup"] == "failed (fail-open)"


# --- (1) flip_own_drafts, unit ---------------------------------------------


def test_flip_own_drafts_flips_own_and_skips_foreign_and_closed():
    ready = []
    rep = fr.flip_own_drafts(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", armable={"o/r#5"},
                             gh=_gh("daedalus/t_5c6c67c5-x"), ready=lambda r, n: ready.append((r, n)) or 0)
    assert ready == [("o/r", 5)] and rep["draft_flipped"] == ["o/r#5"]
    assert rep["draft_flipped_at"]["o/r#5"]["head"] == HEAD

    rep = fr.flip_own_drafts(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", armable=set(),
                             gh=_gh("daedalus/t_deadbeef-parent"), ready=lambda r, n: pytest.fail("flipped"))
    assert "not this card" in rep["prs"]["o/r#5"]["draft_flip"] and "draft_flipped" not in rep

    rep = fr.flip_own_drafts(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", armable={"o/r#5"},
                             gh=_gh("daedalus/t_5c6c67c5-x", state="closed"),
                             ready=lambda r, n: pytest.fail("flipped a closed PR"))
    assert rep["prs"]["o/r#5"] == {}


@pytest.mark.parametrize("rc", [20, 21, 3, None])
def test_flip_own_drafts_failed_flip_refuses(rc):
    with pytest.raises(fr.DraftPrError) as exc:
        fr.flip_own_drafts(op.extract_pr_refs(PR_URL), task_id="t_5c6c67c5", armable={"o/r#5"},
                           gh=_gh("daedalus/t_5c6c67c5-x"), ready=lambda r, n: rc)
    assert exc.value.prs == ["o/r#5"] and "--draft-ok" in str(exc.value)


# --- (1) E2E through request_review on a temp board ------------------------


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kb, "configured_max_review_rounds", lambda: 0)
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    kb.init_db()
    monkeypatch.setattr(op, "_default_query", lambda: (lambda repo, n: {"state": "OPEN"}))
    return home


def _claimed(conn):
    tid = kb.create_task(conn, title="slice: soak", assignee="worker")
    kb.claim_task(conn, tid)
    run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    return tid, run


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind)).fetchall()]


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]


NEG = f"Close-on read: NOT met. {PR_URL} stays open."


def test_e2e_negative_result_handback_flips_its_own_draft(board, monkeypatch):
    """Red on main: request_review routed the card to review and never called ace_gate (no event)."""
    ready, box = [], []
    monkeypatch.setattr(fr, "_default_gh", lambda: _gh(f"daedalus/{box[0]}-fix"))
    monkeypatch.setattr(fr, "_default_ready", lambda: (lambda r, n: ready.append((r, n)) or 0))
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        box.append(tid)
        ok, reason = kb.request_review(conn, tid, summary=NEG, metadata={"pr_url": PR_URL},
                                       expected_run_id=run, with_reason=True)
        assert ok is True, reason
        assert _status(conn, tid) == "review"
        assert _events(conn, tid, "review_negative_result")
        flipped = _events(conn, tid, "handoff_draft_flipped")
        assert [list(e["flipped"]) for e in flipped] == [["o/r#5"]]
        assert kb.latest_run(conn, tid).metadata["handoff_freshness"]["draft_flipped"] == ["o/r#5"]
    assert ready == [("o/r", 5)]


def test_e2e_negative_result_failed_flip_refuses_with_audited_event(board, monkeypatch):
    box = []
    monkeypatch.setattr(fr, "_default_gh", lambda: _gh(f"daedalus/{box[0]}-fix"))
    monkeypatch.setattr(fr, "_default_ready", lambda: (lambda r, n: 21))   # review hold
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        box.append(tid)
        with pytest.raises(fr.DraftPrError):
            kb.request_review(conn, tid, summary=NEG, metadata={"pr_url": PR_URL}, expected_run_id=run)
        assert _status(conn, tid) == "running"
        assert _events(conn, tid, "completion_blocked_draft_pr") == [
            {"prs": ["o/r#5"], "path": "review_negative_result"}]
        assert _events(conn, tid, "review_negative_result") == []


def test_e2e_negative_result_lookup_failure_refuses(board, monkeypatch):
    box = []
    monkeypatch.setattr(fr, "_default_gh", lambda: _gh(f"daedalus/{box[0]}-fix", fail_first=2))
    monkeypatch.setattr(fr, "_default_ready", lambda: (lambda r, n: pytest.fail("flipped unread")))
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        box.append(tid)
        with pytest.raises(fr.DraftLookupError):
            kb.request_review(conn, tid, summary=NEG, metadata={"pr_url": PR_URL}, expected_run_id=run)
        assert _status(conn, tid) == "running"


def test_e2e_negative_result_foreign_draft_mention_still_routes(board, monkeypatch):
    """A stacked parent draft the handoff only mentions is not this card's to flip, and not a refusal."""
    monkeypatch.setattr(fr, "_default_gh", lambda: _gh("daedalus/t_deadbeef-parent"))
    monkeypatch.setattr(fr, "_default_ready", lambda: (lambda r, n: pytest.fail("flipped foreign")))
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.request_review(conn, tid, summary=f"NOT met: waits on parent {PR_URL}",
                                 expected_run_id=run) is True
        assert _status(conn, tid) == "review"
        assert _events(conn, tid, "handoff_draft_flipped") == []


def test_e2e_positive_handback_untouched(board, monkeypatch):
    """No negative result: the policy path completes the card (complete_task's own gate owns the flip)."""
    calls = []
    monkeypatch.setattr(kb, "_negative_result_draft_flip", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(fr, "_default_gh", lambda: None)
    monkeypatch.setattr(kb, "configured_receipt_gate", lambda: False)
    with kb.connect() as conn:
        tid, run = _claimed(conn)
        assert kb.request_review(conn, tid, summary="shipped; 3 tests pass", expected_run_id=run) is True
    assert calls == []
