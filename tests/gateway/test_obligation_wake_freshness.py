"""Reconciler wakes re-read decision rows after waiting in the gateway queue."""
import asyncio
import sqlite3

import pytest

from gateway import kanban_wake_freshness as fresh
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_work_obligations as obligations
from tests.gateway.test_control_socket_wake import (
    RecordingAdapter, _human_turn, _params, _runner, store,
)


@pytest.fixture
def decisions(tmp_path, monkeypatch):
    path = tmp_path / "decisions.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE obligations (id TEXT, task_id TEXT, state TEXT)")
        conn.executemany("INSERT INTO obligations VALUES (?, 't_a486319d', ?)", [
            ("ob_66296ecbf9d0", "open_unclaimed"), ("ob_2baa438fdfca", "open_unclaimed")])
    boards = []

    def connect(*, board=None):
        boards.append(board)
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    monkeypatch.setattr(kb, "connect_readonly", connect)
    logs = []
    monkeypatch.setattr(obligations, "post_logs", logs.append)
    return path, boards, logs


def _text(ids):
    return (f"[obligations] card t_a486319d has {len(ids)} open decision(s); claim one to hold it:\n" +
            "\n".join(f"- {oid} (detached:run, waiting 15 min): | decision\n"
                      f"  claim: hermes kanban --board default obligation claim {oid}" for oid in ids))


def _enqueue(store, text):
    _human_turn(store)
    adapter = RecordingAdapter()
    assert asyncio.run(_runner(store, adapter)._deliver_control_wake(_params(text=text)))["delivered"]
    return adapter.handled[0]


@pytest.mark.parametrize("state", ["resolved", "deleted"])
def test_queued_obligation_resolved_before_delivery_is_suppressed(store, decisions, state):
    path, boards, logs = decisions
    event = _enqueue(store, _text(["ob_66296ecbf9d0"]))
    with sqlite3.connect(path) as conn:
        if state == "deleted":
            conn.execute("DELETE FROM obligations")
        else:
            conn.execute("UPDATE obligations SET state = 'resolved'")
    # Real admitted-turn entry must stop before session/model setup.
    runner = GatewayRunner.__new__(GatewayRunner)
    assert asyncio.run(runner._handle_message_with_agent_admitted(event, event.source, "k", 0)) is None
    assert boards == ["default"]
    assert len(logs) == 1
    assert all(s in logs[0] for s in ("t_a486319d", "ob_66296ecbf9d0", "sent-at=", "delivered-at="))
    assert asyncio.run(fresh.drop_stale(event)) is True
    assert len(logs) == 1


@pytest.mark.parametrize("state", obligations.OPEN_STATES)
def test_open_obligation_delivers_unchanged_after_send_tag(store, decisions, state):
    path, _, logs = decisions
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE obligations SET state = ?", (state,))
    text = _text(["ob_66296ecbf9d0"])
    event = _enqueue(store, text)
    assert "sent-at=" in event.text
    assert event.text.splitlines()[1:] == text.splitlines()[1:]
    sent = event.text
    assert asyncio.run(fresh.drop_stale(event)) is False
    assert event.text == sent
    assert logs == []


def test_mixed_wake_removes_only_resolved_decision(store, decisions):
    path, _, logs = decisions
    event = _enqueue(store, _text(["ob_66296ecbf9d0", "ob_2baa438fdfca"]))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE obligations SET state = 'resolved' WHERE id = 'ob_66296ecbf9d0'")
    assert asyncio.run(fresh.drop_stale(event)) is False
    assert "ob_66296ecbf9d0" not in event.text
    assert "ob_2baa438fdfca" in event.text
    assert "has 1 open decision(s)" in event.text
    assert len(logs) == 1
    assert asyncio.run(fresh.drop_stale(event)) is False
    assert len(logs) == 1


def test_unreadable_board_keeps_wake(store, decisions, monkeypatch):
    event = _enqueue(store, _text(["ob_66296ecbf9d0"]))
    def unreadable(**kwargs):
        raise OSError("unavailable")
    monkeypatch.setattr(kb, "connect_readonly", unreadable)
    assert asyncio.run(fresh.drop_stale(event)) is False
    assert decisions[2] == []


def test_merged_human_text_is_never_removed(store, decisions):
    event = _enqueue(store, _text(["ob_66296ecbf9d0"]))
    event.text += "\nHuman: also inspect this"
    with sqlite3.connect(decisions[0]) as conn:
        conn.execute("UPDATE obligations SET state = 'resolved'")
    assert asyncio.run(fresh.drop_stale(event)) is False
    assert "Human: also inspect this" in event.text
    assert decisions[1:] == ([], [])


def test_multiline_summary_and_named_board(store, decisions):
    text = _text(["ob_66296ecbf9d0"]).replace("| decision", "| decision\nmore context")
    text = text.replace("--board default", "--board voice")
    event = _enqueue(store, text)
    with sqlite3.connect(decisions[0]) as conn:
        conn.execute("UPDATE obligations SET state = 'resolved'")
    assert asyncio.run(fresh.drop_stale(event)) is True
    assert decisions[1] == ["voice"]


def test_unrelated_control_wake_is_unchanged(store, decisions):
    event = _enqueue(store, "ordinary wake: ob_66296ecbf9d0")
    assert event.text == "ordinary wake: ob_66296ecbf9d0"
    assert asyncio.run(fresh.drop_stale(event)) is False
    assert decisions[1:] == ([], [])
