"""Mac Studio default-deny: a kanban worker starts on the Studio only for an ALLOWLISTED capability (t_062a5948).

Ace 2026-11-11 (Discord 1570218756024569917, scope addition 1570222371485519942): kanban workers stay off the Mac
Studio, which is the orchestration machine. This module adds one rule on top of t_9965b8d5
(:mod:`hermes_cli.kanban_studio_capability`). That rule already makes a ``local`` route name the Studio-only capability
that holds it (``justify``). Here, while the switch is on, that named capability must also be

* on the allowlist ``kanban.placement.studio_default_deny.allow`` (the capabilities the operator has accepted as
  truly Studio-only), or
* covered by a live operator override for this card (``hermes kanban studio-override``),

else the card WAITS: ``studio_default_deny:<capability> (<source>: <evidence>; no pool host has it)``. It is never
dropped and never spawned on the Studio by itself. A portable card already never falls back to the Studio
(t_9965b8d5), so a card held here is one that no pool host can take.

Matching: an allowlist entry matches the capability, or the head before ``:`` (``workspace_kind`` covers
``workspace_kind:origin_not_github``). A ``studio_needs`` capability such as ``control-plane,obsidian-vault`` is
allowed only if EVERY part is allowed.

Exempt: operator-only cards, meaning the assignee is an operator profile (:data:`kanban_db.OPERATOR_PROFILES`) or
the card is ``no_worker``.

Override (``hermes kanban studio-override <card> --capability <cap> --why <reason> [--once | --until <ISO>]``):
operator token only, ``--capability`` required. One ``studio_override`` board event is written. ``--once`` (the
default) is used up by the card's next ``spawned`` event. ``--until`` holds until that time. The override covers a held
capability only when it names one of its parts. Every override also writes one line to the capability-gap ledger
``<root>/state/studio-capability-gap/overrides.jsonl`` (t_b3884ae0's state dir).

Holding: the first tick that holds a card in an episode writes ONE ``mechanical_wake`` event (source
``studio-default-deny``). The gateway owner-wake turns that into a silent turn in the card's home session, not a chat
post. A hold over 60 minutes pages #alerts once per episode through the root's episode pager (producer
``studio-default-deny``, key ``held:<card>``, latched by a ``studio_default_deny_paged`` event). The structural-hold
report (kanban_placement_starvation) also names the fix on the card.

Switch: ``kanban.placement.studio_default_deny.enabled`` in the ROOT config.yaml, read every tick, default false. The
rule is also inert whenever t_9965b8d5's rule is off (its kill switch, Studio not last resort, map unreadable).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

KNOB = "studio_default_deny"
WAIT_PREFIX = "studio_default_deny"
OVERRIDE_EVENT = "studio_override"
HOLD_WAKE_SOURCE = "studio-default-deny"
WAKE_EVENT = "mechanical_wake"
PAGED_EVENT = "studio_default_deny_paged"
PAGER_PRODUCER = "studio-default-deny"
PAGE_AFTER_S = 60 * 60
LEDGER_DIR = ("state", "studio-capability-gap")
LEDGER_FILE = "overrides.jsonl"


def config(kcfg: Optional[Mapping]) -> tuple:
    """``(enabled, allow)`` from the ROOT ``kanban`` section. Default ``(False, frozenset())``."""
    placement = (kcfg or {}).get("placement") if isinstance(kcfg, Mapping) else None
    sect = placement.get(KNOB) if isinstance(placement, Mapping) else None
    if not isinstance(sect, Mapping):
        return False, frozenset()
    val = sect.get("enabled", False)
    if isinstance(val, str):
        on = val.strip().lower() in ("on", "true", "yes", "1")
    else:
        on = bool(val)
    allow = sect.get("allow") or ()
    if isinstance(allow, str):
        allow = [a for a in allow.replace(",", " ").split()]
    words = frozenset(str(a).strip().lower() for a in allow if str(a).strip())
    return on, words


def parts(capability: Optional[str]) -> tuple:
    return tuple(p.strip().lower() for p in str(capability or "").split(",") if p.strip())


def _covered(part: str, words) -> bool:
    return part in words or part.split(":", 1)[0] in words


def allowed(capability: Optional[str], allow) -> bool:
    """True when every part of ``capability`` is on ``allow``. An unnamed capability is never allowed."""
    ps = parts(capability)
    return bool(ps) and all(_covered(p, allow) for p in ps)


def operator_only(row) -> bool:
    from hermes_cli import kanban_db as _kb
    keys = row.keys() if hasattr(row, "keys") else ()
    if "no_worker" in keys and row["no_worker"]:
        return True
    return str(row["assignee"] or "").strip().lower() in _kb.OPERATOR_PROFILES


def live_override(conn, task_id: str, capability: Optional[str], *, now: Optional[float] = None) -> Optional[dict]:
    """The newest ``studio_override`` on the card that still holds and names a part of ``capability``, else None.
    ``once``: no ``spawned`` event after it. ``until``: ``now`` before it."""
    want = set(parts(capability))
    if not want:
        return None
    now = time.time() if now is None else now
    for ev_id, raw in conn.execute(
            "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC",
            (task_id, OVERRIDE_EVENT)):
        try:
            p = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(p, dict) or str(p.get("capability") or "").strip().lower() not in want:
            continue
        until = p.get("until")
        if isinstance(until, (int, float)):
            if now < until:
                return p
            continue
        used = conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'spawned' AND id > ? LIMIT 1",
                            (task_id, ev_id)).fetchone()
        if used is None:
            return p
    return None


def wait_reason(why: Mapping) -> str:
    cap = why.get("capability") or "unnamed"
    src, ev = why.get("source") or "?", why.get("evidence") or ""
    return f"{WAIT_PREFIX}:{cap} ({src}: {ev}; no pool host has it; not on the Studio allowlist)"


def decide(conn, row, why: Optional[Mapping], *, enabled: bool, allow, now: Optional[float] = None):
    """``None`` = the Studio route stands (rule off, exempt, allowlisted, or overridden); else the wait reason.
    ``why`` is what ``kanban_studio_capability.justify`` named (never None here: an unnamed route already waits)."""
    if not enabled or why is None or operator_only(row):
        return None
    cap = why.get("capability")
    if allowed(cap, allow):
        return None
    if live_override(conn, row["id"], cap, now=now) is not None:
        return None
    return wait_reason(why)


def _held_this_episode(conn, task_id: str) -> bool:
    """True when the current wait episode already woke the operator: a ``mechanical_wake`` from this source with no
    episode-ending event after it."""
    from hermes_cli import kanban_placement_reasons as _pr
    ends = sorted(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS)
    for kind, raw in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN (?, %s) ORDER BY id DESC"
            % ",".join("?" * len(ends)), (task_id, WAKE_EVENT, *ends)):
        if kind != WAKE_EVENT:
            return False
        try:
            if (json.loads(raw or "{}") or {}).get("source") == HOLD_WAKE_SOURCE:
                return True
        except (TypeError, ValueError, AttributeError):
            continue
    return False


def wake_text(task_id: str, reason: str) -> str:
    return (f"{task_id} is held off the Mac Studio by default-deny: {reason}. No pool host can take it. "
            f"If it truly needs the Studio, run `hermes kanban studio-override {task_id} --capability <cap> "
            f"--why \"<reason>\"` (one run), or add the capability to kanban.placement.studio_default_deny.allow. "
            f"Otherwise fix the gap (a pool host that advertises it, or a card that does not need it).")


def _episode_start(conn, task_id: str) -> Optional[int]:
    """When the current hold episode began: the oldest ``mechanical_wake`` from this source after the last
    episode-ending event, or None."""
    from hermes_cli import kanban_placement_reasons as _pr
    ends = sorted(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS)
    start = None
    for kind, created, raw in conn.execute(
            "SELECT kind, created_at, payload FROM task_events WHERE task_id = ? AND kind IN (?, ?, %s) "
            "ORDER BY id DESC" % ",".join("?" * len(ends)), (task_id, WAKE_EVENT, PAGED_EVENT, *ends)):
        if kind not in (WAKE_EVENT, PAGED_EVENT):
            break
        if kind == PAGED_EVENT:
            return None  # this episode already paged
        try:
            if (json.loads(raw or "{}") or {}).get("source") == HOLD_WAKE_SOURCE:
                start = int(created)
        except (TypeError, ValueError, AttributeError):
            continue
    return start


def _root_pager():
    """An ``EpisodePager`` from the running root's ``scripts/lib/episode_pager.py``, or None (hermetic home)."""
    import importlib.util
    import sys
    from hermes_cli import kanban_db as _kb
    path = _kb.kanban_home() / "scripts" / "lib" / "episode_pager.py"
    if not path.is_file():
        return None
    name = "_hermes_episode_pager"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod.EpisodePager(PAGER_PRODUCER)


def notify_holds(conn, waits: Mapping[str, str], *, now: Optional[int] = None, pager_factory=None) -> list:
    """Per held card: ONE ``mechanical_wake`` per hold episode (silent turn in the card's home session), and ONE
    #alerts page once the episode is :data:`PAGE_AFTER_S` old (latched by a ``studio_default_deny_paged`` event).
    Returns the ids woken this call."""
    from hermes_cli import kanban_db as _kb
    now = int(time.time()) if now is None else int(now)
    factory = _root_pager if pager_factory is None else pager_factory
    pager = None
    woke = []
    for tid, reason in (waits or {}).items():
        if not str(reason or "").startswith(WAIT_PREFIX + ":"):
            continue
        try:
            if not _held_this_episode(conn, tid):
                with _kb.write_txn(conn):
                    _kb._append_event(conn, tid, WAKE_EVENT,
                                      {"source": HOLD_WAKE_SOURCE, "text": wake_text(tid, reason)})
                woke.append(tid)
                continue
            start = _episode_start(conn, tid)
            if start is None or now - start < PAGE_AFTER_S:
                continue
            if pager is None:
                pager = factory()
            if pager is None:
                continue
            text = (f"🔒 Studio default-deny: {tid} held {(now - start) // 60} min, no pool host can take it. "
                    f"{reason}. Decide: `hermes kanban studio-override {tid} --capability <cap> --why \"...\"`, "
                    f"allowlist the capability, or fix the gap.")
            outcome = pager.page(f"held:{tid}", text)
            if outcome != "failed":
                with _kb.write_txn(conn):
                    _kb._append_event(conn, tid, PAGED_EVENT, {"waited": now - start, "outcome": outcome})
        except Exception as exc:  # one bad card never stops the tick
            _kb._log.warning("kanban studio default-deny wake/page on %s failed: %s", tid, exc)
    return woke


# -- the operator verb -------------------------------------------------------------------------------------------

class OverrideRefused(ValueError):
    pass


def _parse_until(value: str, now: float) -> int:
    from hermes_cli import kanban_db as _kb
    try:
        ts = _kb.parse_wake_at(value, now=int(now))
    except Exception as exc:
        raise OverrideRefused(f"--until {value!r} is not a time ({exc})") from exc
    if ts <= now:
        raise OverrideRefused(f"--until {value!r} is in the past")
    return int(ts)


def ledger_path(root: Optional[Path] = None) -> Path:
    if root is None:
        from hermes_cli import kanban_db as _kb
        root = _kb.kanban_home()
    return Path(root).joinpath(*LEDGER_DIR, LEDGER_FILE)


def _fix_card(conn, capability: str) -> Optional[str]:
    """An open card whose idempotency key is ``studio-capgap:<cap>`` (t_b3884ae0's key), or None."""
    row = conn.execute("SELECT id FROM tasks WHERE idempotency_key = ? AND status NOT IN ('done', 'archived') "
                       "ORDER BY created_at DESC LIMIT 1", (f"studio-capgap:{capability}",)).fetchone()
    return row[0] if row else None


def override(conn, task_id: str, capability: str, why: str, *, once: bool = True, until: Optional[str] = None,
             actor: Optional[str] = None, now: Optional[float] = None, root: Optional[Path] = None) -> dict:
    """Record one override. Raises :class:`OverrideRefused` with the line to print."""
    from hermes_cli import kanban_db as _kb
    now = time.time() if now is None else now
    state = _kb._operator_token_state()
    if state != "ok":
        raise OverrideRefused(f"studio-override is operator-only: {_kb.OPERATOR_TOKEN_ENV} {state.replace('_', ' ')}")
    cap = (capability or "").strip().lower()
    if not cap or "," in cap:
        raise OverrideRefused("--capability is required: name ONE capability (the one the wait reason names)")
    if len((why or "").split()) < 3:
        raise OverrideRefused("--why is required: at least 3 words saying why this card needs the Studio")
    row = conn.execute("SELECT id, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise OverrideRefused(f"unknown card {task_id}")
    until_ts = _parse_until(until, now) if until else None
    payload = {"capability": cap, "why": why.strip(), "by": actor or "operator", "at": int(now),
               "once": until_ts is None, "until": until_ts, "fix_card": _fix_card(conn, cap)}
    with _kb.write_txn(conn):
        _kb._append_event(conn, task_id, OVERRIDE_EVENT, payload)
    line = {"kind": "override", "task_id": task_id, **payload}
    path = ledger_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
        payload["ledger"] = str(path)
    except OSError as exc:  # the board event is the record; say the ledger write failed
        payload["ledger_error"] = str(exc)
    last = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'placement_wait' "
                        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    held = None
    if last:
        try:
            reason = (json.loads(last[0] or "{}") or {}).get("reason") or ""
        except (TypeError, ValueError, AttributeError):
            reason = ""
        if reason.startswith(WAIT_PREFIX + ":"):
            held = reason[len(WAIT_PREFIX) + 1:].split(" (", 1)[0]
    payload["held_capability"] = held
    return payload


def cmd_studio_override(args) -> int:
    """``hermes kanban studio-override``: one line on success (rc 0), one REFUSED line otherwise (rc 1)."""
    import sys
    from hermes_cli import kanban_db as _kb
    actor = (os.environ.get("HERMES_PROFILE") or "").strip() or "operator"
    try:
        with _kb.connect_closing(board=getattr(args, "board", None)) as conn:
            p = override(conn, args.task_id, args.capability, args.why, until=getattr(args, "until", None),
                         actor=actor)
    except OverrideRefused as exc:
        print(f"kanban studio-override: REFUSED {exc}", file=sys.stderr)
        return 1
    scope = "once (next run only)" if p["once"] else f"until {p['until']}"
    print(f"kanban studio-override: {args.task_id} may start on the Studio for {p['capability']} {scope}; "
          f"fix card: {p['fix_card'] or 'none'}; event={OVERRIDE_EVENT}; ledger={p.get('ledger') or 'NOT WRITTEN'}")
    if p.get("held_capability") and p["capability"] not in parts(p["held_capability"]):
        print(f"kanban studio-override: WARNING {args.task_id} is held for {p['held_capability']}, not "
              f"{p['capability']}: this override does not release it", file=sys.stderr)
    return 0
