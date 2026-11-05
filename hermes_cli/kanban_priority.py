"""Kanban's adapter to the fleet admission door; ordering belongs to admission."""
from __future__ import annotations

import math
import os


def set_asap(conn, task_id: str, flag: bool, *, operator: str) -> bool:
    """Only an explicit operator edit may mark a card urgent; creation never copies it."""
    from hermes_cli import kanban_db as kb
    if os.environ.get("HERMES_KANBAN_TASK") or kb._is_delegated_child():
        raise PermissionError("ASAP is operator-set only")
    from hermes_cli.kanban_db import _home_session_guarded
    # The same ownership guard as operator model edits; a caller-supplied
    # label alone must never authorize a foreign session's mutation.
    return _home_session_guarded("set-asap")(_set_asap)(conn, task_id, flag, operator=operator)


def _set_asap(conn, task_id, flag, *, operator):
    from hermes_cli import kanban_db as kb
    if not operator or not operator.strip():
        raise PermissionError("ASAP requires an operator identity")
    with kb.write_txn(conn):
        row = conn.execute("SELECT asap FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return False
        if bool(row["asap"]) != bool(flag):
            conn.execute("UPDATE tasks SET asap=? WHERE id=?", (int(bool(flag)), task_id))
            kb._append_event(conn, task_id, "asap_set", {"asap": bool(flag), "operator": operator})
    kb.notify_task_updated(conn, task_id, ("asap",))
    return True


def waiting_by_host(cards, plan, *, routes, local_host: str, board=None, default_assignee=None,
                    conn=None, caps=None, cap_only=None, worktree_ports=None):
    """Use the dispatcher's resolved routes, never its pressure-filtered slot budget.

    No age or workload ordering is decided here. The common publisher selects its
    urgency-first visible window; retain explicit urgent cards before the slot cap.
    """
    from hermes_cli.kanban_worker_pool import count_free

    out = {local_host: []}
    if plan is None:
        hosts = {}
    else:
        hosts = plan.hosts
    out.update({name: [] for name in hosts})
    own_only = cap_only or {}
    for card in cards:
        route, arg = routes.get(card["id"], ("local", None))
        if route == "wait":
            continue
        assignee = card["assignee"] or default_assignee
        if not assignee:
            continue
        if route in ("local", "portable") and not own_only.get(card["id"]):
            out[local_host].append(card)
        if plan is None or route == "local":
            continue
        only = None
        if route == "portable" and conn is not None:
            from hermes_cli.kanban_db_dispatch import _portable_only
            only, _ = _portable_only(conn, card, assignee, plan, caps,
                                     own_only.get(card["id"]),
                                     (worktree_ports or {}).get(card["id"]))
            if only is not None and local_host.lower() not in {n.lower() for n in only}:
                out[local_host] = [r for r in out[local_host] if r["id"] != card["id"]]
        for name, host in plan.hosts.items():
            if route in ("pin", "origin") and name != arg:
                continue
            if only is not None and name.lower() not in {n.lower() for n in only}:
                continue
            if (not host.enabled or host.state != "active" or name in plan.disabled
                    or assignee not in host.profiles or plan.board_refusal(name, board)
                    or plan.detail.get(name, {}).get("reachable") is False):
                continue
            out[name].append(card)
    if plan is not None:
        for name, host in hosts.items():
            capacity = count_free(host, int(plan.detail.get(name, {}).get("running", 0)))
            capacity = max(0, capacity - plan.taken.get(name, 0))
            rows = out[name]
            out[name] = ([r for r in rows if r["asap"]]
                         + [r for r in rows if not r["asap"]])[:capacity]
    return out


def publish_waiting(conn, waiting, *, verdicts=None, client=None):
    """Merge only this board's acknowledged units; never replace a host queue."""
    from hermes_cli import kanban_priority_door as door
    if not door.enabled():
        return
    if client is None:
        from hermes_cli import alloc_client as client
    from hermes_cli.fleet_placement import placement
    table = placement.load_hosts()
    need = placement.need_of(table.footprint("kanban"))
    key = conn.execute("PRAGMA database_list").fetchone()[2]
    current = set(_published.get(key, set()))
    wanted = set()
    _published[key] = current
    for host, cards in waiting.items():
        hid = table.resolve(host)
        if not hid:
            continue
        # Empty convenience publication updates only the capacity fact; units
        # are managed explicitly above, never ranked or replaced by this client.
        client.publish_demand("kanban", hid, [], (verdicts or {}).get(host, {
            "admittable": False, "binding": "unknown", "deficit_cpu": 0.0}))
        for card in cards:
            uid = door.unit_id(conn, card["id"])
            client.enqueue("kanban", hid, uid, need.cpus, need.ram_gb)
            current.add((hid, uid))
            wanted.add((hid, uid))
            if card["asap"]:
                client.mark_asap("kanban", hid, uid)
    for hid, uid in list(current - wanted):
        client.withdraw("kanban", hid, uid)
        current.discard((hid, uid))


_published = {}


def verdicts_for_plan(plan):
    """Read the same per-host gate as take(); never infer CPU from a hot band."""
    import re
    import time
    from hermes_cli import kanban_headroom
    out = {}
    if plan is None or getattr(plan, "signal", None) is None:
        return out
    if plan.signal.placement_table is None:
        return out
    for host in plan.hosts:
        ok, reason = kanban_headroom._class_verdict(plan, host, None, now=time.time())
        # may_start returns admission.Verdict.reason, including its measured
        # fractional CPU shortfall. PSI and missing measurements have no deficit.
        match = re.search(r"saturated:([a-zA-Z0-9_]+)(?: ([+-]?[0-9.]+))?", reason)
        binding = match[1] if match else ("cpu" if ok else "unknown")
        free = {}
        if match and binding == "cpu" and match[2]:
            free["cpu"] = float(match[2])
        out[host] = door_verdict(ok, binding, free, plan.detail.get(host, {}).get("ncpu", 0))
    return out


def door_verdict(admittable: bool, binding: str, free, ncpu: float) -> dict:
    """Band pressure is not CPU usage; publish only measured headroom deficits."""
    cpu = free.get("cpu")
    measured = (isinstance(cpu, (int, float)) and not isinstance(cpu, bool)
                and math.isfinite(cpu) and isinstance(ncpu, (int, float))
                and not isinstance(ncpu, bool) and math.isfinite(ncpu) and ncpu > 0)
    return {"admittable": bool(admittable), "binding": binding,
            "deficit_cpu": max(0.0, -cpu * ncpu) if measured else 0.0}
