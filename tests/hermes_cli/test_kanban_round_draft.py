"""Round draft (t_c418eebc): the worker owns its PR's draft bit for a fix round.

Round start persists the mark, then drafts the card's own ready PR on this run's branch; the handback un-drafts
once. Off by default (t_d3da1a4e). The Prism findings-route never drafts.
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
    monkeypatch.setenv("KANBAN_ROUND_DRAFT", "1")
    monkeypatch.delenv("KANBAN_HANDOFF_FRESHNESS", raising=False)
    monkeypatch.delenv("KANBAN_HANDOFF_DRAFT_FLIP", raising=False)
    state = {"pr": {"state": "open", "draft": False, "head": {"sha": SHA, "ref": "worker/" + CARD}},
             "comments": [], "events": []}

    def gh(*args):
        if args[0] == "repos/o/r/pulls/1":
            return json.loads(json.dumps(state["pr"]))
        if args[0] == "repos/o/r/pulls/2" and "pr2" in state:
            return json.loads(json.dumps(state["pr2"]))
        if args[0].startswith("repos/o/r/issues/1/comments"):
            return json.loads(json.dumps(state["comments"]))
        return None
    log = []

    def draft(repo, n):
        log.append(("draft", repo, n))
        if state.get("draft_fails"):
            return False
        state["pr" if n == 1 else "pr2"]["draft"] = True
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

    def persist(key, mark):
        if state.get("persist_fails"):
            raise OSError("board locked")
        log.append(("mark", key, bool(mark.get("consumed"))))
        marks[key] = mark
    return SimpleNamespace(
        state=state, log=log, marks=marks, records=records,
        start=lambda run, branch="worker/" + CARD: rd.draft_at_round_start(
            CARD, run, records, branch=branch, gh=gh, draft=draft, comment=comment, persist=persist),
        handback=lambda **kw: fr.check(op.extract_pr_refs("o/r#1"), task_id=CARD, allow_arm=False, gh=gh,
                                       flipped=records, ready=ready, round_runs={"101", "102"},
                                       marked=marks, consume=lambda key: marks[key].update(consumed=True), **kw))


def test_round_start_drafts_own_ready_pr_and_marks_it(rig):
    assert rig.start(101) == {"o/r#1": "drafted + marked"}
    # The mark is on the card BEFORE the PR is drafted (t_d3da1a4e).
    assert rig.log == [("mark", "o/r#1", False), ("draft", "o/r", 1), ("comment", "o/r", 1, rd.mark_line(101, SHA))]
    assert rig.state["pr"]["draft"] is True
    assert rig.marks["o/r#1"]["run"] == "101" and rig.marks["o/r#1"]["head"] == SHA


def test_default_off_drafts_nothing_and_marks_nothing(rig, monkeypatch):
    monkeypatch.delenv("KANBAN_ROUND_DRAFT")
    assert rig.start(101) == {}
    assert rig.log == [] and not rig.marks and rig.state["pr"]["draft"] is False


def test_failed_mark_write_leaves_the_pr_ready(rig):
    """t_d3da1a4e: the ownership record is written before the draft; if it cannot be written, no draft."""
    rig.state["persist_fails"] = True
    assert rig.start(101) == {"o/r#1": "skip: mark not persisted; not drafted"}
    assert rig.log == [] and not rig.marks and rig.state["pr"]["draft"] is False


def test_sibling_pr_on_the_same_card_is_never_touched(rig):
    rig.records["o/r#2"] = {"head": SHA, "at": 1}
    rig.state["pr2"] = {"state": "open", "draft": False, "head": {"sha": "c" * 40, "ref": "worker/" + CARD + "-docs"}}
    assert rig.start(101) == {"o/r#1": "drafted + marked", "o/r#2": "skip: not this run's branch"}
    assert ("draft", "o/r", 2) not in rig.log and "o/r#2" not in rig.marks
    assert rig.state["pr2"]["draft"] is False


def test_forged_mark_comment_does_not_clear_an_operator_hold(rig):
    """Prism 852b8897186d repro: operator drafted at head H; anyone posts the mark line for an own run id."""
    rig.state["pr"]["draft"] = True
    rig.state["comments"] = [{"body": rd.mark_line(101, SHA)}]
    report = rig.handback()
    assert report["draft_held"] and rig.log == []


def test_persisted_mark_by_own_run_at_head_readies(rig):
    rig.start(101)
    rig.log.clear()
    assert rig.handback()["draft_flipped"] == ["o/r#1"] and rig.log == [("ready", "o/r", 1)]


def test_persisted_mark_at_stale_head_or_before_flip_stays_held(rig):
    rig.state["pr"]["draft"] = True
    rig.marks["o/r#1"] = {"run": "101", "head": "b" * 40, "at": 2}
    assert rig.handback()["draft_held"] and rig.log == []
    rig.marks["o/r#1"] = {"run": "101", "head": SHA, "at": 0}     # older than the card's last flip
    assert rig.handback()["draft_held"] and rig.log == []
    rig.marks["o/r#1"] = {"run": "999", "head": SHA, "at": 2}     # not one of this card's runs
    assert rig.handback()["draft_held"] and rig.log == []


@pytest.mark.parametrize("kill_switch", [False, True])
def test_historical_mark_does_not_hold_later_new_head_draft(rig, monkeypatch, kill_switch):
    rig.start(101)
    assert rig.handback()["draft_flipped"] == ["o/r#1"]
    if kill_switch:
        monkeypatch.setenv("KANBAN_ROUND_DRAFT", "0")
        rig.marks["o/r#1"].pop("consumed")
    rig.state["pr"]["head"]["sha"] = "b" * 40
    rig.state["pr"]["draft"] = True
    rig.log.clear()
    assert rig.handback()["draft_flipped"] == ["o/r#1"]
    assert rig.log == [("ready", "o/r", 1)]


def test_mixed_case_persisted_mark_is_consumed_by_real_handback(tmp_path, monkeypatch):
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KANBAN_ROUND_DRAFT", "1")
    monkeypatch.setattr(op, "FLEET_OWNERS", {"o"})
    monkeypatch.setattr(fr, "worker_draft_repos", lambda: {"o/r"})
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "milestone_only")
    monkeypatch.setattr(kb, "_pre_pr_refusals", lambda *a, **kw: [])
    monkeypatch.setattr(kb, "is_milestone_card", lambda *a: True)
    monkeypatch.setattr(op, "_default_query", lambda: lambda *a: {"state": "OPEN"})
    state = {"draft": True}
    ready_calls = []

    def gh(path, *a):
        if "/pulls/" in path:
            return {"state": "open", "draft": state["draft"],
                    "head": {"sha": SHA, "ref": "worker/" + tid}}
        if "/timeline?" in path:
            return {"pages": [[{"id": 8, "event": "convert_to_draft", "actor": {"id": 7}}]]}
        return None

    def ready(repo, number):
        ready_calls.append((repo, number))
        state["draft"] = False
        return 0

    monkeypatch.setattr(fr, "_default_gh", lambda: gh)
    monkeypatch.setattr(fr, "_default_ready", lambda: ready)
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="mixed case round", assignee="worker")
        kb.claim_task(conn, tid)
        run = kb.get_task(conn, tid).current_run_id
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "handoff_draft_flipped",
                             {"flipped": {"O/R#1": {"head": SHA, "at": 1}}})
            kb._append_event(conn, tid, "round_draft_marked", {"marked": {"O/R#1": {
                "run": str(run), "head": SHA, "at": 2, "actor_id": 7, "transition_id": 8}}})
        assert kb.complete_task(conn, tid, summary="mixed case handback",
                                metadata={"pr_url": "https://github.com/O/R/pull/1"}, expected_run_id=run)
        assert rd.mark_records(conn, tid)["O/R#1"]["consumed"] is True
    assert ready_calls == [("O/R", 1)]


def test_draft_failure_logs_and_continues_without_retry(rig):
    rig.state["draft_fails"] = True
    assert rig.start(101) == {"o/r#1": "draft failed (logged; round runs CI per push)"}
    # one attempt, no retry; the mark written before it is voided so it can never adopt a later draft
    assert rig.log == [("mark", "o/r#1", False), ("draft", "o/r", 1), ("mark", "o/r#1", True)]


def test_second_run_adopts_marked_draft_and_handback_undrafts_once(rig):
    rig.start(101)                       # run 101 drafts, then dies mid-round
    rig.log.clear()
    assert rig.start(102) == {"o/r#1": "skip: already draft"}
    assert rig.log == []                 # no second draft, no second mark
    rig.state["pr"]["head"]["sha"] = "b" * 40  # WIP push: a new-head draft is the card's own
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
    monkeypatch.setenv("KANBAN_ROUND_DRAFT", "1")
    rig.state["pr"]["draft"] = False
    rig.state["pr"]["head"]["ref"] = "feature/no-card"
    assert rig.start(101, branch="feature/no-card") == {"o/r#1": "skip: branch does not name this card"}
    rig.state["pr"]["head"]["ref"] = "worker/" + CARD
    rig.state["pr"]["state"] = "closed"
    assert rig.start(101) == {"o/r#1": "skip: not open"} and rig.log == []


@pytest.mark.parametrize("reviewer", [False, True])
def test_worker_boot_reads_the_cards_flip_receipt_from_the_board(tmp_path, monkeypatch, reviewer):
    """run_from_env against a real temp board: the receipt (handoff_draft_flipped) and run id drive the draft."""
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("KANBAN_ROUND_DRAFT", "1")
    monkeypatch.setattr(rd, "_workspace_branch", lambda: "worker/" + tid)
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="slice", assignee="worker")
        kb.claim_task(conn, tid)
        run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "handoff_draft_flipped", {"flipped": {"o/r#1": {"head": SHA, "at": 1}}})
        assert rd.card_run_ids(conn, tid) == {str(run)}
        if reviewer:
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "claimed", {"source_status": "review"}, run_id=run)
    seen, kws = [], []
    monkeypatch.setattr("agent.delegation_context.owned_kanban_task", lambda: tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    monkeypatch.setattr(rd, "draft_at_round_start",
                        lambda *a, **kw: seen.append(a) or kws.append(kw) or kw["persist"]("o/r#1", {"run": a[1], "head": SHA, "at": 2}))
    rd.run_from_env()
    if reviewer:
        assert seen == []
        return
    assert seen == [(tid, str(run), {"o/r#1": {"head": SHA, "at": 1}})]
    assert kws[-1]["branch"] == "worker/" + tid
    with kb.connect() as conn:   # the mark is on the card, written by the kernel from the worker's own run id
        assert rd.mark_records(conn, tid) == {"o/r#1": {"run": str(run), "head": SHA, "at": 2}}


@pytest.mark.parametrize("switch", ["KANBAN_HANDOFF_FRESHNESS", "KANBAN_HANDOFF_DRAFT_FLIP"])
def test_disabled_paired_switch_never_drafts(rig, monkeypatch, switch):
    monkeypatch.setenv(switch, "0")
    assert rig.start(101) == {} and rig.log == []


def test_new_host_operations_are_pinned(monkeypatch):
    monkeypatch.setenv("GH_HOST", "foreign.invalid")
    calls = []
    monkeypatch.setattr(rd.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or
                        SimpleNamespace(returncode=0, stdout='{"id":7}'))
    rd.gh_draft("o/r", 1)
    rd.gh_comment("o/r", 1, "receipt")
    rd.gh_snapshot("repos/o/r/pulls/1")
    assert "--hostname" in calls[-1][0]
    assert calls[-1][1]["env"]["GH_SHIM_FRESH"] == "1"   # t_d3da1a4e: the shim's 30 s floor served stale PR state
    assert all(kw["env"]["GH_HOST"] == "github.com" for _, kw in calls)
    assert calls[0][0][calls[0][0].index("--repo") + 1] == "https://github.com/o/r"


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


@pytest.mark.parametrize("summary, readied", [("Fix complete", True), ("FAIL: close gate not met", False)])
def test_direct_review_handoff_readies_marked_round_draft(tmp_path, monkeypatch, summary, readied):
    """Prism 1c13b5bb5159: review_policy=all goes through request_review(), never complete_task's freshness
    gate; a positive handback still flips the round draft ready once, a negative one leaves it draft."""
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KANBAN_ROUND_DRAFT", "1")
    monkeypatch.setattr(op, "FLEET_OWNERS", {"o"})
    monkeypatch.setattr(fr, "worker_draft_repos", lambda: {"o/r"})
    monkeypatch.setattr(kb, "configured_review_policy", lambda: "all")
    monkeypatch.setattr(kb, "_enforce_branch_base", lambda *a, **kw: None)
    monkeypatch.setattr(kb, "_enforce_handback_head", lambda *a, **kw: None)
    state = {"draft": True}
    ready_calls = []

    def gh(path, *a):
        if "/pulls/" in path:
            return {"state": "open", "draft": state["draft"], "head": {"sha": SHA, "ref": "worker/" + tid}}
        if "/timeline?" in path:
            return {"pages": [[{"id": 8, "event": "convert_to_draft", "actor": {"id": 7}}]]}
        return None

    def ready(repo, number):
        ready_calls.append((repo, number))
        state["draft"] = False
        return 0

    monkeypatch.setattr(fr, "_default_gh", lambda: gh)
    monkeypatch.setattr(fr, "_default_ready", lambda: ready)
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="direct review round", assignee="worker")
        kb.claim_task(conn, tid)
        run = kb.get_task(conn, tid).current_run_id
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "handoff_draft_flipped", {"flipped": {"o/r#1": {"head": SHA, "at": 1}}})
            kb._append_event(conn, tid, "round_draft_marked", {"marked": {"o/r#1": {
                "run": str(run), "head": SHA, "at": 2, "actor_id": 7, "transition_id": 8}}})
        meta = {"pr_url": "https://github.com/o/r/pull/1"}
        assert kb.request_review(conn, tid, summary=summary, metadata=meta, reviewer="argus",
                                 expected_run_id=run)
        assert kb.get_task(conn, tid).status == "review"
        assert bool(rd.mark_records(conn, tid)["o/r#1"].get("consumed")) is readied
    assert ready_calls == ([("o/r", 1)] if readied else [])
    assert state["draft"] is (not readied)
