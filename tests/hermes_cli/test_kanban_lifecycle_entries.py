"""Lifecycle entry contracts apply to raw writers as well as kernel verbs."""
import json
import sqlite3
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path: Path):
    with kb.connect_closing(tmp_path / "kanban.db") as db:
        yield db


@pytest.mark.parametrize("current_run", [False, True])
def test_review_entry_binds_implementer_before_any_exit(conn, current_run):
    tid = kb.create_task(conn, title="Recovered review", assignee="builder")
    if current_run:
        conn.execute("INSERT INTO task_runs(task_id,profile,status,outcome,started_at) VALUES(?,'old-builder','completed','completed',1)", (tid,))
        kb.claim_task(conn, tid)
    conn.execute("UPDATE tasks SET status='review',assignee='human:apollo' WHERE id=?", (tid,))
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert event is not None
    assert json.loads(event[0])["implementer"] == "builder"
    kb._append_event(conn, tid, "review_requested", {"implementer": "builder", "reviewer": "human:apollo", "summary": "verified"})
    handoffs = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested'", (tid,)).fetchall()
    assert len(handoffs) == 1
    assert json.loads(handoffs[0][0])["summary"] == "verified"

    # A new raw INSERT has no prior run/assignee to recover.
    conn.execute("INSERT INTO tasks(id,title,status,created_at) VALUES('raw-review','Raw review','review',1)")
    payload = conn.execute("SELECT payload FROM task_events WHERE task_id='raw-review' AND kind='review_requested'").fetchone()[0]
    assert json.loads(payload)["implementer"] == "human:operator"


def test_human_ready_is_operator_todo_and_cannot_promote(conn):
    tid = kb.create_task(conn, title="Operator action", assignee="human:apollo")
    assert kb.get_task(conn, tid).status == "todo"
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "todo"
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.get_task(conn, tid).status == "todo"
    events = conn.execute(
        "SELECT count(*) FROM task_events WHERE task_id=? AND kind='needs_operator'", (tid,),
    ).fetchone()[0]
    assert events == 1
    assert kb.assign_task(conn, tid, "builder")
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "ready"
    conn.execute("UPDATE tasks SET status='todo',assignee='human:apollo' WHERE id=?", (tid,))
    kb._append_event(conn, tid, "dependency_wait", {"source_status": "review", "kind": "dependency"})
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "review"


def test_untyped_block_requires_a_recovery_predicate(conn):
    tid = kb.create_task(conn, title="Missing recovery", assignee="builder")
    with pytest.raises(kb.BlockRefused, match="kind|unblock_when"):
        kb.block_task(conn, tid, reason="stuck")
    assert kb.get_task(conn, tid).status == "ready"


def test_legacy_repair_is_dry_run_first_and_idempotent(conn):
    from hermes_cli.kanban_lifecycle_repair import repair

    conn.execute("DROP TRIGGER tasks_human_ready_insert")
    conn.execute("DROP TRIGGER tasks_human_ready_update")
    tid = kb.create_task(conn, title="Legacy operator", assignee="human:apollo")
    conn.commit()
    assert repair(conn)["before"] == 1
    assert kb.get_task(conn, tid).status == "ready"
    report = repair(conn, apply=True)
    assert (report["before"], report["after"]) == (1, 0)
    assert kb.get_task(conn, tid).status == "todo"
    assert repair(conn, apply=True)["before"] == 0
    held = kb.create_task(conn, title="Legacy review hold", assignee="reviewer")
    conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (held,))
    kb._append_event(conn, held, "blocked", {"source_status": "review"})
    conn.commit()
    repair(conn, apply=True)
    assert kb._resume_status_from_events(conn, held) == "review"
