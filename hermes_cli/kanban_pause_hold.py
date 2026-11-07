"""Fleet pause hold: a card of a paused host spawns nowhere (fleet pause/resume SPEC v0.3 §5.8, t_133d6efd).

The manifest ``<kanban_home()>/state/fleet-pause/<host>/manifest.json`` is written by ``scripts/fleet-pause``
(hermes-home). ONE predicate, :func:`decide`, evaluated in two layers:

* layer 1, :func:`gate_rows`: a post-route pass in ``_dispatch_once_locked`` after every route is known and
  before any spawn (ready lane before ``_browser_gate``, review lane after ``_browser_hold_review_rows``), so the
  plan branch, the no-plan branch and every way a row became ready pass through it;
* layer 2, :func:`refuse_if_held`: the first statement of the two run openers ``_claim_and_open_run`` and
  ``_open_review_run``, the only functions that turn a ready/review row into a running row + ``task_runs`` row.
  A held card raises :class:`PauseHeld` before the CAS; the caller's ``write_txn`` rolls back.

Path root is ``kanban_home()``, never a profile ``HERMES_HOME``. A missing manifest = no pause; an unreadable one
holds every card of that host (fail closed). Kill switch ``kanban.placement.pause_hold: on|off`` (default on),
read by both layers through :func:`enabled`.

Stale-hold fail-safes (t_0c5a95ab, Ace 2026-11-06 20:31 "the CI box is not paused anymore ... some kind of fail safe"):

* a hold-phase manifest past ``lease_until + LEASE_GRACE_S`` whose ``driver.pid`` is not alive is treated as
  resumed: :func:`decide` holds nothing and #alerts gets ONE "stale pause manifest auto-cleared" page per manifest
  id (latch ``<host>/stale-cleared.<id>.json``);
* a resume pin is owed only while the unit still awaits its first run (``resume.new_run_id`` unset and
  ``resume.action`` not a settled one); a pin that conflicts with the card's own route waits at most
  ``PIN_CONFLICT_GRACE_S`` after ``resumed_at`` and then routes ordinarily, so a resumed host never holds;
* :func:`clear_stale_labels` ends, on every live tick, the open ``placement_wait pause_hold`` of a card the current
  manifest no longer holds (``{cleared: true}``), so no reader sees a stale label for a card the dispatcher skips
  (no_worker / human lane cards are never routed, so their last label never moved);
* :func:`holds` is the read model behind ``hermes kanban placement-holds``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

CODE = "pause_hold"
REFUSED_EVENT = "claim_refused_pause_hold"
UNREADABLE = "unreadable"
HOLD_PHASES = frozenset({"draining", "paused", "paused-forced", "paused-partial"})
PIN_PHASES = frozenset({"resuming", "resumed"})
STATUS_FILE = "hold-status.json"
_UNREADABLE = object()
#: a hold-phase manifest stays a hold this long past ``lease_until`` (= hermes-home host_window.GRACE_S).
LEASE_GRACE_S = 900
#: a resume pin that conflicts with the card's own route waits this long after ``resumed_at``, then lets go.
PIN_CONFLICT_GRACE_S = 900
#: ``unit.resume.action`` values under which no post-resume run is owed (fleet-pause stamps them).
SETTLED_ACTIONS = frozenset({"no_run_owed", "already_moved", "none"})
STALE_EVENT_REASON = "stale pause manifest auto-cleared"
_warned_stale: set = set()


class PauseHeld(Exception):
    """A run opener refused a card whose origin host is paused."""

    def __init__(self, host: str, manifest_id: str, task_id: str = ""):
        super().__init__(f"held by fleet-pause {manifest_id} (host {host})")
        self.host, self.manifest_id, self.task_id = host, manifest_id, task_id

    @property
    def reason(self) -> str:
        return f"{CODE} ({self.host}:{self.manifest_id} stage=claim)"


def root() -> Path:
    from hermes_cli import kanban_db as _kb
    return _kb.kanban_home() / "state" / "fleet-pause"


def enabled() -> bool:
    """``kanban.placement.pause_hold`` from the ROOT config; anything but an explicit off is on."""
    try:
        from hermes_cli import kanban_db as _kb
        placement = (_kb._board_home_kanban_cfg() or {}).get("placement") or {}
        value = placement.get("pause_hold", "on") if isinstance(placement, dict) else "on"
    except Exception:
        _log.warning("kanban pause_hold: root config unreadable; hold stays on", exc_info=True)
        return True
    return value is not False and str(value).strip().lower() not in ("off", "false", "0", "no")


def _valid(doc) -> bool:
    """Every field :func:`decide` dereferences has the shape it expects (Prism aceb690c4071)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("phase"), str) or not doc.get("host"):
        return False
    units = doc.get("units")
    if units is None:
        return True
    if not isinstance(units, list):
        return False
    for unit in units:
        if not isinstance(unit, dict):
            return False
        resume = unit.get("resume")
        if resume is not None and not isinstance(resume, dict):
            return False
    return True


def _load(host: str, cache: dict):
    if host in cache:
        return cache[host]
    path = root() / host / "manifest.json"
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        cache[host] = None
        return None
    except (OSError, ValueError):   # ValueError: UnicodeDecodeError on non-UTF-8 bytes
        _log.warning("kanban pause_hold: %s unreadable; every %s card is HELD", path, host)
        cache[host] = _UNREADABLE
        return _UNREADABLE
    try:
        doc = json.loads(raw)
        if not _valid(doc):
            raise ValueError("manifest shape")
    except (ValueError, RecursionError):
        _log.warning("kanban pause_hold: %s unreadable; every %s card is HELD", path, host)
        doc = _UNREADABLE
    cache[host] = doc
    return doc


def decide(origin_host: Optional[str], task_id: str, cache: dict):
    """None | ("wait", "pause_hold (<host>:<id>)") | ("pin", host). Pure over the manifest contents.

    Never raises: any unexpected failure holds the host's card (fail closed), so neither layer can take a
    dispatch tick or a claim down with something other than :class:`PauseHeld`."""
    try:
        return _decide(origin_host, task_id, cache)
    except Exception:
        _log.warning("kanban pause_hold: decide(%s) failed; card %s HELD", origin_host, task_id, exc_info=True)
        if origin_host:
            cache[str(origin_host)] = _UNREADABLE
        return ("wait", f"{CODE} ({origin_host}:{UNREADABLE})")


def _decide(origin_host, task_id, cache):
    if not origin_host:
        return None
    doc = _load(str(origin_host), cache)
    if doc is None:
        return None
    if doc is _UNREADABLE:
        return ("wait", f"{CODE} ({origin_host}:{UNREADABLE})")
    phase = doc.get("phase")
    if phase in HOLD_PHASES:
        why = stale_reason(doc)
        if why:
            _note_stale(str(origin_host), doc, why)
            return None
        return ("wait", f"{CODE} ({origin_host}:{doc.get('id') or '?'})")
    if phase in PIN_PHASES:
        for unit in doc.get("units") or ():
            if unit.get("lane") == "kanban" and unit.get("id") == task_id:
                return ("pin", str(origin_host)) if owes_run(unit) else None
    return None


def owes_run(unit: dict) -> bool:
    """A resumed kanban unit still owed its first post-resume run (the resume pin applies)."""
    resume = unit.get("resume") or {}
    return resume.get("new_run_id") is None and resume.get("action") not in SETTLED_ACTIONS


def _driver_alive(doc: dict) -> bool:
    """The manifest's ``driver.pid`` exists on THIS host. No pid / a non-int = dead. EPERM = alive."""
    pid = (doc.get("driver") or {}).get("pid") if isinstance(doc.get("driver"), dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def stale_reason(doc: dict, now: Optional[float] = None) -> Optional[str]:
    """Why a HOLD-phase manifest no longer holds, or None while it does: its lease (+ LEASE_GRACE_S) ran out and no
    driver process is alive. A manifest without a numeric ``lease_until`` never goes stale here (fail closed)."""
    t = time.time() if now is None else float(now)
    lease = doc.get("lease_until")
    if not isinstance(lease, (int, float)) or isinstance(lease, bool):
        return None
    if t < float(lease) + LEASE_GRACE_S or _driver_alive(doc):
        return None
    pid = (doc.get("driver") or {}).get("pid") if isinstance(doc.get("driver"), dict) else None
    return (f"phase {doc.get('phase')} lease expired {int((t - float(lease)) // 60)} min ago and the driver "
            f"(pid {pid}) is gone")


def _pager():
    """``(argv_prefix)`` sender for the stale page, or None in a hermetic home (no notify.py: never page)."""
    from hermes_cli import kanban_budget as _kbudget
    script = _kbudget._notify_script_path()
    if script is None:
        return None
    import sys
    return lambda body: _kbudget._run_notify([sys.executable, script, "--sev", "error", "--send", body]) is not False


def _note_stale(host: str, doc: dict, why: str) -> None:
    """ONE #alerts page per stale manifest id, latched on disk only after the send went out. Never raises."""
    mid = str(doc.get("id") or "?")
    try:
        latch = root() / host / f"stale-cleared.{mid}.json"
        if latch.exists():
            return
        if mid not in _warned_stale:
            _warned_stale.add(mid)
            _log.warning("kanban pause_hold: %s %s treated as resumed: %s", host, mid, why)
        send = _pager()
        if send is None:
            return
        body = (f"🧹 {STALE_EVENT_REASON}: fleet-pause {host} {mid} ({why}). The dispatcher no longer holds {host} "
                f"cards on it. Resume or abandon it properly: `fleet-pause status {host}`, then "
                f"`fleet-pause resume {host}` / `fleet-pause abandon {host} --reason ...`.")
        if send(body):
            latch.write_text(json.dumps({"id": mid, "host": host, "why": why, "at": int(time.time())}),
                             encoding="utf-8")
    except Exception:
        _log.warning("kanban pause_hold: stale page for %s %s failed", host, mid, exc_info=True)


def _row(r, key):
    try:
        return r[key]
    except (IndexError, KeyError):
        return None


def held_routes(rows, cache: dict) -> dict:
    """Ready-lane PRE-ROUTE pass (Prism 76b7233abe29): ``{task_id: ("wait", reason)}`` for every row the hold
    keeps. The dispatcher routes only the rows NOT in this dict, so ``origin_route`` (which can persist
    ``origin_host=NULL`` under continuation=enforce) never sees a held card. Empty when the switch is off."""
    if not rows or not enabled():
        return {}
    out = {}
    for r in rows:
        verdict = decide(_row(r, "origin_host"), r["id"], cache)
        if verdict is not None and verdict[0] == "wait":
            out[r["id"]] = verdict
    return out


def _pin_conflict(r, current, host: str, conn=None, pool=None, caps=None) -> Optional[str]:
    """Why a resume pin to ``host`` must WAIT instead of being applied, or None:

    * an explicit ``host: studio`` pin, or a ``local`` route carrying a rule (access-bound, studio affinity,
      unknown pin, not_portable) (Prism 536374336fb5); ``("local", None)`` and ``portable`` may be pinned;
    * a ``pin``/``origin`` route to ANOTHER host (Prism c3421355edd2): the resume fence never runs elsewhere;
    * the origin's hard checks (registry, assignee, hard capabilities, Studio-only access, split brain), the
      same ones ``origin_route`` applies, but without its probe arm, so an empty probe never clears the origin
      of a held/resuming card (Prism 904f143f5a38). Needs a pool plan; the caller waits planless anyway."""
    from hermes_cli import kanban_worker_pool as _kwp
    body = _row(r, "body")
    if body:
        pin, _ignored = _kwp.resolve_pin(body, ())
        if pin == _kwp.PIN_STUDIO:
            return "host_studio"
    if current and current[0] == "local" and current[1]:
        return str(current[1])
    if current and current[0] in ("pin", "origin") and current[1] and str(current[1]).lower() != host.lower():
        return f"routed_to:{current[1]}"
    if conn is not None and pool is not None:
        from hermes_cli import kanban_continuation as _kcont
        why = _kcont.origin_hard_refusal(conn, r, pool, host, caps)
        if why:
            return f"origin_refused:{why}"
    return None


def _pin_conflict_expired(host: str, cache: dict, now: Optional[float] = None) -> bool:
    """A ``resumed`` manifest whose ``resumed_at`` is more than PIN_CONFLICT_GRACE_S ago. ``resuming`` never
    expires here: the resume driver is still running and the pin is its fence."""
    doc = cache.get(str(host))
    if not isinstance(doc, dict) or doc.get("phase") != "resumed":
        return False
    at = doc.get("resumed_at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    t = time.time() if now is None else float(now)
    return t - float(at) >= PIN_CONFLICT_GRACE_S


def _write_status(mode: str, now: float) -> None:
    from hermes_cli import kanban_db_dispatch as _kbd
    try:
        interval = float((_kbd._root_kanban_cfg() or {}).get("dispatch_interval_seconds", 60) or 60)
    except (TypeError, ValueError):
        interval = 60.0
    path = root() / STATUS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{STATUS_FILE}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"mode": mode, "pid": os.getpid(), "updated_at": int(now),
                                   "interval_s": interval}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        _log.warning("kanban pause_hold: could not write %s", path, exc_info=True)


def gate_rows(conn, rows, result, row_route: dict, dry_run: bool, cache: dict, *, lane: str,
              planned: bool = True, entry: str = "gateway", pins_out: Optional[dict] = None,
              pool=None, caps=None):
    """Layer 1. Ready lane: writes ``row_route`` and returns ``rows``. Review lane: records the wait in
    ``result.placement_waits``, writes a resume pin into ``pins_out`` (Prism 01f1e61334ff) and returns the rows
    that may proceed.

    ``hold-status.json`` is published on the ready lane only by a live GATEWAY tick (RC-P3-2: a one-shot
    ``hermes kanban dispatch`` runs the same code from disk before the gateway restarts and must not vouch for it).
    """
    on = enabled()
    if lane == "ready" and not dry_run and entry == "gateway":
        _write_status("on" if on else "off", time.time())
    if not on or not rows:
        return rows
    kept = []
    for r in rows:
        verdict = decide(_row(r, "origin_host"), r["id"], cache)
        if verdict is None:
            kept.append(r)
            continue
        if verdict[0] == "wait":
            if lane == "review":
                result.placement_waits[r["id"]] = verdict[1]
                continue
            row_route[r["id"]] = verdict
            kept.append(r)
            continue
        current = row_route.get(r["id"]) if lane == "ready" else None
        conflict = _pin_conflict(r, current, verdict[1], conn, pool, caps)
        if conflict is not None and _pin_conflict_expired(verdict[1], cache):
            # The pin can never be honoured (the card's own route forbids the origin) and the card can never earn
            # the first run that would retire the pin: past the grace it routes ordinarily (t_0c5a95ab).
            kept.append(r)
            continue
        if conflict is not None:
            why = f"{CODE} ({verdict[1]}:resume_pin_conflict:{conflict})"
            if lane == "review":
                result.placement_waits[r["id"]] = why
                continue
            row_route[r["id"]] = ("wait", why)
            kept.append(r)
            continue
        if lane == "review":
            if not planned:
                result.placement_waits[r["id"]] = "pool_unavailable"
                continue
            if pins_out is not None:
                pins_out[r["id"]] = verdict[1]
            kept.append(r)
            continue
        kept.append(r)
        if current and current[0] in ("pin", "origin", "wait"):
            continue  # same destination (a conflict waited above), or another gate's wait: leave it
        # RC-P3-1: with no pool plan a pin route reaches spillover.take(None); wait like a body pin does (I-11).
        row_route[r["id"]] = verdict if planned else ("wait", "pool_unavailable")
    return kept if lane == "review" else rows


def _open_pause_labels(conn) -> list:
    """``[(task_id, origin_host, detail)]``: ready/review cards whose LATEST placement_wait is an open (not cleared,
    no episode end after it) ``pause_hold``."""
    from hermes_cli import kanban_placement_reasons as _pr
    end = sorted(_pr.PLACEMENT_WAIT_EPISODE_END_KINDS)
    rows = conn.execute(
        "SELECT t.id, t.origin_host, e.payload FROM tasks t "
        "JOIN task_events e ON e.id = (SELECT MAX(id) FROM task_events WHERE task_id = t.id AND kind = ?) "
        "WHERE t.status IN ('ready', 'review') AND NOT EXISTS (SELECT 1 FROM task_events x "
        "  WHERE x.task_id = t.id AND x.id > e.id AND x.kind IN (%s))" % ",".join("?" * len(end)),
        (_pr.PLACEMENT_WAIT_EVENT, *end),
    ).fetchall()
    out = []
    for tid, origin, raw in rows:
        try:
            p = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(p, dict) or p.get("cleared"):
            continue
        code = p.get("code") or str(p.get("reason") or "").split(" ", 1)[0]
        if code == CODE:
            out.append((tid, origin, p.get("detail")))
    return out


def clear_stale_labels(conn, result, dry_run: bool, cache: dict) -> list:
    """End the open ``pause_hold`` placement_wait of every card the CURRENT manifest no longer holds and this tick
    did not hold for another reason: one ``{cleared: true, reason}`` event each. Returns the cleared ids.

    The hold itself is always re-derived from the manifest by :func:`decide`; this only retires the LABEL a card
    keeps when the dispatcher never routes it again (no_worker, a human-lane assignee, a card whose origin_host was
    dropped). 11-06: t_1bf66aac / t_6aa3a051 read ``pause_hold (ci-box:...)`` 37 h after ci-box resumed and its
    manifest was archived. Best-effort, never raises; nothing on a dry run or with the switch off."""
    if dry_run or not enabled():
        return []
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_placement_reasons as _pr
    try:
        waits = getattr(result, "placement_waits", None) or {}
        stale = []
        for tid, origin, detail in _open_pause_labels(conn):
            if tid in waits:
                continue                           # this tick holds it: its own wait row is current
            verdict = decide(origin, tid, cache)
            if verdict is not None and verdict[0] == "wait":
                continue
            doc = cache.get(str(origin)) if origin else None
            now_reads = ("card has no origin_host" if not origin else
                         f"{origin} manifest phase {doc.get('phase')}" if isinstance(doc, dict) else
                         f"{origin} has no manifest.json")
            stale.append((tid, f"{CODE} ({detail}) no longer holds: {now_reads}"))
        if not stale:
            return []
        with _kb.write_txn(conn):
            for tid, why in stale:
                _kb._append_event(conn, tid, _pr.PLACEMENT_WAIT_EVENT, {"cleared": True, "reason": why})
        return [t for t, _ in stale]
    except Exception:
        _log.warning("kanban pause_hold: could not clear stale pause_hold labels", exc_info=True)
        return []


def holds(conn=None, *, now: Optional[float] = None) -> dict:
    """Read model for ``hermes kanban placement-holds``: every host manifest under :func:`root` with whether it
    holds right now, and (with ``conn``) the cards carrying an open ``pause_hold`` label, each marked live/stale."""
    t = time.time() if now is None else float(now)
    hosts = {}
    base = root()
    for d in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []:
        doc = _load(d.name, {})
        if doc is None:
            hosts[d.name] = {"manifest": None, "holds": False, "why": "no manifest.json (archived or never paused)"}
            continue
        if doc is _UNREADABLE:
            hosts[d.name] = {"manifest": None, "holds": True, "why": "manifest unreadable: every card HELD"}
            continue
        phase = doc.get("phase")
        stale = stale_reason(doc, t) if phase in HOLD_PHASES else None
        owed = [u.get("id") for u in doc.get("units") or () if u.get("lane") == "kanban" and owes_run(u)] \
            if phase in PIN_PHASES else []
        hosts[d.name] = {
            "manifest": doc.get("id"), "phase": phase, "lease_until": doc.get("lease_until"),
            "resumed_at": doc.get("resumed_at"),
            "driver_pid": (doc.get("driver") or {}).get("pid") if isinstance(doc.get("driver"), dict) else None,
            "driver_alive": _driver_alive(doc),
            "holds": phase in HOLD_PHASES and not stale,
            "why": stale or (f"phase {phase}" if phase in HOLD_PHASES else f"phase {phase}: not a hold"),
            "pins_owed": owed,
            "pin_conflict_expired": _pin_conflict_expired(d.name, {d.name: doc}, t),
        }
    cards = []
    if conn is not None:
        cache: dict = {}
        for tid, origin, detail in _open_pause_labels(conn):
            verdict = decide(origin, tid, cache)
            cards.append({"task_id": tid, "origin_host": origin, "label": detail,
                          "live": verdict is not None and verdict[0] == "wait",
                          "now": None if verdict is None else list(verdict)})
    return {"now": int(t), "hosts": hosts, "cards": cards}


def clear_card(conn, task_id: str, operator: str) -> bool:
    """Operator verb: end ``task_id``'s open pause_hold label now (``{cleared: true}``). It does NOT lift a live
    hold: the next tick re-derives it from the manifest and writes a fresh wait. True when a label was open."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_placement_reasons as _pr
    if not any(tid == task_id for tid, _o, _d in _open_pause_labels(conn)):
        return False
    with _kb.write_txn(conn):
        _kb._append_event(conn, task_id, _pr.PLACEMENT_WAIT_EVENT,
                          {"cleared": True, "reason": f"operator clear: {operator}"})
    return True


def refuse_if_held(conn, task_id: str) -> None:
    """Layer 2: raise :class:`PauseHeld` for a card whose origin host is in a hold phase. Caller holds the txn;
    the raise rolls it back, so nothing is written here (RC-P3-3: the caller records the refusal)."""
    if not enabled():
        return
    row = conn.execute("SELECT origin_host FROM tasks WHERE id = ?", (task_id,)).fetchone()
    origin = row[0] if row is not None else None
    if not origin:
        return
    verdict = decide(origin, task_id, {})
    if verdict is not None and verdict[0] == "wait":
        manifest_id = verdict[1].split(":", 1)[1].rstrip(")")
        raise PauseHeld(str(origin), manifest_id, task_id)


def record_refusal(conn, exc: PauseHeld, *, stage: str = "claim") -> None:
    """The ``claim_refused_pause_hold`` event, in its OWN txn after the opener's txn rolled back. Best-effort."""
    from hermes_cli import kanban_db as _kb
    from hermes_cli import kanban_db_connect as _kbc
    try:
        with _kbc.write_txn(conn):
            _kb._append_event(conn, exc.task_id, REFUSED_EVENT,
                              {"host": exc.host, "manifest_id": exc.manifest_id, "stage": stage})
    except Exception:
        _log.warning("kanban pause_hold: could not record the refusal for %s", exc.task_id, exc_info=True)
