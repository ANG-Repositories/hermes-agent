"""Per-unit shared-door lifecycle for kanban. No private ordering or admission."""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager

_log = logging.getLogger(__name__)
EVENT = "door_authorized"
RELEASED = "door_released"


class DoorHeld(RuntimeError):
    """No start was made; the dispatcher must return the claim without penalty."""


def enabled():
    from hermes_cli.fleet_placement import admission_demand
    return getattr(admission_demand.load_priority(), "priority_mode", "spawn-only") in ("fcfs", "classes")


def unit_id(conn, task_id):
    # The board path separates identical ids on different boards; no user prompt content.
    import hashlib
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    board = hashlib.sha256(path.encode()).hexdigest()[:16]
    row = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=? "
                       "AND COALESCE(outcome,'') != 'door_held'", (task_id,)).fetchone()
    return f"kb-{board}-{task_id}-{int(row[0])}"


def canonical_host(host=None):
    from hermes_cli.fleet_placement import placement
    table = placement.load_hosts()
    hid = table.resolve(host or "local")
    if not hid:
        raise DoorHeld(f"unknown door host: {host or 'local'}")
    return hid


def _receipt(conn, task, doc):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        if not kb._claim_still_held(conn, task):
            raise DoorHeld("claim lost before door receipt")
        kb._append_event(conn, task.id, EVENT, doc, run_id=task.current_run_id)


@contextmanager
def start(conn, task, host=None, *, reservation_id=None, client=None):
    """One authorization per actual start, upgraded in place when capacity was booked.

    Receipt precedes spawn. A repeated authorization never spawns again. An
    uncertain transport leaves the original ID intact, so its retry cannot spend
    twice. Pair-owned leases survive restart in task_events, not a process cache.
    """
    if not enabled():
        yield
        return
    if client is None:
        from hermes_cli import alloc_client as client
    from hermes_cli import kanban_headroom
    hid = canonical_host(host)
    # Before claim the next run count is N; after claim it is N+1.
    uid = unit_id(conn, task.id)
    prefix, count = uid.rsplit("-", 1)
    uid = f"{prefix}-{int(count) - 1}"
    row = conn.execute("SELECT asap FROM tasks WHERE id=?", (task.id,)).fetchone()
    try:
        if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND run_id=? AND kind=?",
                        (task.id, task.current_run_id, EVENT)).fetchone():
            raise DoorHeld("run already authorized; reconcile original start")
        # A direct spawn_now has not participated in a publisher tick yet.
        from hermes_cli.fleet_placement import placement
        need = placement.need_of(placement.load_hosts().footprint("kanban"))
        client.enqueue("kanban", hid, uid, need.cpus, need.ram_gb)
        if row and row[0]:
            client.mark_asap("kanban", hid, uid)
        ans = client.authorize_start("kanban", hid, uid, ttl_s=kanban_headroom.RESERVE_TTL_S,
                                     existing_reservation_id=reservation_id,
                                     existing_consumer="kanban" if reservation_id else None)
    except (client.Refused, client.Unreachable) as exc:
        raise DoorHeld(f"door unavailable: {exc}") from exc
    if not ans.get("authorized") or ans.get("reused"):
        raise DoorHeld(ans.get("reason") or "door already reserved; reconcile original start")
    doc = {"reservation_id": ans["reservation_id"], "consumer": f"kanban@{hid}",
           "host": hid, "unit_id": uid, "until": ans["until"]}
    try:
        _receipt(conn, task, doc)
        yield
    except BaseException:
        # Caller must stop an already-created worker before propagating an error.
        try:
            if client.release(doc["reservation_id"], consumer=doc["consumer"]):
                _mark_released(conn, task.id, task.current_run_id, doc)
        except (client.Refused, client.Unreachable):
            _log.exception("door release failed; durable receipt retained for sweep")
        raise


def spawn(conn, task, workspace, *, host, spawn, spawn_kwargs, state, reservation_id=None):
    from hermes_cli.kanban_db_dispatch import _stop_own_spawn
    with start(conn, task, host, reservation_id=reservation_id):
        try:
            return spawn(task, workspace, **spawn_kwargs)
        except BaseException:
            _stop_own_spawn(state.proc)
            raise


def defer(conn, task):
    """A held start is not a failure; preserve the waiting ID for the next run."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        if kb._claim_still_held(conn, task):
            retry = kb._retry_status_for_run(conn, task.id, task.current_run_id)
            conn.execute("UPDATE tasks SET status=?,claim_lock=NULL,claim_expires=NULL,"
                         "worker_pid=NULL WHERE id=?", (retry, task.id))
            kb._end_run(conn, task.id, outcome="door_held", metadata={"retry_status": retry})
            kb._append_event(conn, task.id, "door_held", {}, run_id=task.current_run_id)


def _mark_released(conn, tid, run_id, doc):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        kb._append_event(conn, tid, RELEASED, doc, run_id=run_id)


def maintain(conn, *, client=None):
    """Renew running authorizations and release finished runs, including old modes."""
    if client is None:
        from hermes_cli import alloc_client as client
    rows = conn.execute(
        "SELECT e.task_id,e.run_id,e.payload,t.status,t.current_run_id FROM task_events e "
        "JOIN tasks t ON t.id=e.task_id WHERE e.kind=? AND NOT EXISTS "
        "(SELECT 1 FROM task_events r WHERE r.task_id=e.task_id AND r.run_id=e.run_id AND r.kind=?)",
        (EVENT, RELEASED)).fetchall()
    from hermes_cli.kanban_headroom import RESERVE_TTL_S
    for row in rows:
        doc = json.loads(row["payload"])
        try:
            if row["status"] == "running" and row["current_run_id"] == row["run_id"]:
                if not client.renew(doc["reservation_id"], consumer=doc["consumer"], ttl_s=RESERVE_TTL_S):
                    _log.warning("door lease expired: %s run %s", row["task_id"], row["run_id"])
            elif client.release(doc["reservation_id"], consumer=doc["consumer"]):
                _mark_released(conn, row["task_id"], row["run_id"], doc)
        except (client.Refused, client.Unreachable):
            _log.exception("door lifecycle failed for %s; retry next tick", row["task_id"])
