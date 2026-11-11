"""The one ``placement:`` line ``kanban create`` prints: where the card will run, and which line decided it.

Card t_0a5740f3. Ace 2026-11-11 (Discord 1570197131329937549): kanban workers stay off the Mac Studio
unless truly necessary. Before this the author saw nothing at mint time, and the first sign of a Studio
placement was the placement event after the spawn.

The route is the dispatcher's own (:func:`kanban_db_dispatch._route_row`) for the card as it now stands,
computed from the pool files and the capability map. It does not know this tick's load, slots or probe
results, and it does not know links added later. So the line says where the card goes, not when. Binds are read
dry-run and no placement event is written. The one write a dropped bare ``host: studio`` pin can make is the
dispatcher's own once-per-card notice (``kanban_studio_pin.note_drop``), which the first tick would write anyway.
Never raises: on any error there is no line.
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Optional

_log = logging.getLogger(__name__)

_ROW_SQL = ("SELECT id, assignee, body, skills, workspace_kind, workspace_path, no_worker, priority, "
            "origin_host, workspace_of, branch_name, asap FROM tasks WHERE id = ?")
_HOST_LINE_RE = re.compile(r"^[ \t]*host:[^\n]*$", re.IGNORECASE | re.MULTILINE)
STUDIO = "the Mac Studio"


def _pool_config():
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_worker_pool as kwp
    return kwp.read_pool(kb.kanban_home() / "fleet", kanban_cfg=kbd._root_kanban_cfg())


def _requires_line(body: str, cap: Optional[str]) -> Optional[str]:
    from hermes_cli import kanban_host_capabilities as khc
    for m in khc.REQUIRES_RE.finditer(body or ""):
        if cap is None or cap in khc.declared_words(m.group(0)):
            return m.group(0).strip()
    return None


def _host_line(body: str) -> Optional[str]:
    m = _HOST_LINE_RE.search(body or "")
    return m.group(0).strip() if m else None


def _quote(line: Optional[str], fallback: str) -> str:
    return f"`{line}`" if line else fallback


def _decided_by(body: str, rule: Optional[str], studio: Optional[dict]) -> str:
    """The line (quoted) or the rule that decided a Studio route."""
    rule = rule or ""
    rec = studio or {}
    if rec.get("source") == "declared" or rule.startswith("requires:"):
        cap = rec.get("capability") or rule[len("requires:"):] or None
        line = _requires_line(body, cap)
        if line:
            return _quote(line, "")
    if rule == "host_studio":
        return " + ".join(f"`{x}`" for x in (_host_line(body), _requires_line(body, None)) if x) or "rule host_studio"
    if rec.get("source") == "skill":
        return f"skill {rec.get('evidence')} (needs {rec.get('capability')})"
    if rec.get("kind") == "structural":
        return f"rule {rec.get('capability')} ({rec.get('source')}: {rec.get('evidence')})"
    return f"rule {rule or 'local'}"


def preview_line(conn, task_id: str) -> Optional[str]:
    """``will run on <where>, decided by <line or rule>`` for one card, or None."""
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_dispatch as kbd
        row = conn.execute(_ROW_SQL, (task_id,)).fetchone()
        if row is None:
            return None
        cfg = _pool_config()
        if cfg is None or not tuple(getattr(cfg, "hosts", ()) or ()):
            why = getattr(cfg, "refused", None) or "no worker pool configured"
            return f"will run on {STUDIO}, decided by: {why}"
        plan = SimpleNamespace(config=cfg, continuation_states={}, continuation_dry_run=True)
        caps = kbd._capability_tick(cfg)
        last_resort = kbd._studio_last_resort(kb.kanban_home() / "fleet")
        studio_on = kbd._studio_capability_on(caps, last_resort)
        rules: dict = {}
        studio: dict = {}
        route, arg = kbd._route_row(
            conn, row, plan, None, {}, None, None, {}, unknown_out={}, caps=caps, caps_out={},
            rules_out=rules, access_out={}, binds=kbd._workspace_binds(conn, [row], dry_run=True),
            worktree_out={}, carry_out={}, pause_cache={}, studio_on=studio_on, studio_out=studio)
        body = row["body"] or ""
        rule = rules.get(task_id)
        if route == "portable":
            where = "a pool host" if last_resort else f"a pool host or {STUDIO} (whichever has room)"
            return f"will run on {where}, decided by: no line pins it"
        if route in ("pin", "origin"):
            if rule and rule.startswith("bind_host:"):
                return f"will run on {arg}, decided by: rule {rule}"
            return f"will run on {arg}, decided by: {_quote(_host_line(body), 'rule ' + str(rule))}"
        if route == "local":
            return f"will run on {STUDIO}, decided by: {_decided_by(body, rule or arg, studio.get(task_id))}"
        return f"will wait ({arg}), decided by: rule {rule or arg}"
    except Exception:  # noqa: BLE001 - a preview must never fail a create
        _log.debug("kanban create: placement preview for %s failed", task_id, exc_info=True)
        return None
