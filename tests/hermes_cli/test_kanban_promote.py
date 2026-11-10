"""Tests for the kanban `promote` verb (issue #28822).

The realistic bug scenario from #28822 is: a child task ends up in
``todo`` with all its parents already ``done`` (because the
auto-promote daemon hasn't run, or a manual close raced it).
Direct-SQL setup is used to construct that state deterministically.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


def _stuck_todo(conn, *, parents_done=True, n_parents=1):
    """Build the #28822 scenario: child in 'todo' whose parents may
    have closed as 'done' without the auto-promote logic firing.
    """
    parent_ids = [
        kb.create_task(conn, title=f"parent{i}", assignee="setup")
        for i in range(n_parents)
    ]
    child_id = kb.create_task(
        conn, title="child", parents=parent_ids, assignee="setup"
    )
    assert kb.get_task(conn, child_id).status == "todo"
    if parents_done:
        for pid in parent_ids:
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (pid,)
            )
    return child_id, parent_ids


def test_promote_stuck_todo_succeeds(conn):
    child, _ = _stuck_todo(conn, parents_done=True)
    ok, err = kb.promote_task(conn, child, actor="tester")
    assert ok and err is None
    assert kb.get_task(conn, child).status == "ready"


def test_promote_refuses_undone_parent_and_names_the_real_remedy(conn):
    # #106195: promotion must never report a 'ready' that the first claim reverts.
    child, (parent,) = _stuck_todo(conn, parents_done=False)
    ok, err = kb.promote_task(conn, child, actor="tester", reason="recovery")
    assert not ok
    assert err and parent in err
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None  # still gated; nothing pretended




# ---------------------------------------------------------------------------
# CLI `_cmd_promote` — bulk via `--ids` (the issue's anti-respawn use case:
# promote all children of a closed parent in one command).
# ---------------------------------------------------------------------------


def _promote_ns(task_id, *, ids=None, reason=None, force=False,
                dry_run=False, as_json=False):
    return argparse.Namespace(
        task_id=task_id,
        reason=list(reason or []),
        ids=list(ids or []) or None,
        force=force,
        dry_run=dry_run,
        json=as_json,
    )


def test_cli_promote_bulk_ids_promotes_all(kanban_home, capsys):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent")
        children = [
            kb.create_task(conn, title=f"c{i}", parents=[parent])
            for i in range(3)
        ]
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    rc = kb_cli._cmd_promote(_promote_ns(children[0], ids=children[1:]))
    assert rc == 0
    out = capsys.readouterr().out
    for c in children:
        assert c in out
    with kbc.connect() as conn:
        for c in children:
            assert kb.get_task(conn, c).status == "ready"




# ---------------------------------------------------------------------------
# t_102864fa: a stale land script replaying promote/triage-resolve on a card
# that is already terminal must be a refused no-op that says so, like complete.

def _event_count(conn, tid):
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)
    ).fetchone()[0]


@pytest.mark.parametrize("terminal", ["done", "archived"])
def test_promote_on_terminal_card_is_a_refused_noop(conn, terminal):
    tid = kb.create_task(conn, title="t", assignee="setup")
    conn.execute(
        "UPDATE tasks SET status = ?, completed_at = 1 WHERE id = ?", (terminal, tid)
    )
    before = _event_count(conn, tid)
    ok, err = kb.promote_task(conn, tid, actor="tester")
    assert not ok
    assert err and ("already done" in err or "archived" in err)
    assert kb.get_task(conn, tid).status == terminal
    assert _event_count(conn, tid) == before


@pytest.mark.parametrize("terminal", ["done", "archived"])
def test_triage_resolve_on_terminal_card_is_a_refused_noop(conn, terminal):
    tid = kb.create_task(conn, title="t", assignee="setup")
    conn.execute(
        "UPDATE tasks SET status = ?, completed_at = 1 WHERE id = ?", (terminal, tid)
    )
    before = _event_count(conn, tid)
    ok, err = kb.triage_resolve_task(conn, tid, to="done", reason="replay", actor="tester")
    assert not ok
    assert err and ("already done" in err or "archived" in err)
    assert "complete instead" not in err
    assert kb.get_task(conn, tid).status == terminal
    assert _event_count(conn, tid) == before


# ---------------------------------------------------------------------------
# t_8b654880: the tasks_human_ready_* triggers park a human card in 'todo', so
# promote printed "-> ready" over a todo card and complete refused todo: an
# operator could never close a human:* card. Promote must not claim a status the
# card does not hold, and the operator must be able to close it from todo.

@pytest.mark.parametrize("assignee", ["human", "human:apollo"])
def test_human_card_promote_never_reports_ready_and_operator_completes_from_todo(
    conn, capsys, assignee,
):
    tid = kb.create_task(conn, title="operator card", assignee=assignee)
    assert kb.get_task(conn, tid).status == "todo"  # trigger parked it

    for dry_run in (True, False):
        assert kb_cli._cmd_promote(_promote_ns(tid, dry_run=dry_run)) == 1
        output = capsys.readouterr()
        assert "-> ready" not in output.out
        assert "human cards stay in 'todo'" in output.err
        assert f"hermes kanban complete {tid}" in output.err
        assert kb.get_task(conn, tid).status == "todo"
    assert not any(e.kind == "promoted_manual" for e in kb.list_events(conn, tid))

    assert kb.complete_task(conn, tid, summary="shipped elsewhere")
    task = kb.get_task(conn, tid)
    assert task.status == "done"
    audit = conn.execute(
        "SELECT old_status, new_status FROM task_status_audit WHERE task_id=? "
        "ORDER BY id DESC LIMIT 1", (tid,),
    ).fetchone()
    assert tuple(audit) == ("todo", "done")


def test_todo_completion_preserves_worker_parent_and_run_guards(conn):
    worker = kb.create_task(conn, title="unstarted worker", assignee="setup")
    conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (worker,))
    assert not kb.complete_task(conn, worker, summary="not started")

    parent = kb.create_task(conn, title="unfinished prerequisite", assignee="setup")
    human = kb.create_task(conn, title="gated operator", assignee="human:apollo", parents=[parent])
    assert not kb.complete_task(conn, human, summary="still gated")
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
    assert not kb.complete_task(conn, human, summary="stale worker", expected_run_id=123)
    assert kb.get_task(conn, human).status == "todo"
    assert kb.complete_task(conn, human, summary="prerequisite complete")

