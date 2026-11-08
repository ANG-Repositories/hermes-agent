"""Incomplete gateway turns must not spin the standing-goal loop."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.run_goals import GatewayGoalsMixin
from hermes_cli import goals


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["dict", "text", "streamed"])
async def test_incomplete_turns_park_backoff_reset_and_notify_once(tmp_path, monkeypatch, delivery):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayGoalsMixin()
    runner.config = {"goals": {"max_incomplete_turns": 3}}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner.async_session_store = SimpleNamespace(get_or_create_session=AsyncMock(
        return_value=SimpleNamespace(session_id="incomplete")))
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    runner._delivery_adapter_for = lambda source: None
    runner._session_key_for_source = lambda source: "test"
    event = SimpleNamespace(text="[Continuing toward your standing goal]\nGoal: ship it", internal=True)
    source = SimpleNamespace(chat_id="origin")
    mgr = await asyncio.to_thread(goals.GoalManager, "incomplete")
    await asyncio.to_thread(mgr.set, "ship it")
    clock = mgr.state.created_at

    async def turn(completed=False, text="This turn did not complete"):
        event._goal_turn_completed = completed
        event._streamed_final_response = text if delivery == "streamed" else ""
        result = {"completed": completed, "final_response": text} if delivery == "dict" else (
            text if delivery == "text" else None)
        await runner._run_post_turn_hooks(
            agent_result=result,
            source=source, is_internal=event.internal, event=event, parts=("goal continuation",))
        return await asyncio.to_thread(goals.GoalManager, "incomplete")

    with patch("hermes_cli.goals.time.time", side_effect=lambda: clock), patch(
        "hermes_cli.goals.judge_goal", return_value=("continue", "next", False, None, False)
    ) as judge:
        for _ in range(2):
            mgr = await turn()
            assert not mgr.state.waiting_until
        for delay in (60, 120, 240, 480, 960, 1800, 1800):
            mgr = await turn(text="")  # blank failures also settle the goal
            assert mgr.state.waiting_until == clock + delay
            assert mgr.idle_wake_prompt(clock + delay - 1) is None
            assert mgr.idle_wake_prompt(clock + delay)
            clock += delay
        assert judge.call_count == 0
        assert mgr.state.turns_used == 0
        assert runner._defer_goal_status_notice_after_delivery.await_count == 1
        assert runner._defer_goal_status_notice_after_delivery.call_args.args[0] is source
        mgr = await turn(completed=True, text="made progress")
        assert mgr.state.consecutive_incomplete_turns == 0
        assert judge.call_count == 1
        runner._defer_goal_status_notice_after_delivery.reset_mock()  # exclude normal judge notice
        for _ in range(3):
            mgr = await turn()
        assert runner._defer_goal_status_notice_after_delivery.await_count == 1
        # A human message resets the prior failure streak even if its own turn fails.
        event.text, event.internal = "try again", False
        mgr = await turn()
        assert mgr.state.consecutive_incomplete_turns == 1
        assert not mgr.state.waiting_until
        assert not mgr.idle_wake_prompt(clock)  # no immediate recovery-loop wake
        mgr = await turn(completed=True, text="")
        assert mgr.state.consecutive_incomplete_turns == 0
        assert judge.call_count == 1  # blank success only resets, never judges


@pytest.mark.parametrize("claimed", [False, True])
def test_blank_success_preserves_retry_recovery(tmp_path, monkeypatch, claimed):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mgr = goals.GoalManager("blank-retry")
    mgr.set("ship it")
    clock = mgr.state.created_at
    monkeypatch.setattr(goals.time, "time", lambda: clock)
    mgr.evaluate_after_turn("", turn_completed=False, max_incomplete_turns=1)
    deadline = mgr.state.waiting_until
    if claimed:
        clock = deadline
        assert mgr.idle_wake_prompt(checkin_minutes=0)
    with patch.object(goals, "judge_goal") as judge:
        decision = mgr.evaluate_after_turn("", turn_completed=True)
    judge.assert_not_called()
    assert not decision["should_continue"]
    mgr = goals.GoalManager("blank-retry")
    assert mgr.state.consecutive_incomplete_turns == 0
    assert mgr.state.incomplete_retry_wait
    for _ in range(2):
        clock = mgr.state.waiting_until
        assert mgr.idle_wake_prompt(checkin_minutes=0)
        mgr.evaluate_after_turn("", turn_completed=True)
        mgr = goals.GoalManager("blank-retry")
        assert mgr.state.incomplete_retry_wait and mgr.state.waiting_until == clock + 60
    assert mgr.idle_wake_prompt(mgr.state.waiting_until - 1, checkin_minutes=0) is None
    assert mgr.idle_wake_prompt(mgr.state.waiting_until, checkin_minutes=0)


@pytest.mark.asyncio
async def test_delivery_preserves_completion_before_returning_text():
    from gateway.run_turn import GatewayTurnMixin

    event = SimpleNamespace()
    with patch("gateway.run_turn.diagnostic_wake_muted", return_value=True):
        for completed in (False, True):
            await GatewayTurnMixin()._hmwa_deliver_turn_response(
                event, None, None, "test", 1, {"completed": completed}, [], "", "", False)
            assert event._goal_turn_completed is completed


@pytest.mark.asyncio
async def test_incomplete_limit_is_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mgr = await asyncio.to_thread(goals.GoalManager, "configured")
    await asyncio.to_thread(mgr.set, "ship it")
    decision = await asyncio.to_thread(
        mgr.evaluate_after_turn, "", turn_completed=False, max_incomplete_turns=1)
    assert not decision["should_continue"]
    assert mgr.state.waiting_until > 0
    assert decision["message"]


def test_failed_real_event_rearms_idle_checkins(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mgr = goals.GoalManager("rearm")
    mgr.set("ship it")
    clock = mgr.state.created_at
    monkeypatch.setattr(goals.time, "time", lambda: clock)
    mgr.state.idle_wakes = 3
    mgr._save()
    mgr.evaluate_after_turn("failed user turn", real_event=True, turn_completed=False)
    mgr = goals.GoalManager("rearm")
    assert mgr.state.idle_wakes == 0
    with patch.object(goals, "judge_goal", return_value=("continue", "more", False, None, False)):
        mgr.evaluate_after_turn("recovered", real_event=False)
    clock += 1801
    assert mgr.idle_wake_prompt(clock, checkin_minutes=30, max_checkins=3)


def test_loop_observation_does_not_consume_due_retry(tmp_path, monkeypatch):
    from hermes_cli.loops import goal_blocks_loop_tick

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mgr = goals.GoalManager("loop-retry")
    mgr.set("ship it")
    clock = mgr.state.created_at
    monkeypatch.setattr(goals.time, "time", lambda: clock)
    mgr.evaluate_after_turn("", turn_completed=False, max_incomplete_turns=1)
    clock = mgr.state.waiting_until
    assert goal_blocks_loop_tick("loop-retry")
    mgr = goals.GoalManager("loop-retry")
    assert mgr.idle_wake_prompt(clock, checkin_minutes=0)
    assert not mgr.idle_wake_prompt(clock + 1, checkin_minutes=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("global_minutes,goal_minutes", [(0, -1), (30, 0)])
async def test_retry_wakes_with_checkins_disabled(tmp_path, monkeypatch, global_minutes, goal_minutes):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayGoalsMixin()
    runner.config = {"goals": {"checkin_minutes": global_minutes}}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._warm_goals_session_db = AsyncMock()
    entry = SimpleNamespace(session_id="retry-off", origin=object(), suspended=False)
    runner.session_store = SimpleNamespace(list_sessions=lambda: [entry])
    prompts = []

    async def wake(entry, minutes, max_checkins):
        mgr = await asyncio.to_thread(goals.GoalManager, entry.session_id)
        prompts.append(await asyncio.to_thread(
            mgr.idle_wake_prompt, deadline, checkin_minutes=minutes, max_checkins=max_checkins))

    runner._goal_idle_wake_one = wake
    mgr = await asyncio.to_thread(goals.GoalManager, entry.session_id)
    await asyncio.to_thread(mgr.set, "ship it")
    await asyncio.to_thread(mgr.set_checkin_minutes, goal_minutes)
    await asyncio.to_thread(mgr.evaluate_after_turn, "", turn_completed=False, max_incomplete_turns=1)
    deadline = mgr.state.waiting_until
    assert mgr.idle_wake_prompt(deadline - 1, checkin_minutes=global_minutes) is None
    await runner._goal_idle_wake_scan()
    assert prompts and prompts[0]
    mgr = await asyncio.to_thread(goals.GoalManager, entry.session_id)
    assert mgr.idle_wake_prompt(deadline + 3600, checkin_minutes=global_minutes) is None
    await asyncio.to_thread(mgr.wait_for_seconds, 1, "deployment")
    assert mgr.idle_wake_prompt(mgr.state.waiting_until, checkin_minutes=global_minutes) is None


@pytest.mark.parametrize("reset", ["completed", "human"])
@pytest.mark.parametrize("barrier", ["timer", "session", "pid"])
def test_reset_preserves_unrelated_wait(tmp_path, monkeypatch, reset, barrier):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    mgr = goals.GoalManager("wait-owner")
    mgr.set("ship it")
    mgr.evaluate_after_turn("", turn_completed=False, max_incomplete_turns=1)
    if barrier == "timer":
        mgr.wait_for_seconds(3600, "deployment")
    elif barrier == "session":
        monkeypatch.setattr(goals, "_session_waiting", lambda sid: True)
        mgr.wait_on_session("deployment", "deployment")
    else:
        monkeypatch.setattr(goals, "_pid_alive", lambda pid: True)
        mgr.wait_on(12345, "deployment")
    before = (mgr.state.waiting_until, mgr.state.waiting_on_pid, mgr.state.waiting_on_session,
              mgr.state.waiting_reason, mgr.state.waiting_since)
    with patch.object(goals, "judge_goal", return_value=("continue", "more", False, None, False)) as judge:
        decision = mgr.evaluate_after_turn(
            "made progress", turn_completed=reset == "completed", human_message=reset == "human")
    assert (mgr.state.waiting_until, mgr.state.waiting_on_pid, mgr.state.waiting_on_session,
            mgr.state.waiting_reason, mgr.state.waiting_since) == before
    assert not decision["should_continue"]
    judge.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["dict", "text", "streamed"])
@pytest.mark.parametrize("failed,interrupted", [(False, False), (True, False), (False, True)])
async def test_iteration_handoff_is_counted_not_retried(tmp_path, monkeypatch, delivery, failed, interrupted):
    from gateway.run_turn import GatewayTurnMixin

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = GatewayGoalsMixin()
    runner.config = {}
    runner._run_in_executor_with_context = asyncio.to_thread
    runner.async_session_store = SimpleNamespace(get_or_create_session=AsyncMock(
        return_value=SimpleNamespace(session_id="handoff")))
    runner._defer_goal_status_notice_after_delivery = AsyncMock()
    runner._delivery_adapter_for = lambda source: None
    runner._session_key_for_source = lambda source: "test"
    event = SimpleNamespace(text="[Continuing toward your standing goal]\nGoal: ship it", internal=True)
    result = {"completed": False, "failed": failed, "interrupted": interrupted,
              "turn_exit_reason": "max_iterations_reached(1/1)", "final_response": "saved implementation"}
    with patch("gateway.run_turn.diagnostic_wake_muted", return_value=True):
        await GatewayTurnMixin()._hmwa_deliver_turn_response(
            event, None, None, "test", 1, result, [], "", "", False)
    event._streamed_final_response = result["final_response"] if delivery == "streamed" else ""
    mgr = await asyncio.to_thread(goals.GoalManager, "handoff")
    await asyncio.to_thread(mgr.set, "ship it", max_turns=1)
    with patch.object(goals, "judge_goal", return_value=("continue", "more", False, None, False)) as judge:
        await runner._run_post_turn_hooks(
            agent_result=result if delivery == "dict" else (result["final_response"] if delivery == "text" else None),
            source=SimpleNamespace(chat_id="origin"), is_internal=True, event=event, parts=("goal continuation",))
    mgr = await asyncio.to_thread(goals.GoalManager, "handoff")
    productive = not failed and not interrupted
    assert mgr.state.turns_used == int(productive)
    assert judge.call_count == int(productive)
    assert mgr.state.status == ("paused" if productive else "active")
