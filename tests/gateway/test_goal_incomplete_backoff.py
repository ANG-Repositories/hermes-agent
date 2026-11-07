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
