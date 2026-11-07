"""Round draft (t_c418eebc): the worker owns its PR's draft bit for a fix round.

Round start drafts the card's own ready PR and posts ``round-draft: drafted by worker run <id> at <head>``;
the handback un-drafts once. The Prism findings-route never drafts.
"""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_open_pr as op
from hermes_cli import kanban_pr_freshness as fr
from hermes_cli import kanban_round_draft as rd

CARD, SHA = "t_1234abcd", "a" * 40


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(op, "FLEET_OWNERS", {"o"})
    monkeypatch.setattr(fr, "worker_draft_repos", lambda: {"o/r"})
    monkeypatch.delenv("KANBAN_ROUND_DRAFT", raising=False)
    state = {"pr": {"state": "open", "draft": False, "head": {"sha": SHA, "ref": "worker/" + CARD}}, "comments": []}

    def gh(*args):
        if args[0] == "repos/o/r/pulls/1":
            return json.loads(json.dumps(state["pr"]))
        if args[0].startswith("repos/o/r/issues/1/comments"):
            return json.loads(json.dumps(state["comments"]))
        return None
    log = []

    def draft(repo, n):
        log.append(("draft", repo, n))
        if state.get("draft_fails"):
            return False
        state["pr"]["draft"] = True
        return True

    def comment(repo, n, body):
        log.append(("comment", repo, n, body))
        state["comments"].append({"body": body})
        return True

    def ready(*a):
        log.append(("ready",) + a)
        state["pr"]["draft"] = False
        return 0
    records = {"o/r#1": {"head": SHA, "at": 1}}
    marks: dict = {}
    return SimpleNamespace(
        state=state, log=log, marks=marks,
        start=lambda run: rd.draft_at_round_start(CARD, run, records, gh=gh, draft=draft, comment=comment,
                                                  persist=marks.__setitem__),
        handback=lambda **kw: fr.check(op.extract_pr_refs("o/r#1"), task_id=CARD, allow_arm=False, gh=gh,
                                       flipped=records, ready=ready, round_runs={"101", "102"},
                                       marked=marks, **kw))


def test_round_start_drafts_own_ready_pr_and_marks_it(rig):
    assert rig.start(101) == {"o/r#1": "drafted + marked"}
    assert rig.log == [("draft", "o/r", 1), ("comment", "o/r", 1, rd.mark_line(101, SHA))]
    assert rig.state["pr"]["draft"] is True
    assert rig.marks["o/r#1"]["run"] == "101" and rig.marks["o/r#1"]["head"] == SHA


def test_forged_mark_comment_does_not_clear_an_operator_hold(rig):
    """Prism 852b8897186d repro: operator drafted at head H; anyone posts the mark line for an own run id."""
    rig.state["pr"]["draft"] = True
    rig.state["comments"] = [{"body": rd.mark_line(101, SHA)}]
    report = rig.handback()
    assert report["draft_held"] and rig.log == []


def test_persisted_mark_by_own_run_at_head_readies(rig):
    rig.state["pr"]["draft"] = True
    rig.marks["o/r#1"] = {"run": "101", "head": SHA, "at": 2}
    assert rig.handback()["draft_flipped"] == ["o/r#1"] and rig.log == [("ready", "o/r", 1)]


def test_persisted_mark_at_stale_head_or_before_flip_stays_held(rig):
    rig.state["pr"]["draft"] = True
    rig.marks["o/r#1"] = {"run": "101", "head": "b" * 40, "at": 2}
    assert rig.handback()["draft_held"] and rig.log == []
    rig.marks["o/r#1"] = {"run": "101", "head": SHA, "at": 0}     # older than the card's last flip
    assert rig.handback()["draft_held"] and rig.log == []
    rig.marks["o/r#1"] = {"run": "999", "head": SHA, "at": 2}     # not one of this card's runs
    assert rig.handback()["draft_held"] and rig.log == []


def test_draft_failure_logs_and_continues_without_retry(rig):
    rig.state["draft_fails"] = True
    assert rig.start(101) == {"o/r#1": "draft failed (logged; round runs CI per push)"}
    assert rig.log == [("draft", "o/r", 1)]   # one attempt, no mark, no retry


def test_second_run_adopts_marked_draft_and_handback_undrafts_once(rig):
    rig.start(101)                       # run 101 drafts, then dies mid-round
    rig.log.clear()
    assert rig.start(102) == {"o/r#1": "skip: already draft"}
    assert rig.log == []                 # no second draft, no second mark
    report = rig.handback()
    assert report["draft_flipped"] == ["o/r#1"] and rig.log == [("ready", "o/r", 1)]
    rig.handback()                       # ready now: a second handback does not flip again
    assert rig.log == [("ready", "o/r", 1)]


def test_same_head_draft_without_own_mark_stays_an_operator_hold(rig):
    rig.state["pr"]["draft"] = True
    rig.state["comments"] = [{"body": rd.mark_line(999, SHA)}, {"body": rd.mark_line(101, "b" * 40)}]
    report = rig.handback()
    assert report["draft_held"] and rig.log == []


def test_kill_switch_foreign_branch_and_closed_pr_untouched(rig, monkeypatch):
    monkeypatch.setenv("KANBAN_ROUND_DRAFT", "0")
    assert rig.start(101) == {} and rig.log == []
    rig.state["pr"]["draft"] = True
    rig.marks["o/r#1"] = {"run": "101", "head": SHA, "at": 2}
    assert rig.handback()["draft_held"] and rig.log == []   # kill switch: same-head draft is a hold, as before
    monkeypatch.delenv("KANBAN_ROUND_DRAFT")
    rig.state["pr"]["draft"] = False
    rig.state["pr"]["head"]["ref"] = "feature/no-card"
    assert rig.start(101) == {"o/r#1": "skip: branch does not name this card"}
    rig.state["pr"]["head"]["ref"] = "worker/" + CARD
    rig.state["pr"]["state"] = "closed"
    assert rig.start(101) == {"o/r#1": "skip: not open"} and rig.log == []


def test_worker_boot_reads_the_cards_flip_receipt_from_the_board(tmp_path, monkeypatch):
    """run_from_env against a real temp board: the receipt (handoff_draft_flipped) and run id drive the draft."""
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice", assignee="worker")
        kb.claim_task(conn, tid)
        run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "handoff_draft_flipped", {"flipped": {"o/r#1": {"head": SHA, "at": 1}}})
        assert rd.card_run_ids(conn, tid) == {str(run)}
    seen = []
    monkeypatch.setattr("agent.delegation_context.owned_kanban_task", lambda: tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    monkeypatch.setattr(rd, "draft_at_round_start",
                        lambda *a, **kw: seen.append(a) or kw["persist"]("o/r#1", {"run": a[1], "head": SHA, "at": 2}))
    rd.run_from_env()
    assert seen == [(tid, str(run), {"o/r#1": {"head": SHA, "at": 1}})]
    with kb.connect() as conn:   # the mark is on the card, written by the kernel from the worker's own run id
        assert rd.mark_records(conn, tid) == {"o/r#1": {"run": str(run), "head": SHA, "at": 2}}


def test_ready_carries_owner_card_to_ace_gate(tmp_path, monkeypatch):
    import hermes_constants
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ace_gate.py").touch()
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
    monkeypatch.setenv("KANBAN_ROUND_DRAFT_CARD", "t_deadbeef")
    calls = []
    monkeypatch.setattr(fr.subprocess, "run", lambda *a, **kw: calls.append(kw) or SimpleNamespace(returncode=0))
    assert fr.ace_gate_ready("o/r", 1, task_id=CARD) == 0
    assert fr.ace_gate_ready("o/r", 1) == 0
    assert [c["env"]["KANBAN_ROUND_DRAFT_CARD"] for c in calls] == [CARD, ""]
