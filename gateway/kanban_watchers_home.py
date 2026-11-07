"""Resolve a kanban card's home Discord chat from its home session.

The needs-input pager pages the channel in a card's ``origin:`` line. Cards
minted from a CLI/cron/worker context carry no channel id there, yet their
``session_id`` names a Discord session (t_c5800c91: 42 of the 55 #alerts
needs-ruling lines on 2026-11-04 came from such cards and fell back to
#alerts). Same read as ``scripts/kanban-unblock-when-sweep.py::home_chat``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _state_db_paths(root: Path) -> list[Path]:
    return [root / "state.db", *sorted((root / "profiles").glob("*/state.db"))]


def session_home_target(session_id: Optional[str], root: Optional[Path] = None) -> Optional[tuple[str, str]]:
    """Return (Discord channel/thread, transport profile), else None.

    Persisted receiving-bot ownership wins over the store owner: a shared bot
    may serve a session running under another profile. Legacy rows without
    transport ownership use the profile whose store matched.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return None
    if root is None:
        from hermes_cli.kanban_db import kanban_home

        root = kanban_home()
    for db in _state_db_paths(Path(root)):
        if not db.is_file():
            continue
        try:
            con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
            try:
                con.row_factory = sqlite3.Row
                row = con.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            logger.debug("kanban needs-input: session store %s unreadable", db, exc_info=True)
            continue
        if row is None:
            continue
        try:
            origin = json.loads(row["origin_json"] or "null")
        except (TypeError, ValueError):
            return None
        if not isinstance(origin, dict) or str(origin.get("platform") or "").lower() != "discord":
            return None
        for value in (origin.get("thread_id"), origin.get("chat_id")):
            if str(value or "").isdigit():
                owner = "default" if db.parent == Path(root) else db.parent.name
                transport = row["transport_profile"] if "transport_profile" in row.keys() else None
                return str(value), str(transport or owner)
        return None
    return None
