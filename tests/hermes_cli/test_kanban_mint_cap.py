"""Per-creator mint cap on ``create_task`` (Ace 2026-11-05: 477 of 710 cards in 24 h were bot-minted).

Every minter passes through ``create_task``; the (N+1)th card by an automated ``created_by`` in a rolling
24 h window is refused with :class:`MintCapError`. Humans and the operator CLI default are never capped,
idempotent re-mints never count, ``force_reason`` overrides and is ledgered, ``HERMES_KANBAN_MINT_CAP=0``
turns it off.
"""
from __future__ import annotations

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


def _mint(conn, by, n, **kw):
    return [kb.create_task(conn, title="main-tip red: repo · lint %d" % i, created_by=by, **kw) for i in range(n)]


def test_automated_creator_is_refused_past_the_cap(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "3")
    with kb.connect() as conn:
        ids = _mint(conn, "main-tip-red", 3)
        assert len(set(ids)) == 3
        with pytest.raises(kb.MintCapError) as ei:
            kb.create_task(conn, title="main-tip red: repo · lint 99", created_by="main-tip-red")
        assert ei.value.created_by == "main-tip-red"
        assert (ei.value.count, ei.value.cap) == (3, 3)
        assert "HERMES_KANBAN_MINT_CAP=0" in str(ei.value)
        # Nothing was written by the refused call.
        assert kb.mint_count_24h(conn, "main-tip-red") == 3


def test_cap_is_per_creator(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "2")
    with kb.connect() as conn:
        _mint(conn, "main-tip-red", 2)
        # A different automated creator has its own window.
        assert kb.create_task(conn, title="P40 #alerts page with no owner: x", created_by="pager-owner-card")


@pytest.mark.parametrize("by", [None, "", "user", "human:apollo", "human:ace", "apollo"])
def test_humans_and_operator_cli_are_never_capped(kanban_home, monkeypatch, by):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "1")
    with kb.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title="human card %d" % i, created_by=by)
        assert not kb.mint_cap_applies(by)


def test_idempotent_remint_returns_existing_and_does_not_count(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "1")
    with kb.connect() as conn:
        first = kb.create_task(conn, title="CI starved: repo", created_by="ci-red-owner",
                               idempotency_key="ci-red-owner:repo")
        # At the cap now; the SAME signature re-minted returns the existing card instead of refusing.
        again = kb.create_task(conn, title="CI starved: repo", created_by="ci-red-owner",
                               idempotency_key="ci-red-owner:repo")
        assert again == first
        # A NEW signature from the same creator is what the cap refuses.
        with pytest.raises(kb.MintCapError):
            kb.create_task(conn, title="CI starved: other", created_by="ci-red-owner",
                           idempotency_key="ci-red-owner:other")


def test_force_reason_overrides_and_is_ledgered(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "1")
    with kb.connect() as conn:
        _mint(conn, "live-edit-card", 1)
        t = kb.create_task(conn, title="live edit: forced", created_by="live-edit-card",
                           force_reason="operator: Ace asked for this one now")
        events = [e.payload for e in kb.list_events(conn, t) if e.kind == "mint_cap_forced"]
        assert len(events) == 1
        assert events[0]["reason"] == "operator: Ace asked for this one now"
        assert events[0]["cap"] == 1


def test_window_is_rolling_24h(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "1")
    with kb.connect() as conn:
        t = kb.create_task(conn, title="old", created_by="prism-page-card")
        # Age the card past the window; the creator may mint again.
        conn.execute("UPDATE tasks SET created_at = created_at - ? WHERE id = ?", (kb.MINT_CAP_WINDOW_S + 1, t))
        assert kb.create_task(conn, title="new", created_by="prism-page-card")


def test_archived_cards_still_count(kanban_home, monkeypatch):
    """A reaped/archived card already cost a worker run and its CI; archiving must not refill the budget."""
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "1")
    with kb.connect() as conn:
        t = kb.create_task(conn, title="x", created_by="main-tip-red")
        conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (t,))
        with pytest.raises(kb.MintCapError):
            kb.create_task(conn, title="y", created_by="main-tip-red")


def test_kill_switch_zero_disables(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_MINT_CAP", "0")
    with kb.connect() as conn:
        assert len(set(_mint(conn, "main-tip-red", kb.MINT_CAP_PER_DAY + 5))) == kb.MINT_CAP_PER_DAY + 5


def test_default_cap_is_the_ruled_number(kanban_home):
    assert kb.mint_cap() == kb.MINT_CAP_PER_DAY == 24
    with kb.connect() as conn:
        _mint(conn, "landing-queue", 24)
        with pytest.raises(kb.MintCapError):
            kb.create_task(conn, title="one more", created_by="landing-queue")
