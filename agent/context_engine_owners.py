"""Owner count for an agent's context engine: never shut the engine down under a running turn.

A soft eviction (``AIAgent.release_clients`` on a gateway daemon thread) can reach an agent whose
turn is still executing: ``/stop`` clears the session's running-agent slot before it evicts the
cached agent, so the mid-turn guard in ``_evict_cached_agent`` misses the still-draining turn.
Shutting LCM's stores down then freed the SQLite connection the turn was executing on (gateway
SIGSEGV 2026-11-02 06:50, t_055f9d8d). Each ``run_conversation`` is an owner for its duration; a
release requested while an owner is inside is recorded and run by the last owner to leave.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

_INIT_LOCK = threading.Lock()


class _Owners:
    __slots__ = ("lock", "turns", "pending")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.turns = 0
        self.pending = False


def _owners(agent: Any) -> _Owners:
    owners = agent.__dict__.get("_context_engine_owners")
    if owners is None:
        with _INIT_LOCK:
            owners = agent.__dict__.get("_context_engine_owners")
            if owners is None:
                owners = agent.__dict__["_context_engine_owners"] = _Owners()
    return owners


def enter_turn(agent: Any) -> None:
    owners = _owners(agent)
    with owners.lock:
        owners.turns += 1


def exit_turn(agent: Any, shutdown: Callable[[], None]) -> None:
    """Leave a turn; the last owner out runs a shutdown that was deferred while it was inside."""
    owners = _owners(agent)
    with owners.lock:
        owners.turns = max(0, owners.turns - 1)
        run = owners.pending and owners.turns == 0
        if run:
            owners.pending = False
    if run:
        shutdown()


def shutdown_or_defer(agent: Any, shutdown: Callable[[], None]) -> bool:
    """Run ``shutdown`` now when no turn is inside, else defer it to the last turn's exit.

    Returns True when it ran now."""
    owners = _owners(agent)
    with owners.lock:
        if owners.turns > 0:
            owners.pending = True
            return False
    shutdown()
    return True
