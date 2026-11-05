"""Kanban's adapter to the fleet admission door; ordering belongs to admission."""
from __future__ import annotations

import math
import os


def set_asap(conn, task_id: str, flag: bool, *, operator: str) -> bool:
    """Only an explicit operator edit may mark a card urgent; creation never copies it."""
    from hermes_cli import kanban_db as kb
    if os.environ.get("HERMES_KANBAN_TASK") or kb._is_delegated_child():
        raise PermissionError("ASAP is operator-set only")
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


def waiting_by_host(cards, plan, *, routes, local_host: str, board=None, default_assignee=None):
    """Use the dispatcher's resolved routes, never its pressure-filtered slot budget.

    No age or workload ordering is decided here. The common publisher selects its
    urgency-first visible window; retain explicit urgent cards before the slot cap.
    """
    from hermes_cli.kanban_worker_pool import count_free

    out = {local_host: []}
    out.update({name: [] for name in plan.hosts} if plan is not None else {})
    for card in cards:
        route, arg = routes.get(card["id"], ("local", None))
        if route == "wait":
            continue
        assignee = card["assignee"] or default_assignee
        if route in ("local", "portable"):
            out[local_host].append(card)
        if plan is None or route == "local":
            continue
        for name, host in plan.hosts.items():
            if route in ("pin", "origin") and name != arg:
                continue
            if (not host.enabled or host.state != "active" or name in plan.disabled
                    or assignee not in host.profiles or plan.board_refusal(name, board)):
                continue
            out[name].append(card)
    if plan is not None:
        for name, host in plan.hosts.items():
            capacity = count_free(host, int(plan.detail.get(name, {}).get("running", 0)))
            capacity = max(0, capacity - plan.taken.get(name, 0))
            rows = out[name]
            out[name] = ([r for r in rows if r["asap"]]
                         + [r for r in rows if not r["asap"]])[:capacity]
    return out


def door_verdict(admittable: bool, binding: str, free, ncpu: float) -> dict:
    """PSI is not CPU usage: only measured negative CPU headroom is a core deficit."""
    cpu = free.get("cpu")
    measured = (isinstance(cpu, (int, float)) and not isinstance(cpu, bool)
                and math.isfinite(cpu) and isinstance(ncpu, (int, float))
                and not isinstance(ncpu, bool) and math.isfinite(ncpu) and ncpu > 0)
    return {"admittable": bool(admittable), "binding": binding,
            "deficit_cpu": max(0.0, -cpu * ncpu) if measured else 0.0}
