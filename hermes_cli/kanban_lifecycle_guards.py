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
