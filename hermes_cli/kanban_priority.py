"""Kanban's adapter to the fleet admission door; ordering belongs to admission."""
from __future__ import annotations

import logging
import math
import os

_log = logging.getLogger(__name__)


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
    A card waits on exactly ONE host: the door keeps unit ids unique per workload
    (hermes-home #9223), so a second host's enqueue is refused. Local-first: a
    local/portable card waits here unless its constraints exclude this host, then on
    the first eligible pool host in plan order; a pin/origin card on its host.
    """
    from hermes_cli.kanban_worker_pool import count_free

    out = {local_host: []}
    if plan is None:
        hosts = {}
    else:
        hosts = plan.hosts
    out.update({name: [] for name in hosts})
    own_only = cap_only or {}
    local_names = {local_host.lower()}
    try:
        from hermes_cli.fleet_placement import placement
        resolved = placement.load_hosts().resolve(local_host)
        if resolved:
            local_names.add(resolved.lower())
    except Exception:
        pass
    # Structural slot capacity per pool host, never the pressure band: the door decides admission.
    free = {} if plan is None else {
        name: max(0, count_free(host, int(plan.detail.get(name, {}).get("running", 0))) - plan.taken.get(name, 0))
        for name, host in hosts.items()}
    # Explicit urgent cards claim capacity first; otherwise the dispatcher's own row order.
    for card in [c for c in cards if c["asap"]] + [c for c in cards if not c["asap"]]:
        route, arg = routes.get(card["id"], ("local", None))
        if route == "wait":
            continue
        assignee = card["assignee"] or default_assignee
        if not assignee:
            continue
        only = None
        if route == "portable" and plan is not None and conn is not None:
            from hermes_cli.kanban_db_dispatch import _portable_only
            only, _ = _portable_only(conn, card, assignee, plan, caps,
                                     own_only.get(card["id"]),
                                     (worktree_ports or {}).get(card["id"]))
        allowed = None if only is None else {n.lower() for n in only}
        if (route == "local" or (route == "portable" and not own_only.get(card["id"])
                                 and (allowed is None or local_names & allowed))):
            out[local_host].append(card)
            continue
        if plan is None:
            continue
        for name, host in plan.hosts.items():
            if route in ("pin", "origin") and name != arg:
                continue
            if allowed is not None and name.lower() not in allowed:
                continue
            if (free[name] <= 0 or not host.enabled or host.state != "active" or name in plan.disabled
                    or assignee not in host.profiles or plan.board_refusal(name, board)
                    or plan.detail.get(name, {}).get("reachable") is False):
                continue
            out[name].append(card)
            free[name] -= 1
            break
    return out


def publish_waiting(conn, waiting, *, verdicts=None, client=None):
    """Enqueue/withdraw only this board's own units through the pair door; never replace a host queue.

    Withdrawals run first, so a card that moved host is gone from the old host before the new
    host's enqueue (the door refuses a live id on two hosts). Fault taxonomy (2026-11-05): a Refused
    (missing token, typed auth/invalid) latches the door (class 2); an Unreachable starts the transport
    backoff (class 3); either stops this publish. Returns {(host, unit_id): error} for what failed.
    """
    from hermes_cli import kanban_priority_door as door
    from hermes_cli import kanban_priority_faults as faults
    if not door.enabled() or faults.hold_reason():
        return {}                                    # latched (class 2) or backing off (class 3): no door call
    if client is None:
        client = door._client()
    try:
        errors = _publish_waiting(conn, waiting, verdicts, client, door)
    except _Stop as stop:
        errors = stop.errors
    for (hid, uid), why in errors.items():
        _log.warning("kanban door publish %s %s refused: %s", hid, uid, why)
    return errors


class _Stop(Exception):
    def __init__(self, errors):
        super().__init__("door publish stopped")
        self.errors = errors


def _classify(client, faults, exc, errors):
    """Class 2 (missing token, typed auth/invalid) latches and stops; class 3 (transport) backs off and stops."""
    if isinstance(exc, client.Unreachable):
        faults.note_transport(str(exc))
        raise _Stop(errors)
    if isinstance(exc, client.Refused):
        faults.latch(f"door publish refused: {exc}")
        raise _Stop(errors)


def _publish_waiting(conn, waiting, verdicts, client, door):
    from hermes_cli import kanban_priority_faults as faults
    from hermes_cli.fleet_placement import placement
    table = placement.load_hosts()
    cpus, ram_gb = door.need()
    key = conn.execute("PRAGMA database_list").fetchone()[2]
    current = _published.setdefault(key, set())
    plan = []
    for host, cards in waiting.items():
        hid = table.resolve(host)
        if hid:
            plan.append((host, hid, [(card, door.unit_id(conn, card["id"])) for card in cards]))
    wanted = {(hid, uid) for _, hid, rows in plan for _, uid in rows}
    errors = {}
    for hid, uid in sorted(current - wanted):
        try:
            client.withdraw("kanban", hid, uid)
            current.discard((hid, uid))
        except (client.Refused, client.Unreachable) as exc:
            errors[(hid, uid)] = str(exc)            # still ours; retried after the fault clears
            _classify(client, faults, exc, errors)
    for host, hid, rows in plan:
        # The capacity fact only (last writer wins); units are managed per id below.
        try:
            client.publish_demand("kanban", hid, [], (verdicts or {}).get(host, {
                "admittable": False, "binding": "unknown", "deficit_cpu": 0.0}))
        except (client.Refused, client.Unreachable) as exc:
            errors[(hid, None)] = str(exc)
            _classify(client, faults, exc, errors)
        for card, uid in rows:
            try:
                client.enqueue("kanban", hid, uid, cpus, ram_gb)
            except (client.Refused, client.Unreachable) as exc:
                errors[(hid, uid)] = str(exc)
                _classify(client, faults, exc, errors)
            current.add((hid, uid))
            door.mark_asap_if_set(client, conn, hid, uid, card["id"])
    faults.note_success()
    return errors


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
