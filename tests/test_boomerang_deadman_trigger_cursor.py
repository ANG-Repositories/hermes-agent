"""t_a6da8394: a review_deadman wake is answered only by an owner act AFTER the deadman event.

Replays t_9854b4b7 (board rows, 2026-11-13). The card went to human:apollo at 03:22 (review_requested); Apollo's
08:10 comment ended in `deadman: hold 2026-11-13T16:00:00-08:00`; the hold expired at 16:00 and two review_deadman
events fired, 16:56 (review-failsafe attended read) and 17:58 (kanban-review-deadman). The fork dismissed both with
"operator already acted; stale wake ignored" because deadman_owner_acted counted ANY owner event since the review
began (the 08:10 hold comment), and stale_surface_reason would have refused the surface as "owner comment newer than
handback" / "summarized PR merged" too. The card sat 141 min to a tier-2 page.

Substitution: the real comments are author `apollo` from Apollo's human session; here they are `human:apollo`, which
owner_acted accepts by label without a state.db session probe. Same verdict under the old rule (asserted below).
"""
import asyncio
import json

import pytest

from gateway import kanban_boomerang_wake as bw
from hermes_cli import kanban_db as kb

from tests.test_kanban_boomerang_defaults import _runner

OWNER = "human:apollo"
PR = "ANG-Ventures/hermes-home#11316"


@pytest.fixture(autouse=True)
def owner_scope(monkeypatch):
    from types import SimpleNamespace
    from gateway import kanban_owner_wake as ow
    home = SimpleNamespace(origin=SimpleNamespace(chat_id="42", platform="discord"))
    monkeypatch.setattr(ow, "resolve_owner", lambda *a: home)
    monkeypatch.setattr(bw, "BACKOFF_BASE_S", 0.0, raising=False)


def _ev(conn, tid, kind, payload, ts):
    kb._append_event(conn, tid, kind, payload)
    eid = kb.max_event_id(conn)
    conn.execute("UPDATE task_events SET created_at = ? WHERE id = ?", (ts, eid))
    conn.commit()
    return eid


def _cm(conn, tid, author, body, ts):
    kb.add_comment(conn, tid, author, body)
    conn.execute("UPDATE task_comments SET created_at = ? WHERE id = (SELECT MAX(id) FROM task_comments)", (ts,))
    conn.execute("UPDATE task_events SET created_at = ? WHERE id = ?", (ts, kb.max_event_id(conn)))
    conn.commit()


def _board_9854(tmp_path, monkeypatch):
    """t_9854b4b7's rows from the 01:22 handback to the second wake, in board order with board timestamps."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("BOOMERANG_DEADMAN_TRIGGER_CURSOR", raising=False)
    monkeypatch.delenv("BOOMERANG_STALE_GUARD", raising=False)
    # PR 11316 merged at 02:00: the merged-PR rule must not answer the wake either
    monkeypatch.setattr(bw, "_health_fn", lambda: (lambda repo, n: {"state": "merged"}))
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="t_9854b4b7 replay", assignee=OWNER)
        conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
        conn.commit()
        _ev(conn, tid, "review_requested", {"summary": f"auto-routed: PR {PR} still open"}, 1794561757)
        _cm(conn, tid, OWNER, "Post-merge read (Apollo, 2026-11-13) ...", 1794565752)
        _cm(conn, tid, OWNER, "deadman: hold 2026-11-13T10:00:00-08:00", 1794565753)
        _ev(conn, tid, "assigned", {"assignee": OWNER, "from": "_review-hold"}, 1794568978)
        _ev(conn, tid, "review_requested", {"summary": "review hold: checks settled, handed to human:apollo",
                                            "reviewer": OWNER}, 1794568978)
        _cm(conn, tid, OWNER, "Why this waits (Apollo, 2026-11-13) ...\n"
                              "deadman: hold 2026-11-13T16:00:00-08:00", 1794586248)
        _cm(conn, tid, "default", "Apollo (another session, mention only), 15:57 PST 11-13 ...", 1794614177)
        _cm(conn, tid, "review-failsafe", "[review-failsafe] ATTENDED-READ for Apollo ...", 1794617789)
        wake1 = _ev(conn, tid, "review_deadman", {"by": "review-failsafe-attended-read",
                                                  "review_episode": 2549189, "waiting_min": 813}, 1794617789)
        _cm(conn, tid, "boomerang-wake", "boomerang: review_deadman operator already acted; stale wake ignored "
                                         "(delegation del-20261113-165633-e26b)", 1794617843)
        wake2 = _ev(conn, tid, "review_deadman", {"by": "kanban-review-deadman", "episode": 2549189,
                                                  "threshold_min": 15, "waiting_min": 17}, 1794621535)
    return tid, wake1, wake2


def _entry(tid, event_id, did):
    return dict(key=f"claim-{did}", task_id=tid, board=None, row="review_deadman", kind="review_deadman",
                event_id=event_id, delegation_id=did, verdict_nonce="abcd", route={}, mode="fork",
                assignee=OWNER, pr=PR)


def test_old_rule_dismisses_both_wakes(tmp_path, monkeypatch):
    """Step 2 replay: BOOMERANG_DEADMAN_TRIGGER_CURSOR=0 is the rule that ran on 11-13; both wakes read as acted."""
    tid, w1, w2 = _board_9854(tmp_path, monkeypatch)
    monkeypatch.setenv("BOOMERANG_DEADMAN_TRIGGER_CURSOR", "0")
    for eid, did in ((w1, "del-20261113-165633-e26b"), (w2, "del-20261113-175859-c310")):
        e = _entry(tid, eid, did)
        assert bw.deadman_owner_acted(e) is True
        assert bw.stale_surface_reason(e) == "owner comment newer than handback"


def test_both_wakes_are_not_answered(tmp_path, monkeypatch):
    tid, w1, w2 = _board_9854(tmp_path, monkeypatch)
    for eid, did in ((w1, "del-20261113-165633-e26b"), (w2, "del-20261113-175859-c310")):
        e = _entry(tid, eid, did)
        assert bw.deadman_owner_acted(e) is False
        assert bw.stale_surface_reason(e) is None


@pytest.mark.parametrize("wake", [1, 2])
def test_both_wakes_are_delivered_through_run_entry(tmp_path, monkeypatch, wake):
    tid, w1, w2 = _board_9854(tmp_path, monkeypatch)
    did = ("del-20261113-165633-e26b", "del-20261113-175859-c310")[wake - 1] + "-replay"
    entry = _entry(tid, (w1, w2)[wake - 1], did)
    state = bw.BoomerangWakeState(tmp_path / "claims.json")
    state.claim(entry["key"], entry)

    async def parent(*args):
        return object(), "fork"
    monkeypatch.setattr(bw, "parent_resolver", parent)
    monkeypatch.setattr(bw, "ledger_append", lambda *a: None)
    monkeypatch.setattr(bw, "build_goal", lambda entry: "attended read")
    final = "<<<BOOMERANG-VERDICT nonce=abcd>>>\n" + json.dumps(
        {"action": "surface", "summary": "attended read owed on PR 11316"}) + "\n<<<END-VERDICT>>>"
    monkeypatch.setattr(bw, "run_child", lambda *args: ("completed", final))
    turns = []

    async def resurface(runner, e, text=None):
        turns.append(text)
        return True
    monkeypatch.setattr(bw, "resurface", resurface)
    sent = []
    asyncio.run(bw.run_entry(_runner([], sent), entry, state))
    assert sent == [f"boomerang handled {tid} (review_deadman): attended read owed on PR 11316"]
    assert len(turns) == 1
    with kb.connect_closing() as conn:
        mine = [c.body for c in kb.list_comments(conn, tid) if c.body.endswith(f"(delegation {did})")]
    assert f"boomerang: review_deadman attended read owed on PR 11316 (delegation {did})" in mine
    assert not any("operator already acted" in b for b in mine)


@pytest.mark.parametrize("body,acted", [
    ("Read it; landing after CI.", True),                          # a plain comment after the event
    ("deadman: hold 2099-01-01T00:00:00-08:00", True),             # a fresh hold is an act
    ("re-held\ndeadman: hold 2020-01-01T00:00:00Z", False),        # an expired hold is not
])
def test_owner_comment_after_the_event(tmp_path, monkeypatch, body, acted):
    tid, w1, _w2 = _board_9854(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        _cm(conn, tid, OWNER, body, 1794617800)
    e = _entry(tid, w1, "d-after")
    assert bw.deadman_owner_acted(e) is acted
    assert (bw.stale_surface_reason(e) is not None) is acted


@pytest.mark.parametrize("kind", ["changes_requested", "blocked", "completed"])
def test_owner_disposition_after_review_still_answers(tmp_path, monkeypatch, kind):
    """A disposition is evidence from the review cursor on; an edit is not (comments are covered above)."""
    tid, w1, _w2 = _board_9854(tmp_path, monkeypatch)
    from gateway import kanban_wake_defer as wd
    seen = []

    def fake_owner_acted(conn, row, skip=None):
        for r in conn.execute("SELECT id, kind, payload, created_at FROM task_events WHERE task_id = ? AND id > ? "
                              "ORDER BY id", (row["task_id"], row["cursor"])):
            if r["kind"] != "commented" and not (skip and skip(r)):
                seen.append(r["kind"])
        return "x" if seen else None
    monkeypatch.setattr(wd, "owner_acted", fake_owner_acted)
    with kb.connect_closing() as conn:
        _ev(conn, tid, "edited", {}, 1794600000)
        _ev(conn, tid, kind, {}, 1794600001)
    assert bw.deadman_owner_acted(_entry(tid, w1, "d-disp")) is True
    assert seen == [kind]
