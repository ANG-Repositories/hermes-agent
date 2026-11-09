"""Per-step wall time for the post-loop turn tail (t_238cc30d).

The gap between a turn's last ``API call #N`` line and ``Turn ended`` ran 5-25 s on large
gateway sessions with nothing logged inside it. ``finalize_turn`` opens one recorder on the
agent; the persist path reports its own sub-steps into it through :func:`timed`, so the
seconds are attributed to a named step without threading a timer through every signature.
One INFO line is emitted per phase only when the phase total crosses ``SLOW_TAIL_S``.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

# Below this a tail is unremarkable (p90 0.82 s on 446 measured gateway turns).
SLOW_TAIL_S = 1.0

_ATTR = "_turn_step_timer"


class StepTimer:
    """Ordered ``(label, seconds)`` pairs; nested labels are recorded as ``outer.inner``."""

    def __init__(self) -> None:
        self.steps: List[Tuple[str, float]] = []
        self._stack: List[str] = []
        self._t0 = time.monotonic()

    @contextmanager
    def step(self, label: str) -> Iterator[None]:
        name = ".".join([*self._stack, label])
        self._stack.append(label)
        t0 = time.monotonic()
        try:
            yield
        finally:
            self._stack.pop()
            self.steps.append((name, time.monotonic() - t0))

    def total(self) -> float:
        return time.monotonic() - self._t0

    def summary(self) -> str:
        return " ".join(f"{name}={secs:.2f}s" for name, secs in self.steps)

    def log_if_slow(self, logger, phase: str, session_id: Optional[str]) -> None:
        total = self.total()
        if total >= SLOW_TAIL_S:
            logger.info("turn tail timing: phase=%s total=%.2fs session=%s %s",
                        phase, total, session_id or "none", self.summary())


def start(agent, turn_id: str = "") -> StepTimer:
    timer = StepTimer()
    setattr(agent, _ATTR, timer)
    setattr(agent, _ATTR + "_turn", turn_id)
    return timer


def resume_or_start(agent, turn_id: str = "") -> StepTimer:
    """The recorder the final-text path opened for THIS turn (its flush is part of the tail),
    else a fresh one: a recorder left by a turn that never reached the finalizer is discarded."""
    timer = getattr(agent, _ATTR, None)
    if isinstance(timer, StepTimer) and turn_id and getattr(agent, _ATTR + "_turn", None) == turn_id:
        return timer
    return start(agent, turn_id)


def stop(agent) -> None:
    setattr(agent, _ATTR, None)
    setattr(agent, _ATTR + "_turn", None)


@contextmanager
def timed(agent, label: str) -> Iterator[None]:
    """Record ``label`` on the agent's active recorder; a no-op outside ``finalize_turn``."""
    timer = getattr(agent, _ATTR, None)
    if not isinstance(timer, StepTimer):
        yield
        return
    with timer.step(label):
        yield
