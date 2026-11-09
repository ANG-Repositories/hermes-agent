"""/stop finishes when the async-delegation cancel times out on its file lock.

Live 2026-11-08 15:38 (#1561038985004974203): /stop bumped the generation, then
``interrupt_for_session`` raised ``TimeoutError: timed out acquiring .../async-delegations.lock``
after its 5 s lock wait. The exception escaped ``_interrupt_and_clear_session`` before the marker,
the adapter interrupt and the slot release, so the user saw the error and the session stayed busy
(the next /model was refused). The cancel now runs off the event loop and fails open.
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

import tools.async_delegation as async_delegation
from gateway.session import build_session_key
from tests.gateway.test_stop_marker_fast_lookup import _SpyDB, _runner, _source


@pytest.mark.asyncio
async def test_stop_releases_the_session_when_the_delegation_cancel_times_out(monkeypatch):
    seen = {}

    def _timeout(**_kwargs):
        seen["thread"] = threading.current_thread()
        raise TimeoutError("timed out acquiring /x/state/async-delegations.lock")

    monkeypatch.setattr(async_delegation, "interrupt_for_session", _timeout)
    source = _source()
    key = build_session_key(source)
    runner = _runner(_SpyDB([1, 2, 3]), key)
    agent = MagicMock()
    agent.session_id = "sess-big"
    runner._running_agents[key] = agent
    runner._session_state(key).turn.started_ts = time.time()

    await runner._interrupt_and_clear_session(
        key, source, interrupt_reason="stop", invalidation_reason="stop_command")

    assert not runner._is_session_running(key), "slot must be released even when the cancel fails"
    assert runner._test_marks, "the user-stopped marker must still be written"
    # The lock wait runs on a worker thread, never on the event loop.
    assert seen["thread"] is not threading.main_thread()
