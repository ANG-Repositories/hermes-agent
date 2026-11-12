"""Studio kanban ceiling: the wait page (t_103a4648).

The ceiling itself is ``hosts.<studio>.consumers.kanban.ceiling`` in the fleet host table (config/fleet-hosts.json,
``eligible: true`` + ``ceiling: N``), read by the LoadGate on every tick (kanban_load_gate.utilization_context).
Arming it is an operator edit to that table. This module is only the page: when a Studio card has been held at the
ceiling for ``kanban.dispatch_load_gate.ceiling_wait_page_minutes`` (default 60; 0 = off), ONE #alerts page per
episode through episode_pager (producer ``kanban-studio-ceiling``). The episode clears, with a resolve line, on the
first tick where no card is held at the ceiling.

The wait clock is per card and lives in this process: a card that spawns or stops being held restarts its clock,
and a gateway restart restarts every clock (the page then comes at most one threshold late). episode_pager's state
file de-duplicates the page itself across restarts.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional

_log = logging.getLogger(__name__)

DEFAULT_PAGE_MINUTES = 60
PRODUCER = "kanban-studio-ceiling"
KEY = "studio-ceiling-wait"


def page_minutes(kanban_cfg: Optional[Mapping]) -> Optional[float]:
    """``kanban.dispatch_load_gate.ceiling_wait_page_minutes``: minutes, or None when set to 0 (page off).
    Missing or unreadable = the default 60."""
    gate = (kanban_cfg or {}).get("dispatch_load_gate") if isinstance(kanban_cfg, Mapping) else None
    raw = gate.get("ceiling_wait_page_minutes", DEFAULT_PAGE_MINUTES) if isinstance(gate, Mapping) else None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return float(DEFAULT_PAGE_MINUTES)
    if val != val or val < 0:
        return float(DEFAULT_PAGE_MINUTES)
    return None if val == 0 else val


def episode_pager_sender(root) -> Callable[[str, str], bool]:
    """``send(verb, text)`` over ``<root>/scripts/lib/episode_pager.py``; False when the pager is missing or fails."""
    script = Path(root) / "scripts" / "lib" / "episode_pager.py"

    def send(verb: str, text: str) -> bool:
        if not script.is_file():
            _log.warning("kanban studio ceiling: episode_pager.py missing at %s", script)
            return False
        args = [verb, "--producer", PRODUCER, "--key", KEY, "--via", "fleet-alert",
                "--severity", "warning", "--subsystem", "kanban-placement"]
        args += (["--text", text[:1800], "--remind-h", "6"] if verb == "page" else ["--resolve-text", text[:1800]])
        try:
            r = subprocess.run([sys.executable, str(script), *args], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            _log.warning("kanban studio ceiling pager unavailable", exc_info=True)
            return False
        if r.returncode:
            _log.warning("kanban studio ceiling pager failed rc=%s: %s", r.returncode, (r.stderr or "")[:300])
            return False
        return True

    return send


class CeilingWaitWatch:
    """One episode at a time. ``observe()`` once per gateway tick with the cards held at the ceiling."""

    def __init__(self, pager: Callable[[str, str], bool]) -> None:
        self._pager = pager
        self._since: Dict[str, float] = {}
        self._paged = False

    def observe(self, held: Iterable[str], *, now: float, minutes: Optional[float],
                ceiling: Optional[int], running: Optional[int]) -> Optional[str]:
        """Returns the pager verb sent this tick (``page`` / ``clear``), or None."""
        held = list(dict.fromkeys(held or ()))
        self._since = {t: self._since.get(t, now) for t in held}
        if not held:
            if self._paged and self._pager("clear", "Studio kanban ceiling: no card is waiting at the ceiling now."):
                self._paged = False
                return "clear"
            return None
        if minutes is None or self._paged:
            return None
        oldest_id, oldest_at = min(self._since.items(), key=lambda kv: kv[1])
        waited = now - oldest_at
        if waited < minutes * 60:
            return None
        text = (f"Studio kanban ceiling: {len(held)} card(s) waiting at the ceiling; the oldest, {oldest_id}, "
                f"has waited {waited / 60:.0f} min (page threshold {minutes:g} min). "
                f"Running on the Studio {'?' if running is None else running} of ceiling "
                f"{'?' if ceiling is None else ceiling}. Waiting: {', '.join(held[:10])}"
                f"{' ...' if len(held) > 10 else ''}. The ceiling is hosts.mac-studio.consumers.kanban.ceiling in "
                f"config/fleet-hosts.json; asap cards are exempt.")
        if self._pager("page", text):
            self._paged = True
            return "page"
        return None
