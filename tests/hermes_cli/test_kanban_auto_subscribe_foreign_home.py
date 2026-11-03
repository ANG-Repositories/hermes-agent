"""Auto-subscribe follows the card's HOME, never the calling chat (t_ea40ff83).

Incident 2026-11-03: a cron script queued from #github-alternatives minted
main-red fix cards with ``--session <the SLA lane's #apollo session>``. With
``kanban.cli_auto_subscribe`` on, the create subscribed the CALLING chat, so every
"handed off for review" line and wake reached #github-alternatives, whose Apollo
(correctly) refuses foreign cards, and the home session heard nothing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kbn

CALLER_SID = "20261018_220948_caller"
HOME_SID = "20260927_135034_home"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from gateway.session_context import reset_session_vars

    reset_session_vars()
    kb.init_db()
    (home / "config.yaml").write_text("kanban:\n  cli_auto_subscribe: true\n")
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        for sid, chat in ((CALLER_SID, "chat-caller"), (HOME_SID, "chat-home")):
            db.create_session(session_id=sid, source="discord", origin_json=json.dumps({
                "platform": "discord", "chat_id": chat, "chat_type": "group",
                "user_id": "u1", "scope_id": "g1"}))
    finally:
        db.close()
    # The caller is a turn in chat-caller (the detached-run / terminal bridge shape).
    monkeypatch.setenv("HERMES_SESSION_ID", CALLER_SID)
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "discord")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-caller")
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", "group")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "u1")
    return home


def _create(session: str) -> str:
    out = kc.run_slash(f'--board default create "card" --assignee worker1 --session {session}')
    m = re.search(r"Created\s+(t_[0-9a-f]+)\b", out)
    assert m, out
    return m.group(1)


def _chats(task_id: str) -> list[str]:
    conn = kb.connect()
    try:
        return sorted(s["chat_id"] for s in kbn.list_notify_subs(conn, task_id))
    finally:
        conn.close()


def test_foreign_home_subscribes_home_chat_not_caller(kanban_home):
    tid = _create(HOME_SID)
    assert _chats(tid) == ["chat-home"]


def test_own_home_subscribes_caller_chat(kanban_home):
    tid = _create(CALLER_SID)
    assert _chats(tid) == ["chat-caller"]


def test_operator_home_keeps_caller(kanban_home):
    """Any operator session may act on an operator pseudo-home: the minting chat stays."""
    tid = _create("operator:apollo")
    assert _chats(tid) == ["chat-caller"]


def test_foreign_home_in_another_thread_never_reaches_caller(kanban_home, monkeypatch):
    """The acceptance shape: N cards minted from chat-caller for a foreign home
    put zero subscriptions on chat-caller."""
    tids = [_create(HOME_SID) for _ in range(3)]
    assert all("chat-caller" not in _chats(t) for t in tids)


def test_unknown_home_session_subscribes_nobody(kanban_home):
    tid = _create("20260101_000000_gone")
    assert _chats(tid) == []


def test_tool_create_with_foreign_session_id_subscribes_home(kanban_home, monkeypatch):
    """Same rule on the agent-tool path (kanban_create with an explicit session_id)."""
    from tools import kanban_tools as kt

    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setattr(kt, "_persisted_session_id", lambda v: v or None)
    created = json.loads(kt._handle_create(
        {"board": "default", "title": "tool card", "assignee": "peer", "session_id": HOME_SID}))
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, created["task_id"]).session_id == HOME_SID
    assert created["ok"], created
    assert _chats(created["task_id"]) == ["chat-home"]


def test_tool_create_without_session_id_subscribes_caller(kanban_home, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setattr(kt, "_persisted_session_id", lambda v: v or None)
    created = json.loads(kt._handle_create(
        {"board": "default", "title": "tool card 2", "assignee": "peer"}))
    assert created["ok"], created
    assert _chats(created["task_id"]) == ["chat-caller"]


def _add_session(sid: str, origin: dict) -> None:
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        db.create_session(session_id=sid, source=origin["platform"], origin_json=json.dumps(origin))
    finally:
        db.close()


def _subs(task_id: str) -> list[dict]:
    conn = kb.connect()
    try:
        return [dict(s) for s in kbn.list_notify_subs(conn, task_id)]
    finally:
        conn.close()


def test_home_sub_carries_full_routing_identity(kanban_home):
    """Prism 99063f040047: alt participant id and delivery anchors come from the home."""
    _add_session("20261103_000000_feishu", {
        "platform": "feishu", "chat_id": "group-B", "chat_type": "group",
        "user_id": "open-1", "user_id_alt": "union-1", "parent_chat_id": "parent-B"})
    tid = _create("20261103_000000_feishu")
    (sub,) = _subs(tid)
    assert (sub["chat_id"], sub["user_id"], sub["user_id_alt"]) == ("group-B", "open-1", "union-1")
    meta = sub["delivery_metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    assert meta["parent_chat_id"] == "parent-B"


def test_same_chat_other_participant_home_keeps_home_user(kanban_home):
    """Prism b594b6ccfed9: a home in the caller's chat but another user's session is
    subscribed with the HOME's participant, never the caller's."""
    _add_session("20261103_000000_u2", {
        "platform": "discord", "chat_id": "chat-caller", "chat_type": "group",
        "user_id": "u2", "scope_id": "g1"})
    tid = _create("20261103_000000_u2")
    (sub,) = _subs(tid)
    assert (sub["chat_id"], sub["user_id"]) == ("chat-caller", "u2")


def test_tool_idempotency_hit_subscribes_persisted_home(kanban_home, monkeypatch):
    """Prism 8efbb3c7ece2: a retry that names another session but hits an existing
    card subscribes that card's real home, never the argument's chat."""
    from tools import kanban_tools as kt

    _add_session("20261103_000000_other", {
        "platform": "discord", "chat_id": "chat-other", "chat_type": "group", "user_id": "u3"})
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: False)
    monkeypatch.setattr(kt, "_persisted_session_id", lambda v: v or None)
    first = json.loads(kt._handle_create({"board": "default", "title": "idem", "assignee": "peer",
                                          "session_id": HOME_SID, "idempotency_key": "K1"}))
    again = json.loads(kt._handle_create({"board": "default", "title": "idem", "assignee": "peer",
                                          "session_id": "20261103_000000_other",
                                          "idempotency_key": "K1"}))
    assert again["task_id"] == first["task_id"]
    assert "chat-other" not in _chats(first["task_id"])
