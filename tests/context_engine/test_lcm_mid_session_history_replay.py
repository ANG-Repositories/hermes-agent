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


def _gap_store(tmp_path, gap):
    rows = [{"role": "system", "content": "You are Apollo."}]
    for i in range(60):
        if i not in gap:
            rows.extend(_turn(i))
    eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
    eng.on_session_start(SESSION, platform="discord")
    eng._store.append_batch(SESSION, [dict(r) for r in rows])
    host = [m for i in range(30, 60) for m in _turn(i)]
    host.append({"role": "user", "content": "[kanban] a genuinely new wake"})
    return host


class TestStoreGapIsNotSkipped:
    """Turns the host holds but the store lacks, with later turns stored
    (operator probe on #275): the fallback may skip only rows it has shown
    to be stored, so the gap turns must still land."""

    def test_gap_of_five_turns_mid_history_is_stored(self, tmp_path):
        host = _gap_store(tmp_path, set(range(40, 45)))
        _rebuilt_agent_ingest(tmp_path, host)
        for i in range(40, 45):
            assert _count(tmp_path, f"Card {i} is in review; nothing to do.") >= 1
        assert _count(tmp_path, "[kanban] a genuinely new wake") == 1

    def test_gap_of_one_turn_near_the_end_is_stored(self, tmp_path):
        host = _gap_store(tmp_path, {58})
        _rebuilt_agent_ingest(tmp_path, host)
        assert _count(tmp_path, "Card 58 is in review; nothing to do.") >= 1
        assert _count(tmp_path, "[kanban] a genuinely new wake") == 1

    def test_reused_call_ids_with_same_results_but_new_arguments_are_appended(self, tmp_path):
        # Prism 26ffb81d7a83: ids call_0/call_1 reused, results identical
        # ("OK"), only the assistant's tool arguments differ.
        def calls(n):
            out = []
            for j in range(2):
                out += [
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": f"call_{j}", "type": "function",
                        "function": {"name": "terminal", "arguments": f'{{"n": {n}}}'},
                    }]},
                    {"role": "tool", "tool_call_id": f"call_{j}", "content": "OK"},
                ]
            return out
        rows = [{"role": "system", "content": "You are Apollo."}] + calls(0)
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in rows])
        n0 = _rows(tmp_path)
        new_turn = calls(1)
        _rebuilt_agent_ingest(tmp_path, new_turn)
        assert _rows(tmp_path) == n0 + len(new_turn)


def _plain_turn(i):
    return [
        {"role": "user", "content": f"[kanban] notice {i}: task finished"},
        {"role": "assistant", "content": "Noted; nothing to do."},
    ]


def _plain_gap_store(tmp_path, gap):
    # 4 tool turns (anchors), 12 notification turns whose assistant reply is
    # identical, 2 tool turns: a gap turn lines up with its stored neighbour.
    turns = [_turn(i) for i in range(4)] + [_plain_turn(i) for i in range(100, 112)] + [_turn(i) for i in range(4, 6)]
    keys = list(range(4)) + list(range(100, 112)) + list(range(4, 6))
    rows = [{"role": "system", "content": "You are Apollo."}]
    for key, turn in zip(keys, turns):
        if key not in gap:
            rows.extend(turn)
    eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
    eng.on_session_start(SESSION, platform="discord")
    eng._store.append_batch(SESSION, [dict(r) for r in rows])
    return [m for turn in turns for m in turn]


class TestUserRowGapIsNotSkipped:
    """Prism a6b4b0dcce80 / operator probe-275-gap2: a gap of turns with no
    tool call and a repeated assistant reply differs only on its user row."""

    def test_gap_of_one_notice_is_stored(self, tmp_path):
        host = _plain_gap_store(tmp_path, {105})
        _rebuilt_agent_ingest(tmp_path, host)
        assert _count(tmp_path, "[kanban] notice 105: task finished") >= 1

    def test_gap_of_three_notices_is_stored(self, tmp_path):
        host = _plain_gap_store(tmp_path, {103, 104, 105})
        _rebuilt_agent_ingest(tmp_path, host)
        for i in (103, 104, 105):
            assert _count(tmp_path, f"[kanban] notice {i}: task finished") >= 1

    def test_no_gap_notice_history_appends_nothing(self, tmp_path):
        host = _plain_gap_store(tmp_path, set())
        n0 = _rows(tmp_path)
        _rebuilt_agent_ingest(tmp_path, host)
        assert _rows(tmp_path) == n0

    def test_measured_user_row_rerenders_are_skipped(self, tmp_path):
        # The three re-renders seen on the live store: trailing newline
        # dropped (13007493), a [System note: ...] block dropped (12993102),
        # a timestamp prefix added.
        rows = [{"role": "system", "content": "You are Apollo."}]
        for i in range(6):
            rows.extend(_turn(i))
        users = [i for i, r in enumerate(rows) if r["role"] == "user"]
        rows[users[1]]["content"] += "\n"
        rows[users[2]]["content"] = (
            "[System note: The previous turn was interrupted by a gateway shutdown; the gateway is now back online.]"
            "\n\n[Thu 2026-11-12 00:05:57 PST] " + rows[users[2]]["content"]
        )
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in rows])
        n0 = _rows(tmp_path)
        host = [dict(r) for r in rows[1:]]
        host[users[1] - 1]["content"] = host[users[1] - 1]["content"].rstrip()
        host[users[2] - 1]["content"] = "[Thu 2026-11-12 00:05:57 PST] " + _turn(2)[0]["content"]
        host[users[3] - 1]["content"] = "[Wed 2026-11-11 23:57:30 PST] " + host[users[3] - 1]["content"]
        _rebuilt_agent_ingest(tmp_path, host)
        assert _rows(tmp_path) == n0

    def test_user_row_with_other_text_ends_the_run(self, tmp_path):
        rows = [{"role": "system", "content": "You are Apollo."}]
        for i in range(6):
            rows.extend(_turn(i))
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in rows])
        host = [dict(r) for r in rows[1:]]
        host[8]["content"] = "[kanban] a different wake"
        assert host[8]["role"] == "user"
        _rebuilt_agent_ingest(tmp_path, host)
        assert _count(tmp_path, "[kanban] a different wake") == 1

    def test_user_row_differing_in_its_own_timestamp_ends_the_run(self, tmp_path):
        # Prism 6017ce8a9844: a second timestamp with no note before it is the
        # user's text; a row differing only there is a different row.
        rows = [{"role": "system", "content": "You are Apollo."}]
        for i in range(6):
            rows.extend(_turn(i))
        rows[9]["content"] = "[Thu 2026-11-12 09:00:00 PST] [Wed 2026-11-11 23:57:30 PST] report"
        assert rows[9]["role"] == "user"
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in rows])
        host = [dict(r) for r in rows[1:]]
        host[8]["content"] = "[Thu 2026-11-12 09:00:00 PST] [Wed 2026-11-11 23:58:30 PST] report"
        _rebuilt_agent_ingest(tmp_path, host)
        assert _count(tmp_path, host[8]["content"]) == 1


def _same_text_turn(i):
    # Same prompt every turn, only the timestamp prefix differs (goal-
    # continuation prompts); the reply differs per turn.
    return [
        {"role": "user", "content": f"[Thu 2026-11-12 01:{i - 100:02d}:00 PST] proceed"},
        {"role": "assistant", "content": f"Done step {i}."},
    ]


class TestRerenderMatchedUserRowIsProvisional:
    """Operator review at 3c0079bde214: a gap turn's user row equals the next
    stored turn's under the re-render key; the cursor must not pass it when
    the run then ends on its reply."""

    def test_gap_of_one_same_text_turn_is_stored(self, tmp_path):
        turns = [_turn(i) for i in range(4)] + [_same_text_turn(i) for i in range(100, 112)] + [_turn(i) for i in range(4, 6)]
        keys = list(range(4)) + list(range(100, 112)) + list(range(4, 6))
        rows = [{"role": "system", "content": "You are Apollo."}]
        for key, turn in zip(keys, turns):
            if key != 105:
                rows.extend(turn)
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in rows])
        _rebuilt_agent_ingest(tmp_path, [m for turn in turns for m in turn])
        assert _count(tmp_path, "[Thu 2026-11-12 01:05:00 PST] proceed") >= 1
        assert _count(tmp_path, "Done step 105.") >= 1


class TestRerenderKeyKeepsLeadingWhitespace:
    """Advisory review at dab71bdd9b78 (finding 1): the host variance is
    trailing whitespace only, so leading indentation makes a different row."""

    def test_gap_turn_differing_only_by_indentation_is_stored(self, tmp_path):
        def notice(i, indent=""):
            return [
                {"role": "user", "content": f"{indent}[kanban] notice {i}: task finished"},
                {"role": "assistant", "content": "Noted; nothing to do."},
            ]
        # Turn 105 is turn 106's text indented; the reply is identical.
        plain = [notice(106, "    ") if i == 105 else notice(i) for i in range(100, 112)]
        turns = [_turn(i) for i in range(4)] + plain + [_turn(i) for i in range(4, 6)]
        keys = list(range(4)) + list(range(100, 112)) + list(range(4, 6))
        rows = [{"role": "system", "content": "You are Apollo."}]
        for key, turn in zip(keys, turns):
            if key != 105:
                rows.extend(turn)
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in rows])
        _rebuilt_agent_ingest(tmp_path, [m for turn in turns for m in turn])
        assert _count(tmp_path, "    [kanban] notice 106: task finished") == 1


class TestRepeatedCallIdsDoNotAnchor:
    """Advisory review at dab71bdd9b78 (finding 2): a provider that reuses
    call_0/call_1 every turn with identical arguments and results (polling).
    A repeated id proves nothing about which occurrence a host row is."""

    def test_new_identical_polling_turn_is_appended(self, tmp_path):
        def poll():
            out = []
            for j in range(2):
                out += [
                    {"role": "assistant", "content": "", "tool_calls": [{
                        "id": f"call_{j}", "type": "function",
                        "function": {"name": "terminal", "arguments": '{"command": "status"}'},
                    }]},
                    {"role": "tool", "tool_call_id": f"call_{j}", "content": "still running"},
                ]
            return out + [{"role": "assistant", "content": "Still running."}]
        rows = [{"role": "system", "content": "You are Apollo."}]
        for _ in range(5):
            rows += poll()
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in rows])
        n0 = _rows(tmp_path)
        new_turn = poll()
        _rebuilt_agent_ingest(tmp_path, new_turn)
        assert _rows(tmp_path) == n0 + len(new_turn)


_NOTE = "[Triggering message id: `{}` — use as `message_id` for reply/react/pin via the discord tools.]\n\n"


def _restart_resume_shape():
    """Stored rows and resumed host history of live session 20261112_092254_4cd44442
    at the 10:39:22 gateway boot (role order copied from the real 108-row ingest).

    The stored user rows were written from the live API-bound message, so they
    carry the Discord triggering note after the timestamp. The history a
    restarted gateway resumes from has the note stripped
    (gateway/run_inbound.py strip_discord_triggering_note); the newest host row,
    built for the live turn, still carries it.
    """
    roles = (
        "uatuattattattatatattattatattatatatattattauattattatatatatatatatatatatatatat"
        "tatattatatatatatatatatatauatatata"
    )
    stored = [{"role": "system", "content": "You are Apollo."}]
    for i in range(10):
        stored += _turn(900 + i)
    host = []
    for k, role in enumerate(roles):
        if role == "u":
            ts = f"[Thu 2026-11-12 09:{k % 60:02d}:44 PST] "
            text = f"[Ace] request {k}: make a handoff"
            stored.append({"role": "user", "content": ts + _NOTE.format(1570492307449446000 + k) + text})
            host.append({"role": "user", "content": ts + text})
        elif role == "t":
            row = {"role": "tool", "tool_call_id": f"toolu_01{k:04d}RPcWTmy5", "content": f'{{"output": "result {k}"}}'}
            stored.append(row)
            host.append(dict(row))
        else:
            n_tools = len(roles[k + 1:]) - len(roles[k + 1:].lstrip("t"))
            row = {"role": "assistant", "content": "" if n_tools else f"Answer {k}."}
            if n_tools:
                row["tool_calls"] = [{
                    "id": f"toolu_01{k + 1 + j:04d}RPcWTmy5", "type": "function",
                    "function": {"name": "terminal", "arguments": f'{{"command": "step {k + 1 + j}"}}'},
                } for j in range(n_tools)]
            stored.append(row)
            host.append(dict(row))
    new_user = {"role": "user", "content": "[Thu 2026-11-12 10:40:36 PST] " + _NOTE.format(1570508630308954122) + "[Ace] new"}
    return stored, host + [new_user]


class TestGatewayRestartResumeWithDiscordNote:
    """t_17732be2: at the 10:39 boot the clean-tail session 20261112_092254_4cd44442
    (78 tool rows, 78 distinct call ids) re-stored its whole 108-row slice: the
    walk ended at host row 0, a user row that differs from the stored one only by
    the Discord triggering note, before any call id was matched."""

    def test_restart_resume_appends_only_the_new_user_row(self, tmp_path):
        stored, history = _restart_resume_shape()
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in stored])
        n0 = _rows(tmp_path)
        fresh = _rebuilt_agent_ingest(tmp_path, history)
        assert fresh._last_ingest_reconciliation["reason"] == "replayed history anchored on stored tool calls"
        assert _rows(tmp_path) == n0 + 1

    def test_restart_system_note_ahead_of_the_timestamp_is_matched(self, tmp_path):
        # Session 20261108_093540_fa4b70b9 at the 10:39 boot: the host row puts the
        # gateway's restart note ahead of a second timestamp; the stored row has neither.
        stored, history = _restart_resume_shape()
        ts, text = "[Thu 2026-11-12 06:43:10 PST] ", "[kanban wake] Card t_b40ec765 waits in review"
        note = ("[System note: The previous turn was interrupted by a gateway shutdown; the gateway "
                "is now back online. Do NOT re-execute old tool calls.]\n\n")
        s_idx = next(i for i, r in enumerate(stored) if r["role"] == "user" and _NOTE[:20] in r["content"])
        stored[s_idx] = {"role": "user", "content": ts + text}
        history[0] = {"role": "user", "content": ts + note + ts + text}
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in stored])
        n0 = _rows(tmp_path)
        _rebuilt_agent_ingest(tmp_path, history)
        assert _rows(tmp_path) == n0 + 1

    def test_gap_user_row_with_other_text_behind_the_note_is_stored(self, tmp_path):
        stored, history = _restart_resume_shape()
        # A host user row the store never got: same timestamp, other text.
        idx = next(i for i, m in enumerate(history) if m["role"] == "user" and i > 40)
        history[idx] = {"role": "user", "content": history[idx]["content"] + " (edited)"}
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in stored])
        _rebuilt_agent_ingest(tmp_path, history)
        assert _count(tmp_path, history[idx]["content"]) == 1
