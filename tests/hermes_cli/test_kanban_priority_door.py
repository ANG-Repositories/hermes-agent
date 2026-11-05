"""Exercise the real spawn boundary against the pinned shared-client contract."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_priority_door as door


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    kb.init_db()
    with kb.connect_closing() as conn:
        yield conn


class Client:
    class Refused(Exception):
        pass

    class Unreachable(Exception):
        pass

    def __init__(self):
        self.calls = []
        self.reused = False
        self.authorized = True

    def enqueue(self, *args):
        self.calls.append(("enqueue", args))

    def withdraw(self, *args):
        self.calls.append(("withdraw", args))

    def publish_demand(self, *args):
        self.calls.append(("verdict", args))

    def mark_asap(self, *args):
        self.calls.append(("mark_asap", args))

    def authorize_start(self, *args, **kwargs):
        self.calls.append(("authorize", args, kwargs))
        return {"authorized": self.authorized, "reused": self.reused,
                "reservation_id": "same-row", "until": 9999999999}

    def release(self, rid, **kwargs):
        self.calls.append(("release", rid, kwargs))
        return True

    def renew(self, rid, **kwargs):
        self.calls.append(("renew", rid, kwargs))
        return True


def test_actual_launch_upgrades_once_and_reconciles_without_second_spawn(board, monkeypatch):
    client = Client()
    monkeypatch.setattr(door, "enabled", lambda: True)
    monkeypatch.setattr(door, "canonical_host", lambda host: "ace-ai")
    from hermes_cli.fleet_placement import placement
    monkeypatch.setattr(placement, "load_hosts", lambda: SimpleNamespace(
        footprint=lambda workload: placement.Footprint(threads=1, ram_gb=3)))
    original = door.start
    monkeypatch.setattr(door, "start", lambda *args, **kwargs: original(*args, **kwargs, client=client))
    tid = kb.create_task(board, title="start", assignee=None)
    before = door.unit_id(board, tid)
    task = kb.claim_task(board, tid)
    assert task is not None
    monkeypatch.setattr("hermes_cli.kanban_continuation.record_placement", lambda *args: True)
    launches = []
    args = dict(board=None, spawn=lambda *args: launches.append(tid), spawn_kwargs={}, host="ace-ai",
                pool=None, placement_record=lambda: {}, state=dispatch._LaunchState(), reservation_id="same-row")
    assert dispatch._launch_claimed(board, task, "/scratch", **args) == dispatch._LAUNCH_OK
    assert launches == [tid]
    authorize = next(c for c in client.calls if c[0] == "authorize")
    assert authorize[1] == ("kanban", "ace-ai", before)
    assert authorize[2]["existing_reservation_id"] == "same-row"
    assert authorize[2]["existing_consumer"] == "kanban"
    assert not any(c[0] == "mark_asap" for c in client.calls)
    with pytest.raises(door.DoorHeld, match="already authorized"):
        dispatch._launch_claimed(board, task, "/scratch", **args)
    assert launches == [tid]
    door.maintain(board, client=client)
    assert client.calls[-1][0] == "renew"
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        kb._end_run(board, tid, outcome="completed")
    door.maintain(board, client=client)
    assert client.calls[-1] == ("release", "same-row", {"consumer": "kanban@ace-ai"})
    count = len(client.calls)
    door.maintain(board, client=client)
    assert len(client.calls) == count
    # Publication is per board and per explicit card, never parent urgency.
    from hermes_cli import kanban_priority as priority
    table = SimpleNamespace(footprint=lambda workload: placement.Footprint(threads=1, ram_gb=3),
                            resolve=lambda host: host)
    monkeypatch.setattr(placement, "load_hosts", lambda: table)
    child = kb.create_task(board, title="child", assignee=None, parents=[tid])
    priority.publish_waiting(board, {"ace-ai": [{"id": child, "asap": False}]}, client=client)
    assert client.calls[-1][0] == "enqueue"
    child_uid = door.unit_id(board, child)
    priority.publish_waiting(board, {"ace-ai": []}, client=client)
    assert client.calls[-1] == ("withdraw", ("kanban", "ace-ai", child_uid))


def test_failed_spawn_releases_and_held_attempt_keeps_arrival_id(board, monkeypatch):
    client = Client()
    monkeypatch.setattr(door, "enabled", lambda: True)
    monkeypatch.setattr(door, "canonical_host", lambda host: "ace-ai")
    from hermes_cli.fleet_placement import placement
    monkeypatch.setattr(placement, "load_hosts", lambda: SimpleNamespace(
        footprint=lambda workload: placement.Footprint(threads=1, ram_gb=3)))
    tid = kb.create_task(board, title="retry", assignee=None)
    uid = door.unit_id(board, tid)
    task = kb.claim_task(board, tid)
    client.authorized = False
    with pytest.raises(door.DoorHeld):
        with door.start(board, task, "ace-ai", client=client):
            pytest.fail("held door must not spawn")
    door.defer(board, task)
    assert door.unit_id(board, tid) == uid
    task = kb.claim_task(board, tid)
    client.authorized = True
    with pytest.raises(RuntimeError, match="spawn failed"):
        with door.start(board, task, "ace-ai", client=client):
            raise RuntimeError("spawn failed")
    assert client.calls[-1] == ("release", "same-row", {"consumer": "kanban@ace-ai"})
