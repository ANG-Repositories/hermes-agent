"""Auth-recovery regression through the real alloc_client and loopback door."""
import json
import logging

import pytest

from tests.hermes_cli.test_kanban_door_round5 import door_fleet, fleet, POOL, _card, _uid
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_priority as priority
from hermes_cli import kanban_priority_door as door
from hermes_cli import kanban_priority_faults as faults
from hermes_cli.fleet_placement import alloc_client, admission_demand


def _pending(conn):
    return json.loads(door._published_path(conn).read_text()).get("pending_withdrawals", [])


def test_pair_token_recovery_retries_durable_withdrawal(door_fleet, caplog):
    tid = _card()
    uid = _uid(tid)
    token = door_fleet.root / alloc_client.TOKENS_REL / f"kanban@{POOL}"
    saved = token.read_bytes()
    caplog.set_level(logging.WARNING, logger=priority.__name__)
    with kbc.connect_closing() as conn:
        assert priority.publish_waiting(conn, {POOL: [{"id": tid, "asap": False}]}) == {}
        assert uid in door_fleet.allocd.waiting(POOL)
        token.unlink()
        assert priority.publish_waiting(conn, {}) == {}
        assert faults.latched() is None
        assert door.load_published(conn) == set()
        pending = _pending(conn)
        assert [(p["host"], p["unit_id"]) for p in pending] == [(POOL, uid)]
        assert pending[0]["first_seen"] > 0 and pending[0]["last_reason"]
        warnings = len(caplog.records)
        assert priority.publish_waiting(conn, {}) == {}
        assert len(caplog.records) == warnings
        assert faults.latched() is None
        # Another writer must not erase the pending list.
        door.save_published(conn, door.load_published(conn))
    token.write_bytes(saved)
    with kbc.connect_closing() as conn:
        assert priority.publish_waiting(conn, {}) == {}
        assert uid not in door_fleet.allocd.waiting(POOL)
        assert _pending(conn) == []


def test_pending_expires_at_server_waiting_ttl(door_fleet, monkeypatch, caplog):
    monkeypatch.setattr(priority, "_table_hosts", lambda: {POOL})
    with kbc.connect_closing() as conn:
        door.save_published(conn, set())
        path = door._published_path(conn)
        doc = json.loads(path.read_text())
        doc["pending_withdrawals"] = [{"host": POOL, "unit_id": "old", "first_seen": 100,
                                        "last_reason": "auth"}]
        path.write_text(json.dumps(doc))
        monkeypatch.setattr("time.time", lambda: 101 + admission_demand.max_age_s())
        monkeypatch.setattr(alloc_client, "withdraw", lambda *a: pytest.fail("expired entry retried"))
        caplog.set_level(logging.INFO, logger=priority.__name__)
        assert priority.publish_waiting(conn, {}) == {}
        assert _pending(conn) == []
        assert "expired pending withdrawal" in caplog.text


def test_real_http404_is_not_auth_and_keeps_record(door_fleet, monkeypatch):
    uid = "http404-unit"
    monkeypatch.setattr(door_fleet.allocd, "route", lambda *a: (404, {"error": "missing endpoint"}))
    with kbc.connect_closing() as conn:
        door.save_published(conn, {(POOL, uid)})
        assert priority.publish_waiting(conn, {})
        assert door.load_published(conn) == {(POOL, uid)}
        assert faults.latched()
        assert _pending(conn) == []
