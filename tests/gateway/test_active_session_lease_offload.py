"""Real inbound boundary with an isolated registry and deterministic I/O barriers."""
import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_cli import active_sessions
from tests.gateway.test_max_concurrent_sessions import (
    _make_event, _make_runner, _silence_global_gateway_hooks,
)


@pytest.mark.asyncio
async def test_registry_write_does_not_block_inbound_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(1)
    runner._persist_active_agents = MagicMock()
    runner._handle_message_with_agent = AsyncMock(return_value="ok")
    runner._post_turn_hooks_after_delivery = AsyncMock()
    runner._clear_durable_active_turn = AsyncMock()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_write = active_sessions._write_entries

    def slow_write(*args):
        # Fails immediately on base, rather than deadlocking the event loop.
        assert threading.get_ident() != loop_thread, "registry I/O on event loop"
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "test did not release registry barrier"
        return real_write(*args)

    monkeypatch.setattr(active_sessions, "_write_entries", slow_write)
    turn = asyncio.create_task(runner._handle_message(_make_event()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        heartbeat = asyncio.Event()
        loop.call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), 2)
    finally:
        release.set()
        await turn
        await runner._drain_active_session_releases()
    assert not active_sessions._read_entries(tmp_path / "runtime" / "active_sessions.json")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _silence_global_gateway_hooks(monkeypatch)
    runner = _make_runner(1)
    runner._persist_active_agents = MagicMock()
    runner._handle_message_with_agent = AsyncMock(return_value="ok")
    runner._post_turn_hooks_after_delivery = AsyncMock()
    runner._clear_durable_active_turn = AsyncMock()
    return runner, tmp_path / "runtime" / "active_sessions.json"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_pending_claim_owns_slot_and_cancellation_releases(rig, monkeypatch, cancel):
    runner, path = rig
    entered = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    real_write = active_sessions._write_entries
    releases = []
    real_release = active_sessions.ActiveSessionLease.release

    def release(lease):
        releases.append(lease.lease_id)
        return real_release(lease)

    def write(*args):
        loop.call_soon_threadsafe(entered.set)
        assert proceed.wait(5)
        return real_write(*args)

    monkeypatch.setattr(active_sessions, "_write_entries", write)
    monkeypatch.setattr(active_sessions.ActiveSessionLease, "release", release)
    turn = asyncio.create_task(runner._handle_message(_make_event()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # Different chat cannot overtake the locally reserved pending slot.
        rejected = await runner._handle_message(_make_event(chat_id="other"))
        assert "(1/1)" in rejected
        assert runner._handle_message_with_agent.await_count == 0
        if cancel:
            turn.cancel()
            await asyncio.sleep(0)
            turn.cancel()  # repeated cancellation must not abandon the worker
    finally:
        proceed.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await turn
    else:
        assert await turn == "ok"
    await runner._drain_active_session_releases()
    assert len(releases) == 1
    assert not active_sessions._read_entries(path)
    assert not runner._running_agents
    assert not runner._active_session_claim_tasks
    assert not runner._active_session_claim_locks


@pytest.mark.asyncio
async def test_cancel_before_dispatch_never_acquires(rig, monkeypatch):
    runner, path = rig
    acquire = MagicMock(wraps=active_sessions.try_acquire_active_session)
    monkeypatch.setattr(active_sessions, "try_acquire_active_session", acquire)
    turn = asyncio.create_task(runner._handle_message(_make_event()))
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    acquire.assert_not_called()
    assert not active_sessions._read_entries(path)


@pytest.mark.asyncio
async def test_exception_and_duplicate_finalizer_release_once(rig, monkeypatch):
    from gateway.session import build_session_key
    runner, path = rig
    releases = []
    real_release = active_sessions.ActiveSessionLease.release

    def release(lease):
        releases.append(lease.lease_id)
        return real_release(lease)

    monkeypatch.setattr(active_sessions.ActiveSessionLease, "release", release)
    runner._handle_message_with_agent.side_effect = RuntimeError("turn failed")
    event = _make_event()
    with pytest.raises(RuntimeError, match="turn failed"):
        await runner._handle_message(event)
    runner._release_running_agent_state(build_session_key(event.source))
    await runner._drain_active_session_releases()
    assert len(releases) == 1
    assert not active_sessions._read_entries(path)


@pytest.mark.asyncio
async def test_cross_runner_claim_honors_registry_cap(rig):
    runner, path = rig
    other = _make_runner(1)
    a, b = await asyncio.gather(
        runner._claim_active_session_slot_off_loop("a", _make_event().source),
        other._claim_active_session_slot_off_loop("b", _make_event().source),
    )
    winners = [lease for lease, _ in (a, b) if lease is not None]
    assert len(winners) == 1
    assert len(active_sessions._read_entries(path)) == 1
    assert sum(message is not None for _, message in (a, b)) == 1
    await runner._release_active_session_lease(winners[0])
    assert not active_sessions._read_entries(path)

