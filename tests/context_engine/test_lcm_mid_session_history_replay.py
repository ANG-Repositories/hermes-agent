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


# Head row of the history the gateway resumed session 20261024_..._9b06a4 from at
# the 10:39:22 boot (state.db, loaded by load_transcript); trimmed goal text.
_RESUMED_SCAFFOLD_HEAD = {
    "role": "user",
    "content": (
        "[Current user objective preserved from compacted history]\n"
        "[Thu 2026-11-12 08:45:04 PST] [Continuing toward your standing goal]\n"
        "Goal: Every open item of this session's lineage is finished and proven."
    ),
}


class TestRestartResumeOfCompactedSession:
    """t_17732be2 send-back: 13 of 18 sessions at the 10:39 boot resumed from a
    compacted transcript, [preserved-objective scaffold] + the stored tail. The
    reconcile returned cursor 1 ("skipped scaffold-only prefix") and never ran
    the anchored walk, so every row after the scaffold was stored again."""

    def test_scaffold_head_then_stored_tail_appends_only_the_new_row(self, tmp_path):
        stored, history = _restart_resume_shape()
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in stored])
        n0 = _rows(tmp_path)
        fresh = _rebuilt_agent_ingest(tmp_path, [dict(_RESUMED_SCAFFOLD_HEAD)] + history)
        assert fresh._last_ingest_reconciliation["reason"] == "replayed history anchored on stored tool calls"
        assert _rows(tmp_path) == n0 + 1

    def test_scaffold_head_then_gap_row_is_stored(self, tmp_path):
        stored, history = _restart_resume_shape()
        idx = next(i for i, m in enumerate(history) if m["role"] == "user" and i > 40)
        history[idx] = {"role": "user", "content": history[idx]["content"] + " (edited)"}
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in stored])
        _rebuilt_agent_ingest(tmp_path, [dict(_RESUMED_SCAFFOLD_HEAD)] + history)
        assert _count(tmp_path, history[idx]["content"]) == 1


def _persisted_marker(path, preview):
    # The host form of a spilled tool result (session ..63465a4e,
    # toolu_018BRyyoksKKzPxJwMuPpDrS at the 10:39 boot).
    return (
        "<persisted-output>\nThis tool result was too large (104,100 characters, 101.7 KB).\n"
        f"Full output saved to: {path}\n"
        "Use the read_file tool with offset and limit to access specific sections of this output.\n\n"
        f"Preview (first 1500 chars):\n{preview}\n...\n</persisted-output>"
    )


class TestRestartResumeOfPersistedOutputMarker:
    """t_17732be2 send-back: session ..63465a4e (live 678 re-stored at the 10:39
    boot). With large-output externalization off, the store's write path adds a
    "[LCM persisted-output marker identity: preview_sha256=...]" line to a
    spilled tool result; the resumed history holds the marker without it, so
    the anchored walk ended at that row."""

    def _shape(self, tmp_path, host_preview):
        spill = tmp_path / "spillover" / "toolu_018BRyyoksKKzPxJwMuPpDrS.txt"
        spill.parent.mkdir()
        spill.write_text('{"success": true, "name": "session-card-overview"}' + "x" * 104_100)
        cfg = _cfg(tmp_path)
        cfg.large_output_externalization_enabled = False
        stored, history = _restart_resume_shape()
        idx = next(i for i, m in enumerate(history) if m["role"] == "tool" and i > 30)
        s_idx = next(i for i, r in enumerate(stored) if r.get("tool_call_id") == history[idx]["tool_call_id"])
        preview = '{"success": true, "name": "session-card-overview"}'
        stored[s_idx] = dict(stored[s_idx], content=_persisted_marker(spill, preview))
        history[idx] = dict(history[idx], content=_persisted_marker(spill, host_preview))
        eng = LCMEngine(config=cfg, hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng.ingest([dict(r) for r in stored])
        n0 = _rows(tmp_path)
        assert "[LCM persisted-output marker identity:" in "".join(
            r[0] for r in sqlite3.connect(str(tmp_path / "lcm.db")).execute(
                "SELECT content FROM messages WHERE session_id=? AND role='tool'", (SESSION,)))
        fresh = LCMEngine(config=cfg, hermes_home=str(tmp_path))
        fresh.on_session_start(SESSION, platform="discord")
        fresh.ingest([dict(m) for m in history])
        return n0, history[idx]

    def test_marker_without_write_path_lines_appends_only_the_new_row(self, tmp_path):
        n0, _ = self._shape(tmp_path, '{"success": true, "name": "session-card-overview"}')
        assert _rows(tmp_path) == n0 + 1

    def test_marker_with_another_preview_is_stored(self, tmp_path):
        n0, row = self._shape(tmp_path, '{"success": true, "name": "another-skill"}')
        assert _rows(tmp_path) > n0 + 1


def test_first_ingest_after_rebind_logs_one_info_line(tmp_path, caplog):
    # A restart is judged from agent.log: one INFO line per session per rebuild.
    stored, history = _restart_resume_shape()
    eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
    eng.on_session_start(SESSION, platform="discord")
    eng._store.append_batch(SESSION, [dict(r) for r in stored])
    caplog.set_level("INFO", logger="plugins.context_engine.lcm.engine")
    fresh = _rebuilt_agent_ingest(tmp_path, history)
    fresh.ingest([dict(m) for m in history] + [{"role": "assistant", "content": "ok"}])
    lines = [r.getMessage() for r in caplog.records if "first ingest after rebind" in r.getMessage()]
    assert len(lines) == 1
    assert f"session={SESSION}" in lines[0] and f"incoming={len(history)}" in lines[0]
    assert "reason=replayed history anchored on stored tool calls" in lines[0] and "appended=1" in lines[0]


def _first_ingest_lines(caplog):
    return [r.getMessage() for r in caplog.records if "first ingest after rebind" in r.getMessage()]


class TestFirstIngestLogLineOnEveryCaller:
    """t_f4a72542: after the 17:35 boot the line fired for 1 of 25 sessions; it
    wrapped only ingest(). Every caller of _ingest_messages can carry the first
    ingest after a rebind, so each must log it exactly once."""

    def _rebound(self, tmp_path, caplog):
        stored, history = _restart_resume_shape()
        eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        eng.on_session_start(SESSION, platform="discord")
        eng._store.append_batch(SESSION, [dict(r) for r in stored])
        n0 = _rows(tmp_path)
        fresh = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
        fresh.on_session_start(SESSION, platform="discord")
        assert fresh._ingest_cursor_needs_reconcile
        caplog.set_level("INFO", logger="plugins.context_engine.lcm.engine")
        return fresh, history, n0

    def _assert_one_line(self, tmp_path, caplog, history, n0):
        lines = _first_ingest_lines(caplog)
        assert len(lines) == 1, lines
        appended = _rows(tmp_path) - n0
        assert appended == 1
        assert f"session={SESSION}" in lines[0] and f"incoming={len(history)}" in lines[0]
        assert "reason=replayed history anchored on stored tool calls" in lines[0]
        assert lines[0].endswith(f"appended={appended}")

    def test_turn_start_preflight_first_ingest_logs_once(self, tmp_path, caplog):
        # The path a gateway boot resume takes: agent/turn_context_compaction.py
        # _engine_preflight_maintenance -> should_compress_preflight ingests the
        # resumed transcript BEFORE the model call, so the post_llm_call ingest()
        # finds the reconcile flag already consumed.
        fresh, history, n0 = self._rebound(tmp_path, caplog)
        fresh.should_compress_preflight([dict(m) for m in history])
        self._assert_one_line(tmp_path, caplog, history, n0)
        fresh.ingest([dict(m) for m in history] + [{"role": "assistant", "content": "ok"}])
        assert len(_first_ingest_lines(caplog)) == 1

    def test_compress_first_ingest_logs_once(self, tmp_path, caplog):
        # Pre-API / threshold / gateway hygiene compaction of a resumed transcript.
        fresh, history, n0 = self._rebound(tmp_path, caplog)
        fresh.compress([dict(m) for m in history], current_tokens=10)
        self._assert_one_line(tmp_path, caplog, history, n0)
        fresh.ingest([dict(m) for m in history] + [{"role": "assistant", "content": "ok"}])
        assert len(_first_ingest_lines(caplog)) == 1

    def test_lcm_tool_call_first_ingest_logs_once(self, tmp_path, caplog):
        fresh, history, n0 = self._rebound(tmp_path, caplog)
        fresh.handle_tool_call("lcm_grep", {"query": "handoff"}, messages=[dict(m) for m in history])
        self._assert_one_line(tmp_path, caplog, history, n0)

    def test_session_end_flush_first_ingest_logs_once(self, tmp_path, caplog):
        fresh, history, n0 = self._rebound(tmp_path, caplog)
        fresh.on_session_end(SESSION, [dict(m) for m in history])
        self._assert_one_line(tmp_path, caplog, history, n0)

    def test_ingest_first_ingest_logs_once(self, tmp_path, caplog):
        fresh, history, n0 = self._rebound(tmp_path, caplog)
        fresh.ingest([dict(m) for m in history])
        self._assert_one_line(tmp_path, caplog, history, n0)
        fresh.ingest([dict(m) for m in history] + [{"role": "assistant", "content": "ok"}])
        assert len(_first_ingest_lines(caplog)) == 1


def _stored_into(tmp_path, stored):
    eng = LCMEngine(config=_cfg(tmp_path), hermes_home=str(tmp_path))
    eng.on_session_start(SESSION, platform="discord")
    eng._store.append_batch(SESSION, [dict(r) for r in stored])
    return _rows(tmp_path)


class TestRestartResumeOfMergedUserTurn:
    """t_ca2c9470: session 20261113_202135_169c983f at the 21:22:26 boot stored all
    103 resumed rows again (persisted ambiguous delta, cursor 0). Its first stored
    row is a merged user turn, "[ts] <a>\\n\\n[ts] [Triggering ...]\\n\\n<b>"; the
    restarted gateway rebuilds it from the clean parts as "<a>\\n\\n<b>", so the
    walk ended at host row 0."""

    def _shape(self):
        stored, history = _restart_resume_shape()
        a = "[Ace] why does this happen when we have plenty of subscriptions available?"
        b = "[Ace] continue"
        s_idx = next(i for i, r in enumerate(stored) if r["role"] == "user" and _NOTE[:20] in r["content"])
        assert history[0]["role"] == "user"
        stored[s_idx] = {"role": "user", "content": (
            "[Fri 2026-11-13 20:21:33 PST] " + a + "\n\n"
            "[Fri 2026-11-13 20:32:07 PST] " + _NOTE.format(1571019880512356353) + b)}
        history[0] = {"role": "user", "content": a + "\n\n" + b}
        return stored, history

    def test_merged_user_turn_rebuilt_from_clean_parts_appends_only_the_new_row(self, tmp_path):
        stored, history = self._shape()
        n0 = _stored_into(tmp_path, stored)
        fresh = _rebuilt_agent_ingest(tmp_path, history)
        assert fresh._last_ingest_reconciliation["reason"] == "replayed history anchored on stored tool calls"
        assert _rows(tmp_path) == n0 + 1

    def test_merged_user_turn_with_a_part_the_store_lacks_is_stored(self, tmp_path):
        stored, history = self._shape()
        idx = next(i for i, m in enumerate(history) if m["role"] == "user" and i > 40)
        history[idx] = {"role": "user", "content": history[idx]["content"] + "\n\n[Ace] and one more thing"}
        _stored_into(tmp_path, stored)
        _rebuilt_agent_ingest(tmp_path, history)
        assert _count(tmp_path, history[idx]["content"]) == 1

    def test_interior_wrapper_led_paragraph_the_store_lacks_is_stored(self, tmp_path):
        """Prism 33e6e68e02a5: only a gateway timestamp proves a merge boundary, so
        an interior "[System note: ...]" paragraph is content, not a wrapper."""
        stored, history = self._shape()
        idx = next(i for i, m in enumerate(history) if m["role"] == "user" and i > 40)
        s_idx = next(i for i, r in enumerate(stored) if r["role"] == "user"
                     and r["content"].endswith(history[idx]["content"].split("] ", 1)[1]))
        stored[s_idx] = dict(stored[s_idx], content=stored[s_idx]["content"] + "\n\n[Ace] B")
        history[idx] = {"role": "user", "content": (
            history[idx]["content"] + "\n\n[System note: payment failed]\n\n[Ace] B")}
        _stored_into(tmp_path, stored)
        _rebuilt_agent_ingest(tmp_path, history)
        assert _count(tmp_path, history[idx]["content"]) == 1

    def test_interior_timestamp_the_host_row_carries_is_content(self, tmp_path):
        """Prism 33e6e68e02a5 round 2: a pasted log line's timestamp in the host row
        is content, so a changed one is stored, not keyed equal to the old one."""
        stored, history = self._shape()
        idx = next(i for i, m in enumerate(history) if m["role"] == "user" and i > 40)
        s_idx = next(i for i, r in enumerate(stored) if r["role"] == "user"
                     and r["content"].endswith(history[idx]["content"].split("] ", 1)[1]))
        stored[s_idx] = dict(stored[s_idx], content=(
            stored[s_idx]["content"] + "\n\n[Fri 2026-11-13 20:21:33 PST] failure"))
        history[idx] = {"role": "user", "content": (
            history[idx]["content"] + "\n\n[Fri 2026-11-13 20:32:07 PST] failure")}
        _stored_into(tmp_path, stored)
        _rebuilt_agent_ingest(tmp_path, history)
        assert _count(tmp_path, history[idx]["content"]) == 1

    def test_unchanged_pasted_timestamp_in_a_merged_turn_is_not_stored_again(self, tmp_path):
        """Prism 0db0d2f7a252: a pasted stamp both rows carry, after the stored row's
        own merge stamps, still matches, so the resumed rows are not stored again."""
        stored, history = self._shape()
        pasted = "\n\n[Fri 2026-11-13 20:21:33 PST] failure"
        s_idx = next(i for i, r in enumerate(stored) if r["role"] == "user" and _NOTE[:20] in r["content"])
        stored[s_idx] = dict(stored[s_idx], content=stored[s_idx]["content"] + pasted)
        history[0] = {"role": "user", "content": history[0]["content"] + pasted}
        n0 = _stored_into(tmp_path, stored)
        _rebuilt_agent_ingest(tmp_path, history)
        assert _rows(tmp_path) == n0 + 1

    def test_unchanged_pasted_timestamp_in_a_rewrapped_row_is_not_stored_again(self, tmp_path):
        """Prism 2efa6eb1a00c: the same pasted stamp in a row whose leading wrappers
        differ still matches."""
        stored, history = self._shape()
        pasted = "\n\n[Fri 2026-11-13 20:21:33 PST] failure"
        idx = next(i for i, m in enumerate(history) if m["role"] == "user" and i > 40)
        s_idx = next(i for i, r in enumerate(stored) if r["role"] == "user"
                     and r["content"].endswith(history[idx]["content"].split("] ", 1)[1]))
        stored[s_idx] = dict(stored[s_idx], content=stored[s_idx]["content"] + pasted)
        history[idx] = {"role": "user", "content": history[idx]["content"] + pasted}
        n0 = _stored_into(tmp_path, stored)
        _rebuilt_agent_ingest(tmp_path, history)
        assert _rows(tmp_path) == n0 + 1


class TestRestartResumeOfTwiceStoredUserTurn:
    """t_ca2c9470: session 20261023_031240_9df89e67 at the 21:22:26 boot stored 89
    rows again. The store holds one user turn twice in a row, "[ts] <text>"
    (13429799) then "<text>" (13431618); the resumed history holds it once, so
    the walk ended at the assistant row after it."""

    def _shape(self):
        stored, history = _restart_resume_shape()
        idx = next(i for i, m in enumerate(history) if m["role"] == "user" and i > 40)
        s_idx = next(i for i, r in enumerate(stored) if r["role"] == "user"
                     and r["content"].endswith(history[idx]["content"].split("] ", 1)[1]))
        stored.insert(s_idx + 1, {"role": "user", "content": history[idx]["content"].split("] ", 1)[1]})
        assert history[idx + 1]["role"] != "user"
        return stored, history, idx

    def test_twice_stored_user_turn_appends_only_the_new_row(self, tmp_path):
        stored, history, _ = self._shape()
        n0 = _stored_into(tmp_path, stored)
        _rebuilt_agent_ingest(tmp_path, history)
        assert _rows(tmp_path) == n0 + 1

    def test_row_after_twice_stored_user_turn_that_the_store_lacks_is_stored(self, tmp_path):
        stored, history, idx = self._shape()
        history[idx + 1] = dict(history[idx + 1], content="A reply the store never got.", tool_calls=None)
        history[idx + 1].pop("tool_calls")
        _stored_into(tmp_path, stored)
        _rebuilt_agent_ingest(tmp_path, history)
        assert _count(tmp_path, "A reply the store never got.") == 1
