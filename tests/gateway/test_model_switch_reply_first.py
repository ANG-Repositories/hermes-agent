"""Reply-first /model (t_2ab07928).

2026-11-09: an idle-session ``/m opus-d`` replied after ~79 s because the confirmation waited on the
durable writes (state.db model column, session-store identity, config write-through) while state.db
writes were stalling, and a /model in another thread waited on the runner-wide switch lock.

Now the switch is applied in memory, the confirmation is sent, and the durable writes run as the
adapter's post-delivery callback inside the session task; the switch lock is per session, with one
runner-wide lock left only around the ``--global`` config.yaml write (#100314).

Real ``BasePlatformAdapter`` session task + post-delivery callback + /stop path, real fork
``/model`` handler, real ``SessionStore``; the state.db write is an event-gated stand-in.
"""

import asyncio
import threading
import time

import hermes_yaml as yaml
import pytest

from agent.i18n import t
from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from tests.gateway.test_model_picker_persist import _make_store_runner, _setup_isolated_home
from tests.gateway.test_turn_lease_flush_window import _Adapter, _wait_for

FOLLOWUP = "switch active for this session, but saving it failed:"


def _source(chat_id="12345"):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm")


def _ev(text, chat_id="12345"):
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=_source(chat_id), message_id=text)


def _switched(model):
    return t("gateway.model.switched", model=model)


async def _drained(h, timeout=5.0):
    """Wait until the session's deferred jobs are committed and the session task is done; read the
    store only after that (a concurrent read races the store creating its tables)."""
    await _wait_for(lambda: not any(not t.done() for t in list(h.adapter._session_tasks.values())),
                    "deferred writes to settle", timeout)


class _GatedDB:
    """state.db stand-in: ``update_session_model`` blocks until ``release`` is set."""

    def __init__(self):
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.models = []

    async def update_session_model(self, session_id, model, **_kw):
        self.entered.set()
        await self.release.wait()
        self.models.append(model)


class _Harness:
    def __init__(self, tmp_path, monkeypatch, *, gate_resolution=None):
        from hermes_cli.model_switch import ModelSwitchResult

        self.cfg_path = _setup_isolated_home(tmp_path, monkeypatch, {"default": "old-model", "provider": "openrouter"})
        self.resolution_gate = threading.Event()

        def _switch(**kw):
            if gate_resolution and kw["raw_input"] == gate_resolution:
                self.resolution_gate.wait(10)  # a cold switch_model() resolution
            return ModelSwitchResult(
                success=True, new_model=kw["raw_input"], target_provider="openrouter", provider_changed=False,
                api_key="sk-test", base_url="https://openrouter.ai/api/v1", api_mode="chat_completions",
                provider_label="OpenRouter", is_global=kw.get("is_global", False))

        monkeypatch.setattr("hermes_cli.model_switch.switch_model", _switch)
        self.adapter = adapter = _Adapter()
        runner = _make_store_runner(adapter, tmp_path / "sessions", monkeypatch)
        runner.adapters = {Platform.TELEGRAM: adapter}
        runner._delivery_adapter_for = lambda _source: adapter
        runner._session_db = self.db = _GatedDB()
        self.runner = runner
        self.turns = []

        async def handler(event):
            cmd = event.get_command()
            if cmd in ("model", "m"):
                return await runner._handle_model_command(event)
            if cmd == "stop":
                return "stopped"
            model, _runtime = runner._resolve_session_agent_runtime(source=event.source)
            self.turns.append((event.text, model))
            return f"answer {event.text}"

        adapter.set_message_handler(handler)

    def key(self, chat_id="12345"):
        return self.runner._session_key_for_source(_source(chat_id))

    def sent_with(self, needle):
        return [m for m in self.adapter.sent if needle in m]


@pytest.mark.asyncio
async def test_reply_is_sent_before_a_blocked_state_db_write(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    started = time.monotonic()
    await h.adapter.handle_message(_ev("/model gpt-5.5"))
    await _wait_for(lambda: h.sent_with(_switched("gpt-5.5")), "the /model confirmation while state.db is blocked")
    reply_latency = time.monotonic() - started
    assert reply_latency < 2.0, f"reply took {reply_latency:.2f}s"
    await asyncio.wait_for(h.db.entered.wait(), 5)  # the write runs after the send, and is still blocked
    assert h.db.models == []
    h.db.release.set()
    await _wait_for(lambda: h.db.models == ["gpt-5.5"], "deferred state.db write to land")
    await _drained(h)
    assert h.runner.session_store.get_model_override(h.key())["model"] == "gpt-5.5"
    assert not h.sent_with(FOLLOWUP)


@pytest.mark.asyncio
async def test_next_turn_reads_the_new_model_while_the_write_is_blocked(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    await h.adapter.handle_message(_ev("/model gpt-5.5"))
    await _wait_for(lambda: h.sent_with(_switched("gpt-5.5")), "confirmation")
    await asyncio.wait_for(h.db.entered.wait(), 5)
    # Nothing durable has landed yet: the in-memory override alone decides the next turn.
    assert h.runner.session_store.get_model_override(h.key()) is None
    model, _runtime = h.runner._resolve_session_agent_runtime(source=_source())
    assert model == "gpt-5.5"
    # A message sent now queues behind the deferred write and runs on the new model.
    await h.adapter.handle_message(_ev("hello"))
    assert h.turns == [], "a turn ran while the session's deferred write was still in flight"
    h.db.release.set()
    await _wait_for(lambda: h.turns, "queued turn")
    assert h.turns == [("hello", "gpt-5.5")]


@pytest.mark.asyncio
async def test_another_sessions_model_is_not_blocked_by_a_slow_switch(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch, gate_resolution="slow-model")
    h.db.release.set()
    slow = asyncio.ensure_future(h.runner._handle_model_command(_ev("/model slow-model", chat_id="111")))
    await asyncio.sleep(0.2)  # the first switch holds its (per-session) lock inside resolution
    try:
        fast = await asyncio.wait_for(h.runner._handle_model_command(_ev("/model fast-model", chat_id="222")), 5)
        assert not slow.done(), "precondition: the first switch is still inside its lock"
    finally:
        h.resolution_gate.set()
    assert _switched("fast-model") in fast
    assert _switched("slow-model") in await asyncio.wait_for(slow, 10)


@pytest.mark.asyncio
async def test_same_session_switches_commit_in_issue_order(tmp_path, monkeypatch):
    """A second /model while the first's deferred write is blocked replies at once, queues behind
    it, and the writes land in issue order: memory, store and state.db all end on the later pick."""
    h = _Harness(tmp_path, monkeypatch)
    await h.adapter.handle_message(_ev("/model model-A"))
    await _wait_for(lambda: h.sent_with(_switched("model-A")), "first confirmation")
    await asyncio.wait_for(h.db.entered.wait(), 5)
    second = asyncio.create_task(h.adapter.handle_message(_ev("/model model-B")))
    await asyncio.sleep(0.05)
    assert not second.done()
    assert not h.sent_with(_switched("model-B"))
    h.db.release.set()
    await asyncio.wait_for(second, 5)
    await _drained(h)
    # model-A's state.db write was already in flight when model-B was issued; it lands first.
    assert h.db.models == ["model-A", "model-B"]
    assert h.runner.session_store.get_model_override(h.key())["model"] == "model-B"
    assert h.runner._session_model_override(h.key())["model"] == "model-B"
    assert not h.sent_with(FOLLOWUP)


@pytest.mark.asyncio
async def test_concurrent_global_switches_serialize_the_config_write(tmp_path, monkeypatch):
    """#100314 regression: with the per-session lock, two sessions' ``--global`` picks still never
    interleave on config.yaml."""
    h = _Harness(tmp_path, monkeypatch)
    h.db.release.set()
    from hermes_cli.model_switch import persist_model_selection

    live, peak, order = [0], [0], []

    async def _write(result, config_path):
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        await asyncio.sleep(0.1)  # a slow disk inside the write
        await asyncio.to_thread(persist_model_selection, result, config_path)
        order.append(result.new_model)
        live[0] -= 1

    monkeypatch.setattr("gateway.slash_commands_model._persist_model_switch_to_config", _write)
    await asyncio.gather(h.runner._handle_model_command(_ev("/model model-A --global", chat_id="111")),
                         h.runner._handle_model_command(_ev("/model model-B --global", chat_id="222")))
    assert peak[0] == 1, "two --global config.yaml writes overlapped"
    # An older issue may be superseded before it reaches config; any writes
    # that do run are serialized and the last committed issue wins.
    assert order and set(order) <= {"model-A", "model-B"}
    assert yaml.safe_load(h.cfg_path.read_text(encoding="utf-8"))["model"]["default"] == order[-1]


@pytest.mark.asyncio
async def test_deferred_global_write_failure_posts_a_followup_line(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    h.db.release.set()

    async def _disk_full(result, config_path):
        raise OSError("disk full")

    monkeypatch.setattr("gateway.slash_commands_model._persist_model_switch_to_config", _disk_full)
    await h.adapter.handle_message(_ev("/model gpt-5.5 --global"))
    await _wait_for(lambda: h.sent_with(FOLLOWUP), "follow-up line for the failed config write")
    reply = h.sent_with(_switched("gpt-5.5"))[0]
    assert "Saved to config.yaml" not in reply and "disk full" not in reply
    followup = h.sent_with(FOLLOWUP)[0]
    assert "OSError" in followup and "disk full" not in followup
    assert h.adapter.sent.index(reply) < h.adapter.sent.index(followup)
    # #100314 semantics: the switch truthfully stays a session override (memory + store).
    assert yaml.safe_load(h.cfg_path.read_text(encoding="utf-8"))["model"]["default"] == "old-model"
    assert h.runner._session_model_override(h.key())["model"] == "gpt-5.5"
    await _drained(h)
    assert h.runner.session_store.get_model_override(h.key())["model"] == "gpt-5.5"


@pytest.mark.asyncio
async def test_stop_during_the_deferred_write_keeps_the_queued_message(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    await h.adapter.handle_message(_ev("/model gpt-5.5"))
    await _wait_for(lambda: h.sent_with(_switched("gpt-5.5")), "confirmation")
    await asyncio.wait_for(h.db.entered.wait(), 5)
    await h.adapter.handle_message(_ev("hello"))  # queued behind the deferred write
    stop = asyncio.create_task(h.adapter.handle_message(_ev("/stop")))
    await asyncio.sleep(0.05)
    h.db.release.set()
    await stop
    await _wait_for(lambda: h.turns, "queued message after /stop")
    assert h.turns == [("hello", "gpt-5.5")]
    assert "_model_switch_pending" not in h.runner.__dict__
    assert len(h.sent_with(FOLLOWUP)) == 1
    assert "CancelledError" in h.sent_with(FOLLOWUP)[0]




# --------------------------------------------------------------------------- Prism round 1 (#169)


@pytest.mark.asyncio
async def test_picker_tap_lands_after_a_queued_typed_switch(tmp_path, monkeypatch):
    """Prism f05146212848 / ea8bef1a2f75: a picker tap while a typed /model's deferred write is still
    blocked commits after it; the older typed switch never overwrites the tap (db, store, config)."""
    from tests.gateway.test_model_picker_persist import _FakePickerAdapter

    h = _Harness(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.model_switch_providers.list_picker_providers",
                        lambda **kw: [{"slug": "openrouter", "name": "OpenRouter", "models": ["model-B"]}])
    monkeypatch.setattr(type(h.adapter), "send_model_picker", _FakePickerAdapter.send_model_picker, raising=False)
    h.adapter.captured_callback = None
    assert await h.runner._handle_model_command(_ev("/model --global")) is None
    await h.adapter.handle_message(_ev("/model model-A --global"))
    await _wait_for(lambda: h.sent_with(_switched("model-A")), "typed confirmation")
    await asyncio.wait_for(h.db.entered.wait(), 5)
    tap = asyncio.ensure_future(h.adapter.captured_callback("12345", "model-B", "openrouter"))
    await asyncio.sleep(0.2)
    assert not tap.done(), "the tap committed before the queued typed switch"
    h.db.release.set()
    assert _switched("model-B") in await asyncio.wait_for(tap, 10)
    await _drained(h)
    assert h.db.models[-1] == "model-B"
    assert yaml.safe_load(h.cfg_path.read_text(encoding="utf-8"))["model"]["default"] == "model-B"


@pytest.mark.asyncio
async def test_pending_switch_survives_persisted_route_reconciliation(tmp_path, monkeypatch):
    """Prism 396516742752: with model-A persisted and the switch to model-B not yet written, the
    runtime resolver must not reconcile the in-memory B back to the persisted A."""
    from gateway.session import PersistedSessionRouteLookup

    h = _Harness(tmp_path, monkeypatch)
    persisted = {"model": "model-A", "provider": "openrouter", "api_mode": "chat_completions"}
    monkeypatch.setattr(h.runner, "_persisted_session_route_identity",
                        lambda _key: PersistedSessionRouteLookup("valid", persisted))
    await h.adapter.handle_message(_ev("/model model-B"))
    await _wait_for(lambda: h.sent_with(_switched("model-B")), "confirmation")
    await asyncio.wait_for(h.db.entered.wait(), 5)
    model, _runtime = h.runner._resolve_session_agent_runtime(source=_source())
    assert model == "model-B"
    h.db.release.set()
    await _drained(h)








def test_telegram_dm_commands_share_one_switch_lock(tmp_path, monkeypatch):
    """Prism da07a87006ed: a lobby-shaped and a threaded Telegram DM /model normalize to the same
    session, so they must take the same lock (the key is known only after topic recovery)."""
    h = _Harness(tmp_path, monkeypatch)
    lobby = _source()
    threaded = SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm", thread_id="42")
    assert h.runner._model_switch_lock_key(lobby) == h.runner._model_switch_lock_key(threaded)


@pytest.mark.asyncio
async def test_session_switch_does_not_discard_queued_global_intent(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    await h.adapter.handle_message(_ev("/model model-A --global"))
    await asyncio.wait_for(h.db.entered.wait(), 5)
    second = asyncio.create_task(h.adapter.handle_message(_ev("/model model-B")))
    await asyncio.sleep(0.05)
    assert not second.done()
    h.db.release.set()
    await second
    await _drained(h)
    assert yaml.safe_load(h.cfg_path.read_text())["model"]["default"] == "model-A"
    assert h.runner.session_store.get_model_override(h.key())["model"] == "model-B"


@pytest.mark.asyncio
async def test_failed_global_cleanup_keeps_new_identity_persisted(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    h.db.release.set()
    await h.adapter.handle_message(_ev("/model model-B"))
    await _drained(h)
    real_clear = h.runner.session_store.clear_model_route_override

    def fail_clear(key):
        raise OSError("clear failed")

    monkeypatch.setattr(h.runner.session_store, "clear_model_route_override", fail_clear)
    await h.adapter.handle_message(_ev("/model model-A --global"))
    await _drained(h)
    assert h.sent_with(FOLLOWUP)
    entry = h.runner.session_store.entry_for(h.key())
    assert entry.model_override_identity["model"] == "model-A"
    assert h.runner.session_store.get_model_override(h.key())["model"] == "model-A"
    monkeypatch.setattr(h.runner.session_store, "clear_model_route_override", real_clear)




@pytest.mark.asyncio
async def test_global_completion_order_after_cross_session_db_delay(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    first_entered, release = asyncio.Event(), asyncio.Event()

    async def update(session_id, model, **kw):
        if model == "model-A":
            first_entered.set()
            await release.wait()

    h.db.update_session_model = update
    await h.adapter.handle_message(_ev("/model model-A --global", "111"))
    await asyncio.wait_for(first_entered.wait(), 5)
    await h.adapter.handle_message(_ev("/model model-B --global", "222"))
    await _wait_for(lambda: yaml.safe_load(h.cfg_path.read_text())["model"]["default"] == "model-B", "newer global saved")
    release.set()
    await _drained(h)
    assert yaml.safe_load(h.cfg_path.read_text())["model"]["default"] == "model-A"


@pytest.mark.asyncio
async def test_cached_agent_billing_write_runs_after_confirmation(tmp_path, monkeypatch):
    from tests.agent.test_switch_model_pool_reload import _make_agent, _make_pool
    from run_agent import AIAgent
    from unittest.mock import Mock
    from types import MethodType

    h = _Harness(tmp_path, monkeypatch)
    h.db.release.set()
    resident = _make_agent("openrouter", "old-model", _make_pool("openrouter"))
    resident.switch_model = MethodType(AIAgent.switch_model, resident)
    resident.session_id = "resident-session"
    entered, release = threading.Event(), threading.Event()

    def billing(*args, **kwargs):
        entered.set()
        assert release.wait(10), "billing write never released"

    resident._session_db = Mock(update_session_billing_route=billing)
    h.runner._agent_cache_lock = threading.Lock()
    h.runner._agent_cache = {h.key(): (resident, time.monotonic())}
    try:
        await h.adapter.handle_message(_ev("/model model-A"))
        await _wait_for(lambda: h.sent_with(_switched("model-A")), "confirmation before resident billing I/O", 3)
        assert resident.model == "model-A"
        await _wait_for(entered.is_set, "deferred billing write", 3)
    finally:
        release.set()
    await _drained(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_phase", ["sqlite", "fallback"])
async def test_cached_agent_real_sqlite_has_no_write_before_confirmation(tmp_path, monkeypatch, blocked_phase):
    from hermes_state import SessionDB, AsyncSessionDB
    from tests.agent.test_switch_model_pool_reload import _make_agent, _make_pool
    from run_agent import AIAgent
    from types import MethodType

    # Capture the real constructor BEFORE the legacy harness disables SQLite.
    real_db = SessionDB
    h = _Harness(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_state.SessionDB", real_db)
    db = real_db(db_path=tmp_path / "state.db")
    h.runner.session_store._db = db
    h.runner._session_db = AsyncSessionDB(db)
    entry = h.runner.session_store.get_or_create_session(_source())
    resident = _make_agent("openrouter", "old-model", _make_pool("openrouter"))
    resident.switch_model = MethodType(AIAgent.switch_model, resident)
    resident.session_id = entry.session_id
    resident._session_db = db
    resident._provider_fallback_active = True
    h.runner._agent_cache_lock = threading.Lock()
    h.runner._agent_cache = {h.key(): (resident, time.monotonic())}
    entered, release = threading.Event(), threading.Event()
    writes = []
    execute = db._execute_write

    def gated_write(*args, **kwargs):
        writes.append(bool(h.sent_with(_switched("model-A"))))
        if blocked_phase == "sqlite":
            entered.set()
            assert release.wait(10), "SQLite write never released"
        return execute(*args, **kwargs)

    def close_fallback(root):
        assert root == entry.session_id
        if blocked_phase == "fallback":
            entered.set()
            assert release.wait(10), "fallback write never released"
        assert h.sent_with(_switched("model-A")), "sticky closure preceded confirmation"
        return 1

    monkeypatch.setattr(db, "_execute_write", gated_write)
    monkeypatch.setattr("agent.fallback_wiring.close_episodes_for_user_route", close_fallback)
    started = time.monotonic()
    try:
        await h.adapter.handle_message(_ev("/model model-A"))
        await _wait_for(lambda: h.sent_with(_switched("model-A")), "real-store confirmation", 3)
        reply_s = time.monotonic() - started
        assert resident.model == "model-A"
        assert h.runner._resolve_session_agent_runtime(source=_source())[0] == "model-A"
        await _wait_for(entered.is_set, "post-delivery durable phase", 3)
        assert all(writes), "SessionDB._execute_write ran before the confirmation"
        print(f"real_sqlite phase={blocked_phase} reply_s={reply_s:.3f}")
    finally:
        release.set()
        await _drained(h, 10)
        db.close()
    assert writes, "real SQLite mutation seam was not exercised"
    assert not h.sent_with(FOLLOWUP)




@pytest.mark.asyncio
async def test_thirty_second_blocked_write_reply_timing(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    started = time.monotonic()
    await h.adapter.handle_message(_ev("/model model-A"))
    await _wait_for(lambda: h.sent_with(_switched("model-A")), "confirmation", 5)
    reply_s = time.monotonic() - started
    await asyncio.wait_for(h.db.entered.wait(), 5)
    await asyncio.sleep(30)
    assert h.db.models == []
    h.db.release.set()
    await _drained(h)
    print(f"blocked_write_s=30 reply_s={reply_s:.3f} settled_s={time.monotonic() - started:.3f}")
    assert reply_s < 5

