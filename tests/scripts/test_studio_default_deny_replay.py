"""t_062a5948: the default-deny replay classifies Studio runs correctly on a fixture board."""
from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("sddr", ROOT / "scripts" / "studio_default_deny_replay.py")
sddr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sddr)

T0 = 1_794_000_000


def _board(tmp_path):
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, no_worker INTEGER DEFAULT 0);"
                      "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT,"
                      " payload TEXT, created_at INTEGER);")

    def card(tid, assignee="daedalus"):
        con.execute("INSERT INTO tasks VALUES (?,?,?,0)", (tid, "title " + tid, assignee))

    def place(tid, run, at, host="mac-studio-m3u", sc=None, rule="default", free=0):
        p = {"host": host, "rule": rule, "studio_capability": sc,
             "considered": [{"host": "ace-ai", "band": "ok", "free_slots": free, "reachable": True}]}
        con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                    (tid, run, "placement", json.dumps(p), at))

    card("t_vault"); card("t_state"); card("t_port"); card("t_op", "apollo"); card("t_pool")
    place("t_vault", 1, T0 + 10, sc={"capability": "obsidian-vault", "source": "declared", "kind": "capability"})
    place("t_state", 2, T0 + 20, sc={"capability": "studio-state", "source": "declared", "kind": "capability"})
    place("t_state", 3, T0 + 86400 + 20, sc={"capability": "studio-state", "source": "declared", "kind": "capability"})
    place("t_port", 4, T0 + 30, rule="default", free=0)
    con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES "
                "('t_x', 9, 'worker_placed', '{}', ?)", (T0 + 330,))
    place("t_op", 5, T0 + 40, sc={"capability": "studio-state", "source": "declared", "kind": "capability"})
    place("t_pool", 6, T0 + 50, host="ace-ai")
    con.commit()
    con.close()
    caps = tmp_path / "caps.json"
    caps.write_text(json.dumps({"hosts": {"studio": ["obsidian-vault", "studio-state", "docker"],
                                          "ace-ai": ["docker"]}}))
    return db, caps


def test_replay_classifies_each_studio_run(tmp_path):
    db, caps = _board(tmp_path)
    only, _ = sddr.load_caps(caps)
    rows, moved, stuck = sddr.replay([str(db)], only, {"obsidian-vault"}, T0, T0 + 2 * 86400)
    v = {(r["task_id"], r["run_id"]): r["verdict"] for r in rows}
    assert v == {("t_vault", 1): "allowlisted", ("t_state", 2): "override", ("t_state", 3): "override",
                 ("t_port", 4): "moves", ("t_op", 5): "exempt"}
    assert [r["wait_s"] for r in moved] == [300]
    assert [r["task_id"] for r in stuck] == ["t_state", "t_state"]


def test_report_counts_overrides_per_day_and_lists_stuck_cards(tmp_path):
    db, caps = _board(tmp_path)
    only, _ = sddr.load_caps(caps)
    rows, moved, stuck = sddr.replay([str(db)], only, {"obsidian-vault"}, T0, T0 + 2 * 86400)
    buf = io.StringIO()
    sddr.report(rows, moved, stuck, {"obsidian-vault"}, 2, out=buf)
    text = buf.getvalue()
    assert "needs an operator override (no host at all) 2" in text
    assert "Mean 1.0 overrides/day" in text and "Under the ~10/day line" in text
    assert "| t_state | 2 | studio-state | declared |" in text
    assert "| obsidian-vault | 1 | 1 | declared | keep |" in text


def test_structural_rule_with_parenthesized_detail_needs_an_override():
    """Prism e3e on #251: ``linked(derived-from->t_x)`` is the structural ``linked`` rule, not a portable run."""
    cap, src = sddr.capability_of({"rule": "linked(derived-from->t_1234abcd)"})
    assert src == "structural"
    assert sddr.studio_only_cap(cap, frozenset())
    assert sddr.covered(cap, {"linked"}) and not sddr.covered(cap, {"profile"})
