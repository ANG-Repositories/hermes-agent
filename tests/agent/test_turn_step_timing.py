"""The turn tail names the step that held its seconds (t_238cc30d).

Large gateway sessions spent 5-25 s between the last ``API call #N`` line and ``Turn ended`` with
nothing logged. The finalizer records each step; a slow persist sub-step must show up by name in
the one ``turn tail timing`` line, and a fast tail must stay silent.
"""

import logging
import os
import time
from unittest.mock import patch

from agent import turn_step_timing
from agent.turn_finalizer import finalize_turn
from hermes_state import SessionDB


def _agent(db):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
                        quiet_mode=True, session_db=db, session_id="tail-timing", skip_context_files=True,
                        skip_memory=True)
    agent._ensure_db_session()
    return agent


def _finalize(agent, messages):
    return finalize_turn(
        agent, final_response="done", api_call_count=1, interrupted=False, failed=False, messages=messages,
        conversation_history=[], effective_task_id="t", turn_id="tail-timing:t:1", user_message="hi",
        original_user_message="hi", _should_review_memory=False,
        _turn_exit_reason="text_response(finish_reason=stop)",
    )


def _tail_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("turn tail timing:")]


def test_slow_persist_write_is_named_and_fast_tail_is_silent(tmp_path, caplog, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        agent = _agent(db)
        caplog.set_level(logging.INFO, logger="agent.conversation_loop")

        _finalize(agent, [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "done"}])
        assert _tail_lines(caplog) == []

        # The effective_last_active recompute runs inside the flush's one write txn (t_7bda73a8), so a slow
        # recompute is charged to the append_batch step that holds that txn.
        real = db._recompute_last_active_best_effort

        def slow_recompute(conn, session_id):
            time.sleep(turn_step_timing.SLOW_TAIL_S + 0.2)
            return real(conn, session_id)

        monkeypatch.setattr(db, "_recompute_last_active_best_effort", slow_recompute)
        caplog.clear()
        _finalize(agent, [{"role": "user", "content": "again"}, {"role": "assistant", "content": "done"}])

        lines = _tail_lines(caplog)
        assert len(lines) == 1 and "phase=pre_turn_ended" in lines[0]
        step = next(tok for tok in lines[0].split() if ".append_batch=" in tok)
        assert float(step.split("=")[1].rstrip("s")) >= turn_step_timing.SLOW_TAIL_S
        assert len(db.get_messages(agent.session_id)) == 4  # the timed write still landed
    finally:
        db.close()
