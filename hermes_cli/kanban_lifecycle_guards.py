"""Persistent entry guards for lifecycle states with mandatory exit data."""
from __future__ import annotations

import re
import sqlite3


def has_event_gate(body: str | None) -> bool:
    return bool(re.search(r"(?m)^unblock_when:[ \t]*event:[A-Za-z0-9_.:-]+[ \t]*$", body or ""))


def install(conn: sqlite3.Connection) -> None:
    # Existing legacy rows remain readable/repairable. Only entering scheduled,
    # or clearing an existing wake, is rejected; unrelated edits stay possible.
    for operation in ("INSERT", "UPDATE OF status, next_eligible_at, body"):
        name = "insert" if operation == "INSERT" else "update"
        conn.execute(f"DROP TRIGGER IF EXISTS tasks_scheduled_wake_{name}")
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS tasks_scheduled_wake_{name}
            BEFORE {operation} ON tasks
            WHEN NEW.status = 'scheduled' AND NEW.next_eligible_at IS NULL
              AND NOT EXISTS (
                WITH RECURSIVE lines(line, rest) AS (
                    SELECT '', COALESCE(NEW.body, '') || char(10)
                    UNION ALL SELECT substr(rest, 1, instr(rest, char(10))-1),
                                     substr(rest, instr(rest, char(10))+1)
                    FROM lines WHERE rest != ''
                )
                SELECT 1 FROM lines WHERE line LIKE 'unblock_when:%'
                AND trim(substr(line, 14)) LIKE 'event:%'
                AND length(trim(substr(line, 14))) > 6
                AND substr(trim(substr(line, 14)), 7) NOT GLOB '*[^A-Za-z0-9_.:-]*'
              )
            BEGIN
                SELECT RAISE(ABORT, 'scheduled needs a wake time');
            END
        """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS tasks_review_handoff_insert
        AFTER INSERT ON tasks
        WHEN NEW.status = 'review'
        BEGIN
            INSERT INTO task_events(task_id, kind, payload, created_at)
            VALUES(NEW.id, 'review_requested', json_object(
                'implementer', COALESCE(NEW.assignee, 'human:operator'),
                'reviewer', NEW.assignee, 'source', 'lifecycle_entry_repair'),
                CAST(strftime('%s','now') AS INTEGER));
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
