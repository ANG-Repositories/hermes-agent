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
    await _wait_for(lambda: not h.runner._model_switch_write_pending(h.key())
                    and not any(not t.done() for t in list(h.adapter._session_tasks.values())),
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
    await asyncio.wait_for(h.adapter.handle_message(_ev("/model model-B")), 5)  # session busy: dispatched inline
    await _wait_for(lambda: h.sent_with(_switched("model-B")), "second confirmation while the first write is blocked")
    assert h.runner._session_model_override(h.key())["model"] == "model-B"
    h.db.release.set()
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
    assert sorted(order) == ["model-A", "model-B"]
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
    assert "disk full" in followup
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
    await h.adapter.handle_message(_ev("/stop"))  # real /stop path: cancel, keep pending, drain
    await _wait_for(lambda: h.turns, "queued message after /stop")
    assert h.turns == [("hello", "gpt-5.5")]
    # The interrupted write stays queued for the next drain instead of being dropped.
    assert len(h.runner.__dict__["_model_switch_pending"][h.key()]) == 1
    assert h.db.models == []


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
    await h.adapter.handle_message(_ev("/model model-A --global"))
    await _wait_for(lambda: h.sent_with(_switched("model-A")), "typed confirmation")
    await asyncio.wait_for(h.db.entered.wait(), 5)
    assert await h.runner._handle_model_command(_ev("/model --global")) is None
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


@pytest.mark.asyncio
async def test_superseded_global_switch_does_not_clear_a_newer_override(tmp_path, monkeypatch):
    """Prism 9ea56997a2df: a newer override installed while a deferred --global switch waits on its
    config write is not cleared from memory or the store by the older switch."""
    h = _Harness(tmp_path, monkeypatch)
    h.db.release.set()
    gate, entered = asyncio.Event(), asyncio.Event()
    from hermes_cli.model_switch import persist_model_selection

    async def _slow_write(result, config_path):
        entered.set()
        await gate.wait()
        await asyncio.to_thread(persist_model_selection, result, config_path)

    monkeypatch.setattr("gateway.slash_commands_model._persist_model_switch_to_config", _slow_write)
    await h.adapter.handle_message(_ev("/model model-A --global"))
    await asyncio.wait_for(entered.wait(), 5)
    newer = {"model": "model-B", "provider": "openrouter", "api_mode": "chat_completions"}
    await h.runner._persist_session_model_override(h.key(), newer)
    gate.set()
    await _drained(h)
    assert h.runner._session_model_override(h.key())["model"] == "model-B"


@pytest.mark.asyncio
async def test_cancelled_drain_does_not_replay_a_completed_config_write(tmp_path, monkeypatch):
    """Prism fce37d9b26ea: a drain cancelled after its config.yaml write resumes after that phase."""
    h = _Harness(tmp_path, monkeypatch)
    h.db.release.set()
    writes = []
    from hermes_cli.model_switch import persist_model_selection

    async def _write(result, config_path):
        await asyncio.to_thread(persist_model_selection, result, config_path)
        writes.append(result.new_model)

    store_gate = asyncio.Event()
    real_persist = h.runner._persist_session_model_override

    async def _held_persist(key, override, **kw):
        if kw.get("persist_only"):
            await store_gate.wait()
        return await real_persist(key, override, **kw)

    monkeypatch.setattr("gateway.slash_commands_model._persist_model_switch_to_config", _write)
    h.runner._persist_session_model_override = _held_persist
    await h.adapter.handle_message(_ev("/model model-A --global"))
    await _wait_for(lambda: writes == ["model-A"], "config write")
    await h.adapter.cancel_session_processing(h.key())  # /stop mid store phase
    assert h.runner._model_switch_write_pending(h.key())
    store_gate.set()
    await _drained(h)
    assert writes == ["model-A"], "the completed config.yaml write was replayed"


@pytest.mark.asyncio
async def test_writes_still_land_when_the_post_delivery_callback_is_skipped(tmp_path, monkeypatch):
    """Prism 49cf8836d4b4: if the adapter never fires the callback, the session task's end drains."""
    h = _Harness(tmp_path, monkeypatch)
    h.db.release.set()
    h.adapter._fire_post_delivery_callback = lambda *_a, **_k: asyncio.sleep(0)
    await h.adapter.handle_message(_ev("/model gpt-5.5"))
    await _wait_for(lambda: h.db.models == ["gpt-5.5"], "orphaned deferred write drained")
    await _drained(h)
    assert h.runner.session_store.get_model_override(h.key())["model"] == "gpt-5.5"


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
    await h.adapter.handle_message(_ev("/model model-B"))
    await _wait_for(lambda: h.sent_with(_switched("model-B")), "second confirmation")
    h.db.release.set()
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
async def test_config_failure_survives_cancelled_drain(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    h.db.release.set()
    entered, release = asyncio.Event(), asyncio.Event()
    real_persist = h.runner._persist_session_model_override

    async def fail_config(result, config_path):
        raise OSError("config disk full")

    async def held_persist(key, override, **kw):
        if kw.get("persist_only"):
            entered.set()
            await release.wait()
        return await real_persist(key, override, **kw)

    monkeypatch.setattr("gateway.slash_commands_model._persist_model_switch_to_config", fail_config)
    h.runner._persist_session_model_override = held_persist
    await h.adapter.handle_message(_ev("/model model-A --global"))
    await asyncio.wait_for(entered.wait(), 5)
    await h.adapter.cancel_session_processing(h.key())
    release.set()
    await _drained(h)
    assert any("config disk full" in line for line in h.sent_with(FOLLOWUP))
