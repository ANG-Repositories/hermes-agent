"""The gateway /goal continuation re-fires only after a turn that changed something.

Ace ruling 2026-10-22 (option b, t_c4ac59ae): on a busy board every kanban wake / page / NO_REPLY
answer was followed by a continuation turn that re-read unchanged state (2-6 min gaps on a goal
armed with a 30-min check-in). A turn the user did not author whose tool calls were all reads (or
none) keeps the judge verdicts but queues no continuation; the idle check-in stays the next nudge.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_cli.goal_turn_effects import classify_turn_effect


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals

    goals._DB_CACHE.clear()
    goals._get_session_db()  # warm from a sync context (see test_goal_verdict_send.py)
    yield home
    goals._DB_CACHE.clear()


class _Adapter:
    def __init__(self) -> None:
        self._pending_messages: dict = {}
        self.sends: list = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sends.append(content)
        return SimpleNamespace(success=True, message_id="m")


def _runner(goals_cfg=None):
    from gateway.run import GatewayRunner

    src = SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="t", chat_type="dm")
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    runner._goals_config = lambda: dict(goals_cfg or {})
    runner.adapters, runner._running_agents, runner._running_agents_ts, runner._queued_events = {}, {}, {}, {}
    entry = SessionEntry(
        session_key=build_session_key(src), session_id=f"te-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(), updated_at=datetime.now(), platform=Platform.TELEGRAM, chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = build_session_key(src)
    adapter = _Adapter()
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner, adapter, entry, src


def _call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}},
    ]}


_WAKE = "[kanban] card t_1234 changed: review requested"
_READ_TURN = [_call("kanban_show", task_id="t_1234"), _call("terminal", command="gh pr view 12 --json state | jq .state")]
_WRITE_TURN = _READ_TURN + [_call("kanban_comment", task_id="t_1234", body="ack")]


def _event(text, messages, *, internal):
    ev = MessageEvent(text=text, message_type=MessageType.TEXT, source=None, internal=internal)
    ev._goal_turn_api_calls = len(messages) + 1
    ev._goal_turn_effect = classify_turn_effect(messages)  # what run_turn stashes
    return ev


async def _run_turn(runner, adapter, entry, src, event, wait=2.0):
    # The enqueue happens inline after the judge hop; ``wait`` only absorbs scheduler jitter.
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "more to do", False, None, False)):
        await runner._post_turn_goal_continuation(session_entry=entry, source=src, final_response="ok", event=event)
        deadline = time.monotonic() + wait
        while not adapter._pending_messages and time.monotonic() < deadline:
            await asyncio.sleep(0.01)


def _set_goal(sid):
    from hermes_cli.goals import GoalManager

    GoalManager(sid).set("land the queue")
    return GoalManager(sid)


@pytest.mark.asyncio
async def test_read_only_wakes_queue_no_continuation_and_leave_state(hermes_home):
    runner, adapter, entry, src = _runner()
    before = await asyncio.to_thread(lambda: _set_goal(entry.session_id).state)
    for _ in range(10):
        await _run_turn(runner, adapter, entry, src, _event(_WAKE, _READ_TURN, internal=True), wait=0.05)
        await _run_turn(runner, adapter, entry, src, _event(_WAKE, [], internal=True), wait=0.05)  # NO_REPLY
    assert not adapter._pending_messages
    from hermes_cli.goals import load_goal

    after = await asyncio.to_thread(load_goal, entry.session_id)
    assert after.status == "active"
    assert (after.turns_used, after.last_turn_at, after.toolless_turns) == (
        before.turns_used, before.last_turn_at, before.toolless_turns,
    )


@pytest.mark.asyncio
async def test_write_turn_queues_exactly_one_continuation(hermes_home):
    runner, adapter, entry, src = _runner()
    await asyncio.to_thread(_set_goal, entry.session_id)
    await _run_turn(runner, adapter, entry, src, _event(_WAKE, _READ_TURN, internal=True), wait=0.05)
    await _run_turn(runner, adapter, entry, src, _event(_WAKE, _WRITE_TURN, internal=True))
    queued = [e for evs in adapter._pending_messages.values() for e in (evs if isinstance(evs, list) else [evs])]
    assert len(queued) == 1 and queued[0].text.startswith("[Continuing toward your standing goal]")


@pytest.mark.asyncio
async def test_user_message_continues_even_when_read_only(hermes_home):
    runner, adapter, entry, src = _runner()
    await asyncio.to_thread(_set_goal, entry.session_id)
    await _run_turn(runner, adapter, entry, src, _event("how is it going?", [], internal=False))
    assert adapter._pending_messages


@pytest.mark.asyncio
async def test_kill_switch_restores_continuation_after_read_only_turn(hermes_home):
    runner, adapter, entry, src = _runner({"nudge_on_read_only_turns": True})
    await asyncio.to_thread(_set_goal, entry.session_id)
    await _run_turn(runner, adapter, entry, src, _event(_WAKE, _READ_TURN, internal=True))
    assert adapter._pending_messages


def test_idle_checkin_still_fires_after_held_turns(hermes_home):
    mgr = _set_goal("idle-after-held")
    t0 = time.time()
    mgr.state.last_turn_at = t0
    mgr._save()
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "k", False, None, False)):
        for _ in range(10):
            d = mgr.evaluate_after_turn("ack", real_event=True, turn_effect="read")
            assert not d["should_continue"] and d["status"] == "active"
    prompt = mgr.idle_wake_prompt(t0 + 30 * 60 + 1, checkin_minutes=30)
    assert prompt and "Goal check-in 1/3" in prompt


def test_judge_can_still_end_the_goal_on_a_read_only_turn(hermes_home):
    mgr = _set_goal("done-on-read")
    with patch("hermes_cli.goals.judge_goal", return_value=("done", "merged", False, None, False)):
        d = mgr.evaluate_after_turn("PR merged", turn_effect="read")
    assert d["status"] == "done"


def _queued(adapter):
    return [e for evs in adapter._pending_messages.values() for e in (evs if isinstance(evs, list) else [evs])]


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_cfg, per_goal_checkin", [
    ({"checkin_minutes": 0}, None),   # config: idle check-ins off
    ({}, 0),                          # per-goal /goal checkin 0
])
async def test_held_turn_is_never_the_last_word(hermes_home, goal_cfg, per_goal_checkin):
    """Regression for Prism 527a33ddb4e7: with no idle check-in to follow, a read-only turn (the loop's
    own continuation, or a wake) must still queue a continuation instead of stopping silently."""
    runner, adapter, entry, src = _runner(goal_cfg)

    def _arm():
        mgr = _set_goal(entry.session_id)
        if per_goal_checkin is not None:
            mgr.set_checkin_minutes(per_goal_checkin)
        return mgr.next_continuation_prompt()

    continuation = await asyncio.to_thread(_arm)
    await _run_turn(runner, adapter, entry, src, _event(continuation, _READ_TURN, internal=True))
    assert len(_queued(adapter)) == 1
    adapter._pending_messages.clear()
    await _run_turn(runner, adapter, entry, src, _event(_WAKE, _READ_TURN, internal=True))
    assert len(_queued(adapter)) == 1


def test_spent_checkin_budget_does_not_hold(hermes_home):
    """No real event re-arms the check-ins and the budget is used up: holding would be a silent stop."""
    mgr = _set_goal("spent-budget")
    mgr.state.idle_wakes = 3
    mgr._save()
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "k", False, None, False)):
        d = mgr.evaluate_after_turn("ack", turn_effect="read", checkin_minutes=30, max_checkins=3)
        assert d["should_continue"] and d["hold_overridden"]
        mgr.state.idle_wakes = 0  # control: a check-in can still follow -> held
        d = mgr.evaluate_after_turn("ack", turn_effect="read", checkin_minutes=30, max_checkins=3)
        assert not d["should_continue"] and d["held"] == "read"


@pytest.mark.asyncio
async def test_loop_own_read_only_continuation_keeps_going_with_checkins_on(hermes_home):
    """The hold targets wakes/pages, not the loop's heartbeat: a read-only continuation turn continues."""
    runner, adapter, entry, src = _runner({"checkin_minutes": 30})
    continuation = await asyncio.to_thread(lambda: _set_goal(entry.session_id).next_continuation_prompt())
    await _run_turn(runner, adapter, entry, src, _event(continuation, _READ_TURN, internal=True))
    assert len(_queued(adapter)) == 1


@pytest.mark.asyncio
async def test_compaction_during_turn_is_unknown_and_not_held(hermes_home):
    """Regression for Prism 3c0c66874847: after mid-turn compaction the last user row (a folded todo
    snapshot) is not the turn start; a write before it must not be lost to a read-only slice."""
    from hermes_cli.goal_turn_effects import turn_effect_for_result

    messages = [
        {"role": "user", "content": _WAKE},
        _call("kanban_comment", task_id="t_1234", body="ack"),
        {"role": "user", "content": "[todo snapshot]"},
        _call("kanban_show", task_id="t_1234"),
    ]
    result = {"messages": messages, "history_offset": 0, "compacted_in_place": True, "compacted_during_turn": True}
    assert turn_effect_for_result(result, messages) is None
    assert turn_effect_for_result({**result, "compacted_during_turn": False}, messages) == "read"

    runner, adapter, entry, src = _runner({"checkin_minutes": 30})
    await asyncio.to_thread(_set_goal, entry.session_id)
    ev = _event(_WAKE, messages, internal=True)
    ev._goal_turn_effect = turn_effect_for_result(result, messages)
    await _run_turn(runner, adapter, entry, src, ev)
    assert len(_queued(adapter)) == 1


@pytest.mark.parametrize("messages, effect", [
    ([], "none"),
    ([_call("read_file", path="x"), _call("terminal", command="git status && git log -3 | cat")], "read"),
    ([_call("terminal", command="gh api repos/o/r/pulls/1 --jq .state")], "read"),
    ([_call("terminal", command="gh api -X PUT repos/o/r/pulls/1/update-branch")], "write"),
    ([_call("terminal", command="git commit -m x && git push")], "write"),
    ([_call("terminal", command="cat a > b")], "write"),
    ([_call("cronjob", action="list")], "read"),
    ([_call("cronjob", action="create")], "write"),
    ([_call("write_file", path="x", content="y")], "write"),
    ([_call("some_plugin_tool")], "write"),  # unknown: never hold a turn that might have acted
])
def test_turn_effect_classifier(messages, effect):
    assert classify_turn_effect(messages) == effect


# ---------------------------------------------------------------------------
# Failed turns must not drive the goal loop (2026-11-07 Agora incident): fresh-agent construction
# raised ImportError on every turn, the judge read the generic error notice as progress, and a
# synthetic continuation was queued every ~10 s.
# ---------------------------------------------------------------------------


def _real_adapter():
    """A real BasePlatformAdapter (pending-message FIFO + drain) that records sends."""
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    class _A(BasePlatformAdapter):
        def __init__(self):
            super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
            self.sent = []

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            return True

        async def disconnect(self) -> None:
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(str(content))
            return SendResult(success=True, message_id="m1")

        async def edit_message(self, chat_id, message_id, content, finalize=False):
            return SendResult(success=True, message_id=message_id)

        async def send_typing(self, chat_id, metadata=None) -> None:
            return None

        async def stop_typing(self, chat_id) -> None:
            return None

        async def get_chat_info(self, chat_id: str):
            return {"id": chat_id}

    return _A()


def _failing_rig(monkeypatch, hermes_home, session_id):
    """Real GatewayRunner._handle_message + real adapter + real post-turn goal hooks; only
    ``_run_agent`` is replaced, by the ImportError agent construction raised in the incident."""
    import sys
    import types

    from gateway import run as gateway_run

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *a, **k: 100_000)
    runner = gateway_run.GatewayRunner(GatewayConfig())
    adapter = _real_adapter()
    src = SessionSource(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm", user_id="u1")
    key = build_session_key(src)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._delivery_adapter_for = lambda source: adapter
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._session_db = MagicMock()
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_a, **_kw: False
    runner.hooks = MagicMock()
    runner.hooks.emit = MagicMock(side_effect=lambda *a, **k: asyncio.sleep(0))
    entry = SessionEntry(session_key=key, session_id=session_id, created_at=datetime.now(),
                         updated_at=datetime.now(), platform=Platform.TELEGRAM, chat_type="dm")
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store._generate_session_key.return_value = key
    runner.session_store.peek_session_id.return_value = session_id
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.has_input_owner.return_value = True
    runner.session_store.transcript_tail_role.return_value = "user"
    runner.session_store.clear_turn_active = lambda *_a, **_k: True
    calls = []
    state = {"broken": True}

    async def fake_run_agent(message, **kwargs):
        calls.append(message)
        runner._release_running_agent_state(kwargs["session_key"], run_generation=kwargs["run_generation"])
        if state["broken"]:
            raise ImportError("The 'anthropic' package is required for the Anthropic provider: venv not installed")
        return {"final_response": f"answer {len(calls)}", "messages": [], "api_calls": 2,
                "history_offset": 0, "last_prompt_tokens": 0}

    runner._run_agent = fake_run_agent
    adapter.set_message_handler(runner._handle_message)
    return runner, adapter, src, calls, state


async def _settle(adapter, calls, expect, quiet=1.5, timeout=20.0):
    """Wait for ``expect`` turns, then for the session to stay idle ``quiet`` s (a queued
    continuation would start another turn inside that window); cancel whatever still runs."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(calls) < expect and loop.time() < deadline:
        await asyncio.sleep(0.02)
    idle_since = None
    while loop.time() < deadline:
        busy = any(not t.done() for t in list(adapter._session_tasks.values())) or adapter._pending_messages
        if busy:
            idle_since = None
        elif idle_since is None:
            idle_since = loop.time()
        elif loop.time() - idle_since >= quiet:
            break
        await asyncio.sleep(0.02)
    for task in list(adapter._session_tasks.values()):
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_failed_turn_pauses_goal_instead_of_continuation_storm(monkeypatch, hermes_home):
    from hermes_cli.goals import GoalManager, load_goal

    sid = f"fail-{uuid.uuid4().hex[:8]}"
    runner, adapter, src, calls, state = _failing_rig(monkeypatch, hermes_home, sid)
    await asyncio.to_thread(lambda: GoalManager(sid).set("ship the fix"))
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "more to do", False, None, False)):
        await adapter.handle_message(MessageEvent(text="go", message_type=MessageType.TEXT, source=src,
                                                  message_id="go"))
        await _settle(adapter, calls, 1)
    assert len(calls) == 1, f"failed turn re-queued {len(calls) - 1} synthetic continuations"
    goal = await asyncio.to_thread(load_goal, sid)
    assert goal.status == "paused" and "ImportError" in (goal.paused_reason or "")
    # Error visibility kept: the user saw the failure notice and the pause notice.
    assert any("went wrong" in s for s in adapter.sent), adapter.sent
    assert any("Goal paused" in s and "/goal resume" in s for s in adapter.sent), adapter.sent
    # Durable: a fresh manager (a reload / restart) still reads paused.
    assert await asyncio.to_thread(lambda: GoalManager(sid).is_active()) is False

    # Repair, then the user resumes: the loop works again (normal continuation is queued).
    state["broken"] = False
    await asyncio.to_thread(lambda: GoalManager(sid).resume())
    with patch("hermes_cli.goals.judge_goal", return_value=("done", "fixed", False, None, False)):
        await adapter.handle_message(MessageEvent(text="retry", message_type=MessageType.TEXT, source=src,
                                                  message_id="retry"))
        await _settle(adapter, calls, 2)
    assert calls[-1] == "retry"
    assert (await asyncio.to_thread(load_goal, sid)).status == "done"


# Prism 89a770df5c2c: the failed-turn pause must not clobber a goal changed since the turn ended or
# while the pause is written (the pause used a plain overwrite from a possibly stale manager).
def _failed_event():
    ev = MessageEvent(text="go", message_type=MessageType.TEXT, source=None, internal=False)
    ev._goal_turn_failed = "ImportError"
    return ev


def _concurrent(action, sid):
    from hermes_cli.goals import GoalManager

    other = GoalManager(sid)
    if action == "replace":
        other.set("a different goal")
    elif action == "clear":
        other.clear()
    elif action == "user_pause":
        other.pause("user-paused")
    else:  # nonterminal edit of the same active goal
        other.add_subgoal("also check CI")


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [True, False])  # False: no turn-end pin, baseline omitted (Prism 8de03d473b72)
@pytest.mark.parametrize("after_read", [1, 2])  # 1: after the manager loaded; 2: between pause read and write
@pytest.mark.parametrize("action, status, goal, paused_reason, subgoals", [
    ("replace", "active", "a different goal", None, 0),
    ("clear", "cleared", "land the queue", None, 0),
    ("user_pause", "paused", "land the queue", "user-paused", 0),
    ("edit", "paused", "land the queue", "turn failed (ImportError)", 1),
])
async def test_failed_turn_pause_loses_to_concurrent_goal_write(hermes_home, action, status, goal,
                                                                  paused_reason, subgoals, after_read, pinned):
    from hermes_cli import goals as g

    runner, adapter, entry, src = _runner()
    sid = entry.session_id
    await asyncio.to_thread(_set_goal, sid)
    pin = ((await asyncio.to_thread(lambda: g._load_goal_raw(sid, strict=True)))[1],)
    real_load, reads, fired = g._load_goal_raw, [], []

    def racing_load(*a, **k):  # the concurrent write lands right after the Nth goal read
        out = real_load(*a, **k)
        reads.append(1)
        if len(reads) == after_read and not fired:
            fired.append(1)
            _concurrent(action, sid)
        return out

    with patch.object(g, "_load_goal_raw", racing_load):
        await runner._post_turn_goal_continuation(
            session_entry=entry, source=src, final_response="error", event=_failed_event(),
            goal_pin=pin if pinned else None)
    assert fired
    after = await asyncio.to_thread(g.load_goal, sid)
    assert (after.status, after.goal, len(after.subgoals)) == (status, goal, subgoals)
    assert (paused_reason in (after.paused_reason or "")) if paused_reason else not after.paused_reason
    assert not adapter._pending_messages


@pytest.mark.asyncio
async def test_failed_turn_does_not_pause_a_goal_set_after_the_turn_ended(hermes_home):
    """The turn-end pin is the old goal; a replacement set during the send was never run by the
    failed turn and must stay active (no pause notice either)."""
    from hermes_cli import goals as g

    runner, adapter, entry, src = _runner()
    sid = entry.session_id
    await asyncio.to_thread(_set_goal, sid)
    pin = ((await asyncio.to_thread(lambda: g._load_goal_raw(sid, strict=True)))[1],)
    await asyncio.to_thread(lambda: g.GoalManager(sid).set("set during the send"))
    await runner._post_turn_goal_continuation(
        session_entry=entry, source=src, final_response="error", event=_failed_event(), goal_pin=pin)
    after = await asyncio.to_thread(g.load_goal, sid)
    assert (after.status, after.goal) == ("active", "set during the send")
    await asyncio.sleep(0.05)
    assert not any("Goal paused" in str(s) for s in adapter.sends)
