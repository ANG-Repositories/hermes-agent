"""A turn flush takes the state.db write lock ONCE (t_7bda73a8).

On a contended store every BEGIN IMMEDIATE is a separate wait behind other writers; the turn tail measured
15.6 s (append batch) + 9.6 s (a second txn for the effective_last_active recompute) under a 6 s-burst holder.
"""
import os
from unittest.mock import patch

from hermes_state import SessionDB


def _agent(db, session_id):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        return AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", model="test/model",
                       quiet_mode=True, session_db=db, session_id=session_id, platform="desktop",
                       skip_context_files=True, skip_memory=True)


def _begins_during(db, fn):
    stmts = []
    db._conn.set_trace_callback(stmts.append)
    try:
        fn()
    finally:
        db._conn.set_trace_callback(None)
    return sum(1 for s in stmts if s.strip().upper().startswith("BEGIN IMMEDIATE"))


def test_turn_flush_is_one_write_transaction_and_keeps_denorm(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        agent = _agent(db, "one-txn")
        messages = [{"role": "user", "content": "seed", "timestamp": 100.0}]
        agent._flush_messages_to_session_db(messages, [])
        # Stale the denorm so only the flush's recompute can repair it.
        db._execute_write(lambda c: c.execute("UPDATE sessions SET effective_last_active = -1 WHERE id = 'one-txn'"))
        db._bump_effective_last_active_for_message = lambda *a, **k: None  # isolate the batch-level recompute
        messages += [{"role": "user", "content": "q", "timestamp": 700.0},
                     {"role": "assistant", "content": "a", "timestamp": 800.0}]

        begins = _begins_during(db, lambda: agent._flush_messages_to_session_db(messages, []))

        assert begins == 1
        assert [m["content"] for m in db.get_messages("one-txn")] == ["seed", "q", "a"]
        stored = db._conn.execute("SELECT effective_last_active FROM sessions WHERE id = 'one-txn'").fetchone()[0]
        assert stored == db.expected_effective_last_active("one-txn") == 800.0
    finally:
        db.close()
