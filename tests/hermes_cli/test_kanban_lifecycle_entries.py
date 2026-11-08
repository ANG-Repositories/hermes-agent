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


def test_review_entry_binds_implementer_before_any_exit(conn):
    tid = kb.create_task(conn, title="Recovered review", assignee="builder")
    conn.execute("UPDATE tasks SET status='review',assignee='human:apollo' WHERE id=?", (tid,))
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='review_requested' ORDER BY id DESC LIMIT 1",
        (tid,),
    ).fetchone()
    assert event is not None
    assert json.loads(event[0])["implementer"] == "builder"


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


def test_untyped_block_requires_a_recovery_predicate(conn):
    tid = kb.create_task(conn, title="Missing recovery", assignee="builder")
    with pytest.raises(kb.BlockRefused, match="kind|unblock_when"):
        kb.block_task(conn, tid, reason="stuck")
    assert kb.get_task(conn, tid).status == "ready"
