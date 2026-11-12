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


def test_operator_exemption_is_byte_exact_as_the_live_rule(tmp_path):
    """Prism 2b96aecfa7f7: ``Apollo`` is not the operator profile ``apollo`` in the live rule; the replay agrees."""
    db, caps = _board(tmp_path)
    con = sqlite3.connect(db)
    con.execute("UPDATE tasks SET assignee='Apollo' WHERE id='t_op'")
    con.commit()
    con.close()
    only, _ = sddr.load_caps(caps)
    rows, _m, _s = sddr.replay([str(db)], only, {"obsidian-vault"}, T0, T0 + 2 * 86400)
    assert {(r["task_id"], r["run_id"]): r["verdict"] for r in rows}[("t_op", 5)] == "override"


def test_local_review_placement_counts_as_a_studio_start(tmp_path):
    """Prism 14e388fdd6ab: the review lane records ``{route_class: local, entry: review}`` with no host."""
    db, caps = _board(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO tasks VALUES ('t_rev', 'r', 'daedalus', 0)")
    con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                ("t_rev", 7, "placement", json.dumps({"route_class": "local", "entry": "review"}), T0 + 60))
    con.commit()
    con.close()
    only, _ = sddr.load_caps(caps)
    rows, _m, stuck = sddr.replay([str(db)], only, {"obsidian-vault"}, T0, T0 + 2 * 86400)
    r = next(r for r in rows if r["task_id"] == "t_rev")
    assert (r["cap"], r["verdict"]) == ("review_lane", "override")
    assert "t_rev" in [s["task_id"] for s in stuck]
    rows, _m, _s = sddr.replay([str(db)], only, {"review_lane"}, T0, T0 + 2 * 86400)
    assert next(r for r in rows if r["task_id"] == "t_rev")["verdict"] == "allowlisted"


def test_pool_placements_after_now_do_not_set_the_wait(tmp_path):
    """Prism 6c3aa1e6038d: with a fixed --now, a pool placement after it must not be read as the next free slot."""
    db, caps = _board(tmp_path)
    only, _ = sddr.load_caps(caps)
    # the only worker_placed in the fixture is at T0+330; replay a window that ends before it
    _rows, moved, _s = sddr.replay([str(db)], only, {"obsidian-vault"}, T0, T0 + 100)
    assert [(r["task_id"], r["wait_s"]) for r in moved] == [("t_port", None)]


def test_two_capabilities_split_across_pool_hosts_is_not_movable(tmp_path):
    """Prism e528d5a843f6: a card needing gpu AND docker can move only if ONE pool host has both."""
    db, _caps = _board(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO tasks VALUES ('t_two', 't', 'daedalus', 0)")
    p = {"host": "mac-studio-m3u", "rule": "requires:gpu,docker",
         "studio_capability": {"capability": "gpu,docker", "source": "declared", "kind": "capability"}}
    con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                ("t_two", 8, "placement", json.dumps(p), T0 + 70))
    con.commit()
    con.close()
    split = tmp_path / "split.json"
    split.write_text(json.dumps({"hosts": {"studio": ["gpu", "docker"], "ace-ai": ["gpu"], "ci-box": ["docker"]}}),
                     encoding="utf-8")
    only, pool = sddr.load_caps(split)
    rows, _m, stuck = sddr.replay([str(db)], only, set(), T0, T0 + 2 * 86400, pool_hosts=pool)
    assert next(r for r in rows if r["task_id"] == "t_two")["verdict"] == "override"
    both = tmp_path / "both.json"
    both.write_text(json.dumps({"hosts": {"studio": ["gpu", "docker"], "ace-ai": ["gpu", "docker"]}}),
                    encoding="utf-8")
    only, pool = sddr.load_caps(both)
    rows, _m, _s = sddr.replay([str(db)], only, set(), T0, T0 + 2 * 86400, pool_hosts=pool)
    assert next(r for r in rows if r["task_id"] == "t_two")["verdict"] == "moves"


def test_pre_capability_host_studio_pin_is_unknown_not_a_move(tmp_path):
    """Prism 9fec6c08b569: rule host_studio with no studio_capability predates the record: unknown, own row."""
    db, caps = _board(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO tasks VALUES ('t_old', 'old pin', 'daedalus', 0)")
    con.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                ("t_old", 9, "placement", json.dumps({"host": "mac-studio-m3u", "rule": "host_studio"}), T0 + 80))
    con.commit()
    con.close()
    only, _ = sddr.load_caps(caps)
    rows, moved, stuck = sddr.replay([str(db)], only, {"obsidian-vault"}, T0, T0 + 2 * 86400)
    r = next(r for r in rows if r["task_id"] == "t_old")
    assert r["verdict"] == "unknown"
    assert "t_old" not in [m["task_id"] for m in moved] and "t_old" not in [s["task_id"] for s in stuck]
    buf = io.StringIO()
    sddr.report(rows, moved, stuck, {"obsidian-vault"}, 2, out=buf)
    text = buf.getvalue()
    assert "unknown (pre-capability Studio pin, rule host_studio) 1" in text
    assert "| host_studio (no capability record) | 1 | 1 |" in text
