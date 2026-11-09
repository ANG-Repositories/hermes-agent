"""Per-stage wall time of one typed ``/model`` (``/m``) command: one INFO line per command (t_7fe48f0b).

An idle-session ``/m opus-d`` took ~79 s on 2026-11-09 while ``switch_model()`` alone took 0.09 s warm
and 3.6 s cold; nothing said where the rest went. The handler marks each stage boundary with
:func:`phase` (one line per boundary, so the long handler body is not re-indented); the time from one
boundary to the next belongs to the earlier phase, and the open phase closes when the command returns::

    PHASE=model_cmd session=<key> outcome=ok lock_wait_s=0.000 config_s=0.002 normalize_s=0.001 ...
        record_s=4.210 record_get_session_s=0.020 record_update_model_s=4.100 ... total_s=4.300

Phases are exclusive and sum to ``total_s - other_s`` (``other_s`` = time before the first phase).
``record_s`` is derived: the sum of the ``record_*`` phases, one per DB/store call. ``durable_*``
fields are nested spans inside ``record_durable`` (config write, override clear, override write-through)
and are not added again. A phase the command never reached prints ``-``.
Telemetry only: every entry point swallows its own errors, so it can never change the reply.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from typing import Dict, Iterator, Optional

logger = logging.getLogger("gateway.run")  # same record stream as the /model handler

# Exclusive phases, in handler order (gateway/slash_commands.py ``_handle_model_command_locked``).
PHASES = ("lock_wait", "config", "normalize", "prepare", "listing", "resolve", "enrich", "guard",
          "fallback_clear", "agent_switch", "record_get_session", "record_update_model", "record_override",
          "announce", "record_durable", "confirm")
RECORD_PHASES = ("record_get_session", "record_update_model", "record_override", "record_durable")
# Nested spans inside ``record_durable`` (_commit_model_switch_durable).
DURABLE_SPANS = ("durable_config", "durable_clear_override", "durable_set_override")

_current: contextvars.ContextVar[Optional["ModelCmdTiming"]] = contextvars.ContextVar(
    "model_cmd_timing", default=None)


class ModelCmdTiming:
    def __init__(self) -> None:
        self.session = "-"
        self.t0 = time.monotonic()
        self.phase: Optional[str] = None
        self.phase_t0 = self.t0
        self.phases: Dict[str, float] = {}
        self.spans: Dict[str, float] = {}
        self.finished = False

    def enter(self, name: Optional[str], now: float) -> None:
        if self.phase is not None:
            self.phases[self.phase] = self.phases.get(self.phase, 0.0) + max(now - self.phase_t0, 0.0)
        self.phase, self.phase_t0 = name, now

    def line(self, outcome: str, total_s: float) -> str:
        def _fmt(name: str, table: Dict[str, float]) -> str:
            value = table.get(name)
            return f"{name}_s=" + ("-" if value is None else f"{value:.3f}")

        fields = [f"PHASE=model_cmd session={self.session} outcome={outcome}"]
        fields += [_fmt(name, self.phases) for name in PHASES]
        reached = [self.phases[n] for n in RECORD_PHASES if n in self.phases]
        fields.append("record_s=" + (f"{sum(reached):.3f}" if reached else "-"))
        fields += [_fmt(name, self.spans) for name in DURABLE_SPANS]
        other = total_s - sum(self.phases.values())
        fields.append(f"other_s={max(other, 0.0):.3f} total_s={total_s:.3f}")
        return " ".join(fields)


def begin() -> Optional[ModelCmdTiming]:
    """Start the timer for this command in phase ``lock_wait``. Never raises."""
    try:
        timing = ModelCmdTiming()
        timing.enter("lock_wait", timing.t0)
        _current.set(timing)
        return timing
    except Exception:
        return None


def phase(name: str) -> None:
    """Close the open phase and open *name* (no-op outside a timed command). Never raises."""
    try:
        timing = _current.get()
        if timing is not None and not timing.finished:
            timing.enter(name, time.monotonic())
    except Exception:
        pass


def finish(timing: Optional[ModelCmdTiming], outcome: str = "ok") -> None:
    """Close the open phase and log the line (no-op for None, once per timer). Never raises."""
    if timing is None:
        return
    try:
        if timing.finished:
            return
        now = time.monotonic()
        timing.enter(None, now)
        timing.finished = True
        logger.info("%s", timing.line(outcome, now - timing.t0))
    except Exception:
        pass


def annotate(*, session: Optional[str] = None) -> None:
    """Name the session key on the active timer once the handler knows it. Never raises."""
    try:
        timing = _current.get()
        if timing is not None and not timing.finished and session:
            timing.session = str(session)
    except Exception:
        pass


@contextlib.contextmanager
def span(name: str) -> Iterator[None]:
    """Record the wall time of the ``with`` body (awaits included) as nested span *name*. Exceptions
    from the body propagate unchanged; the timing itself never raises."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        try:
            timing = _current.get()
            if timing is not None and not timing.finished:
                timing.spans[name] = timing.spans.get(name, 0.0) + max(time.monotonic() - t0, 0.0)
        except Exception:
            pass


def outcome_for(exc: Optional[BaseException]) -> str:
    if exc is None:
        return "ok"
    if type(exc).__name__ == "CancelledError":
        return "cancelled"
    return "error"
