"""Persistent entry guards for lifecycle states with mandatory exit data."""
from __future__ import annotations

import sqlite3


def install(conn: sqlite3.Connection) -> None:
    # Existing legacy rows remain readable/repairable. Only entering scheduled,
    # or clearing an existing wake, is rejected; unrelated edits stay possible.
    for operation in ("INSERT", "UPDATE OF status, next_eligible_at"):
        name = "insert" if operation == "INSERT" else "update"
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS tasks_scheduled_wake_{name}
            BEFORE {operation} ON tasks
            WHEN NEW.status = 'scheduled' AND NEW.next_eligible_at IS NULL
            BEGIN
                SELECT RAISE(ABORT, 'scheduled needs a wake time');
            END
        """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS tasks_review_handoff
        AFTER UPDATE OF status ON tasks
        WHEN NEW.status = 'review' AND OLD.status != 'review'
          AND NOT EXISTS (
            SELECT 1 FROM task_events WHERE task_id=NEW.id
            AND kind='review_requested'
            AND json_valid(payload)
            AND length(json_extract(payload, '$.implementer')) > 0
          )
        BEGIN
            INSERT INTO task_events(task_id, kind, payload, created_at)
            VALUES(NEW.id, 'review_requested', json_object(
                'implementer', COALESCE(
                    (SELECT profile FROM task_runs WHERE task_id=NEW.id
                     AND outcome IN ('completed','review_requested') AND profile IS NOT NULL
                     ORDER BY id DESC LIMIT 1),
                    CASE WHEN OLD.assignee != 'human' AND OLD.assignee NOT LIKE 'human:%'
                         THEN OLD.assignee END, 'human:operator'),
                'reviewer', NEW.assignee, 'source', 'lifecycle_entry_repair'),
                CAST(strftime('%s','now') AS INTEGER));
        END
    """)
    for operation in ("INSERT", "UPDATE OF status, assignee"):
        name = "insert" if operation == "INSERT" else "update"
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS tasks_human_ready_{name}
            AFTER {operation} ON tasks
            WHEN NEW.status = 'ready'
              AND (NEW.assignee = 'human' OR NEW.assignee LIKE 'human:%')
            BEGIN
                UPDATE tasks SET status='todo' WHERE id=NEW.id;
                INSERT INTO task_events(task_id,kind,payload,created_at)
                SELECT NEW.id, 'needs_operator',
                    json_object('assignee',NEW.assignee,'source','lifecycle_entry_repair'),
                    CAST(strftime('%s','now') AS INTEGER)
                WHERE NOT EXISTS (SELECT 1 FROM task_events
                    WHERE task_id=NEW.id AND kind='needs_operator');
            END
        """)
