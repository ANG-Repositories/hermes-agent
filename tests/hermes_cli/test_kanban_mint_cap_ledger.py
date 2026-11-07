"""Mint-cap refusals and bypasses are ledgered (t_f956f87f).

2026-11-06: `live-edit-card` minted 15 cards past the 24/24h cap with no `mint_cap_forced` event, because an
operator ran the release with ``HERMES_KANBAN_MINT_CAP=0``. A refusal left no durable trace at all (it raises
before any row exists), so "did the cap kick in this week" could only be recomputed from created_at, and a
kill-switch bypass was invisible. Pinned here: every refusal appends one ``refused`` row to the board's
``<db>.mint-cap.jsonl`` ledger; a ``--force`` appends ``forced``; a card minted past the default cap while the kill
switch is set appends ``bypassed`` AND carries a ``mint_cap_bypassed`` event. A ledger write failure never
changes the create outcome.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_MINT_CAP", raising=False)
    kb.init_db()
    return home


def _rows(conn):
    p = kb.mint_cap_ledger_path(conn)
    if p is None or not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def test_refusal_appends_one_ledger_row(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "2")
    with kb.connect() as conn:
        for i in range(2):
            kb.create_task(conn, title="t%d" % i, created_by="live-edit-card")
        for _ in range(3):
            with pytest.raises(kb.MintCapError):
                kb.create_task(conn, title="over", created_by="live-edit-card", idempotency_key="k:over")
        rows = _rows(conn)
    assert [r["outcome"] for r in rows] == ["refused"] * 3
    assert {(r["created_by"], r["count"], r["cap"]) for r in rows} == {("live-edit-card", 2, 2)}
    assert all(isinstance(r["at"], int) and r["idempotency_key"] == "k:over" for r in rows)
    assert kb.mint_cap_ledger_path(conn).parent == Path(kb.kanban_db_path()).parent


def test_force_is_ledgered_as_forced(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "1")
    with kb.connect() as conn:
        kb.create_task(conn, title="a", created_by="main-tip-red")
        t = kb.create_task(conn, title="b", created_by="main-tip-red", force_reason="operator: needed now")
        rows = _rows(conn)
    assert [(r["outcome"], r["task_id"], r["reason"]) for r in rows] == [("forced", t, "operator: needed now")]


def test_kill_switch_mint_past_the_default_cap_is_ledgered_and_evented(kanban_home, monkeypatch):
    """The 11-06 16:21 shape: HERMES_KANBAN_MINT_CAP=0 in the env, the creator already at the default cap."""
    with kb.connect() as conn:
        for i in range(kb.MINT_CAP_PER_DAY):
            kb.create_task(conn, title="t%d" % i, created_by="live-edit-card")
        assert _rows(conn) == []
        monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "0")
        t = kb.create_task(conn, title="past", created_by="live-edit-card")
        rows = _rows(conn)
        ev = [e.payload for e in kb.list_events(conn, t) if e.kind == "mint_cap_bypassed"]
    assert [(r["outcome"], r["task_id"], r["count"], r["cap"]) for r in rows] == [
        ("bypassed", t, kb.MINT_CAP_PER_DAY, kb.MINT_CAP_PER_DAY)]
    assert ev and ev[0]["count"] == kb.MINT_CAP_PER_DAY and ev[0]["env"] == "HERMES_KANBAN_MINT_CAP=0"


def test_kill_switch_under_the_default_cap_writes_nothing(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "0")
    with kb.connect() as conn:
        kb.create_task(conn, title="a", created_by="live-edit-card")
        kb.create_task(conn, title="h", created_by="user")
        assert _rows(conn) == []


def test_ledger_write_failure_never_changes_the_outcome(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "1")
    monkeypatch.setattr(kb, "mint_cap_ledger_path", lambda conn: Path("/nonexistent-dir/x/ledger.jsonl"))
    with kb.connect() as conn:
        kb.create_task(conn, title="a", created_by="prism-page-card")
        with pytest.raises(kb.MintCapError):
            kb.create_task(conn, title="b", created_by="prism-page-card")
