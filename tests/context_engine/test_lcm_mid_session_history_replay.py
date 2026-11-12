"""A rebuilt agent's per-turn history must not be re-appended (t_73572f17).

Live shape (root lcm.db, Nov 2026): every time the gateway rebuilds the agent
for a long session, the fresh LCM engine binds with an unknown cursor and the
first post_llm_call ingest hands it the host's whole in-memory history. That
history is a trailing slice of the store: it starts mid-session (no system row,
no compaction scaffold) and is shorter than the stored tail. The cursor
reconcile found the exact stored-suffix match, but no acceptance rule covered
this shape, so it fell through to "persisted ambiguous delta" and appended all
of it again under fresh timestamps (one session: 552,644 rows, 10,848 distinct
contents; 20261108_093540_fa4b70b9 re-appended 347/355/376 rows on three
consecutive turns on 2026-11-12).
"""
from __future__ import annotations

import sqlite3

from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine

SESSION = "mid-session-replay"


def _cfg(tmp_path):
    return LCMConfig(database_path=str(tmp_path / "lcm.db"))


def _turn(i, call_prefix="call"):
    cid = f"{call_prefix}_{i:04d}_9f3a"
    return [
        {"role": "user", "content": f"[kanban] card t_{i:04d} handed off; check it"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": cid, "type": "function",
            "function": {"name": "terminal", "arguments": f'{{"command": "show {i}"}}'},
        }]},
        {"role": "tool", "tool_call_id": cid, "content": f'{{"output": "card {i} is in review"}}'},
        {"role": "assistant", "content": f"Card {i} is in review; nothing to do."},
    ]


def _stored_session(tmp_path, n_turns=60):
    rows = [{"role": "system", "content": "You are Apollo."}]
    for i in range(n_turns):
        rows.extend(_turn(i))
    eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
    eng.on_session_start(SESSION, platform="discord")
    eng._store.append_batch(SESSION, [dict(r) for r in rows])
    return rows


def _rebuilt_agent_ingest(tmp_path, history):
    fresh = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
    fresh.on_session_start(SESSION, platform="discord")
    assert fresh._ingest_cursor_needs_reconcile
    fresh.ingest([dict(m) for m in history])
    return fresh


def _rows(tmp_path):
    return sqlite3.connect(str(tmp_path / "lcm.db")).execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=?", (SESSION,)
    ).fetchone()[0]


def _count(tmp_path, content):
    return sqlite3.connect(str(tmp_path / "lcm.db")).execute(
        "SELECT COUNT(*) FROM messages WHERE session_id=? AND content=?", (SESSION, content)
    ).fetchone()[0]


class TestMidSessionHistoryReplay:
    def test_trailing_history_plus_new_turn_appends_only_the_new_turn(self, tmp_path):
        stored = _stored_session(tmp_path)
        n0 = _rows(tmp_path)
        # Host history starts mid-turn (an assistant row), carries no system
        # row, and is shorter than the stored tail.
        new_user = {"role": "user", "content": "[kanban] a genuinely new wake"}
        history = stored[-83:] + [new_user]
        assert history[0]["role"] == "assistant"
        fresh = _rebuilt_agent_ingest(tmp_path, history)
        assert _rows(tmp_path) == n0 + 1
        assert _count(tmp_path, new_user["content"]) == 1
        assert fresh._last_ingest_reconciliation["action"] == "advanced cursor"

    def test_trailing_history_replayed_twice_appends_nothing(self, tmp_path):
        stored = _stored_session(tmp_path)
        n0 = _rows(tmp_path)
        _rebuilt_agent_ingest(tmp_path, stored[-83:])
        _rebuilt_agent_ingest(tmp_path, stored[-83:])
        assert _rows(tmp_path) == n0

    def test_new_turn_repeating_stored_content_with_fresh_call_ids_is_appended(self, tmp_path):
        # No-loss control: a new turn that repeats the last turns' text but
        # carries its own tool-call ids is new, so it must be stored.
        stored = _stored_session(tmp_path)
        n0 = _rows(tmp_path)
        repeat = _turn(58, call_prefix="toolu") + _turn(59, call_prefix="toolu")
        _rebuilt_agent_ingest(tmp_path, repeat)
        assert _rows(tmp_path) == n0 + len(repeat)

    def test_single_tool_result_delta_repeating_the_tail_is_kept(self, tmp_path):
        # Dup-over-loss stays the rule below two distinct tool-call ids.
        stored = _stored_session(tmp_path)
        n0 = _rows(tmp_path)
        _rebuilt_agent_ingest(tmp_path, stored[-2:])
        assert _rows(tmp_path) > n0

    def test_trailing_history_with_unrecoverable_spill_marker_appends_nothing(self, tmp_path):
        # Live Nov-7 shape: the slice carries a <persisted-output> marker whose
        # spill file is gone (or outside the recoverable temp dir).
        rows = [{"role": "system", "content": "You are Apollo."}]
        for i in range(30):
            rows.extend(_turn(i))
        marker = (
            "<persisted-output>\nThis tool result was too large (102,575 characters, 100.2 KB).\n"
            f"Full output saved to: {tmp_path}/spillover/call_0015_9f3a.txt\n"
            "Use the read_file tool with offset and limit to access specific sections of this output.\n"
            "</persisted-output>"
        )
        tool_row = next(r for r in rows if r.get("tool_call_id") == "call_0015_9f3a")
        tool_row["content"] = marker
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in rows])
        n0 = _rows(tmp_path)
        _rebuilt_agent_ingest(tmp_path, rows[-70:])
        assert _rows(tmp_path) == n0

    def test_history_with_host_rewritten_user_rows_appends_only_new_rows(self, tmp_path):
        # Live shape 2 (fa4b70b9 at 01:00:07, 305 rows): the host re-renders an
        # older user row (timestamp prefix added, or content emptied), so the
        # slice is no exact suffix of the store. The stored tool results still
        # prove which rows were already ingested.
        stored = _stored_session(tmp_path)
        n0 = _rows(tmp_path)
        history = [dict(m) for m in stored[-83:]]
        rewritten = [i for i, m in enumerate(history) if m["role"] == "user"][:3]
        history[rewritten[0]]["content"] = "[Wed 2026-11-11 23:35:33 PST] " + history[rewritten[0]]["content"]
        history[rewritten[1]]["content"] = ""
        new_rows = [
            {"role": "user", "content": "[kanban] a genuinely new wake"},
            {"role": "assistant", "content": "On it."},
        ]
        _rebuilt_agent_ingest(tmp_path, history + new_rows)
        assert _rows(tmp_path) == n0 + 2
        assert _count(tmp_path, "[kanban] a genuinely new wake") == 1

    def test_reused_call_ids_with_new_results_are_appended(self, tmp_path):
        # A provider that numbers calls per turn (call_0, call_1) reuses ids;
        # a new result under an old id is new content and must be stored.
        rows = [{"role": "system", "content": "You are Apollo."}]
        for i in range(10):
            for j in range(2):
                cid = f"call_{j}"
                rows += [
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": cid, "type": "function",
                        "function": {"name": "terminal", "arguments": f'{{"n": {i}}}'},
                    }]},
                    {"role": "tool", "tool_call_id": cid, "content": f"turn {i} call {j}"},
                ]
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in rows])
        n0 = _rows(tmp_path)
        new_turn = []
        for j in range(2):
            cid = f"call_{j}"
            new_turn += [
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": cid, "type": "function",
                    "function": {"name": "terminal", "arguments": '{"n": 99}'},
                }]},
                {"role": "tool", "tool_call_id": cid, "content": f"turn 99 call {j}"},
            ]
        _rebuilt_agent_ingest(tmp_path, new_turn)
        assert _rows(tmp_path) == n0 + len(new_turn)
