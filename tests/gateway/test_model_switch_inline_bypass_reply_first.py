"""Reply-first /model on a BUSY session (t_2bf22375, extends #169 / t_2ab07928).

2026-11-10 21:00:52 PST: a typed /model arrived while the same session was still running a turn
(a /compress). The base adapter dispatched it inline (``_handle_message_while_active`` ->
``_dispatch_inline_reply``), not through ``_process_message_background``, so the after-delivery save
of #169 could not register and the confirmation waited for the whole save (reply_s == total_s ==
9.244 s). The inline path now runs the save after the inline reply is sent.

Real ``BasePlatformAdapter`` busy-session path, real ``/model`` handler, real ``SessionStore``; the
state.db write is an event-gated stand-in that releases itself SAVE_S after it starts.
"""

import asyncio
import logging
import re
import time

import pytest

from tests.gateway.test_model_switch_reply_first import FOLLOWUP, _drained, _ev, _Harness, _switched
from tests.gateway.test_turn_lease_flush_window import _wait_for

SAVE_S = 1.5  # the slow state.db write


def _phase_fields(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("PHASE=model_cmd ")]
    return [dict(re.findall(r"(\w+)=(\S+)", line)) for line in lines], lines


async def _busy(h):
    """Hold the session in a running turn until the returned event is set."""
    release = asyncio.Event()
    inner = h.adapter._message_handler

    async def handler(event):
        if event.text == "long turn":
            await release.wait()
            return "long turn done"
        return await inner(event)

    h.adapter.set_message_handler(handler)
    await h.adapter.handle_message(_ev("long turn"))
    await _wait_for(lambda: h.adapter._session_tasks, "the busy turn to own the session")
    return release


@pytest.mark.asyncio
async def test_busy_session_model_reply_is_sent_before_the_save_settles(tmp_path, monkeypatch, caplog):
    h = _Harness(tmp_path, monkeypatch)
    busy = await _busy(h)
    stamps = {}

    async def _release_after_save_time():
        await h.db.entered.wait()
        stamps["save_started"] = time.monotonic()
        await asyncio.sleep(SAVE_S)
        stamps["save_released"] = time.monotonic()
        h.db.release.set()

    releaser = asyncio.create_task(_release_after_save_time())
    started = time.monotonic()
    try:
        with caplog.at_level(logging.INFO, logger="gateway.run"):
            # Busy session: the base adapter dispatches the command inline.
            cmd = asyncio.create_task(h.adapter.handle_message(_ev("/model gpt-5.5")))
            await _wait_for(lambda: h.sent_with(_switched("gpt-5.5")), "the busy-session /model confirmation",
                            timeout=SAVE_S * 4)
            reply_latency = time.monotonic() - started
            saved_at_reply = list(h.db.models)
            await asyncio.wait_for(cmd, SAVE_S * 4)
            settled_latency = time.monotonic() - started
        print(f"busy-session /model: reply {reply_latency:.3f}s, save settled {settled_latency:.3f}s "
              f"(save held {SAVE_S}s)")
        assert saved_at_reply == [], "the confirmation waited for the state.db write"
        assert reply_latency < SAVE_S / 2, f"reply took {reply_latency:.2f}s behind a {SAVE_S}s save"
        # The save still ran, after the reply, and is durable.
        assert h.db.models == ["gpt-5.5"]
        assert h.runner.session_store.get_model_override(h.key())["model"] == "gpt-5.5"
        assert not h.sent_with(FOLLOWUP)
        fields, lines = _phase_fields(caplog)
        assert len(fields) == 1, lines
        f = fields[0]
        assert f["path"] == "inline_bypass" and f["save"] == "ok", lines[0]
        assert float(f["reply_s"]) < SAVE_S / 2 <= float(f["total_s"]), lines[0]
        print(lines[0])
    finally:
        h.db.release.set()
        busy.set()
        await releaser
        await _drained(h)


@pytest.mark.asyncio
async def test_busy_session_switch_lock_is_released_after_the_inline_save(tmp_path, monkeypatch):
    """No write outlives its owner: the switch lock is held until the save lands, then dropped, and
    the busy turn's next queued message runs on the new model."""
    h = _Harness(tmp_path, monkeypatch)
    busy = await _busy(h)
    try:
        cmd = asyncio.create_task(h.adapter.handle_message(_ev("/model gpt-5.5")))
        await _wait_for(lambda: h.sent_with(_switched("gpt-5.5")), "confirmation")
        await asyncio.wait_for(h.db.entered.wait(), 5)
        lock_key = h.runner._model_switch_lock_key(_ev("x").source)
        assert h.runner._model_switch_locks().get(lock_key).locked(), "save ran outside the switch lock"
        await h.adapter.handle_message(_ev("hello"))  # queued behind the busy turn
        h.db.release.set()
        await asyncio.wait_for(cmd, 5)
        assert lock_key not in h.runner._model_switch_locks()
        busy.set()
        await _wait_for(lambda: h.turns, "queued turn")
        assert h.turns == [("hello", "gpt-5.5")]
    finally:
        h.db.release.set()
        busy.set()
        await _drained(h)
