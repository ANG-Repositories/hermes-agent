"""Every worker run has a placement record; every run without a worker says why (t_4b313c15).

The invariant: a ``spawned`` event is never written without a ``placement`` event for the same run, in the same
transaction. Measured 14 days to 2026-11-11 over every board: one run broke it, landscaping/t_1073ffef run 34.
The operator ended run 34 (``scheduled``) one second after the claim; the ci-box spawn landed four seconds later;
``_set_worker_pid`` saw the claim lost, wrote ``spawned`` (late_spawn, claim_lost) and returned before the
``placement`` write. ``spawn_aborted`` then stopped the worker, but nothing recorded where it had run.

A run that starts no worker carries ``task_runs.no_worker_class``: ``operator`` (a human/human:* lane row),
``synthetic`` (a zero-duration run written for a card that was never claimed) or ``refused`` (claimed, closed
with no worker), so "no placement event" is never the only evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as c:
        yield c


def _kinds(conn, run_id):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE run_id=? ORDER BY id", (run_id,))]


def _payload(conn, run_id, kind):
    row = conn.execute("SELECT payload FROM task_events WHERE run_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                       (run_id, kind)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def _cls(conn, run_id):
    return conn.execute("SELECT no_worker_class FROM task_runs WHERE id=?", (run_id,)).fetchone()[0]


def test_t1073ffef_run34_late_spawn_after_operator_close_still_writes_its_placement(conn):
    """The one real case: the run is ended by an operator transition while the spawn is in flight."""
    tid = kb.create_task(conn, title="operator closes the run mid-spawn", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    run_id = claimed.current_run_id
    assert kb.schedule_task(conn, tid, reason="operator hold", wake_at=2_000_000_000) is True
    record = {"run_id": run_id, "route": "remote-policy", "host": "ci-box", "route_class": "portable"}
    assert kbd._set_worker_pid(conn, tid, 80596, run_id=run_id, placement=record) is False
    kinds = _kinds(conn, run_id)
    assert "spawned" in kinds and "placement" in kinds
    placed = _payload(conn, run_id, "placement")
    assert placed["host"] == "ci-box" and placed["claim_lost"] is True
    # A worker existed, so the run is not a no-worker run, whatever its close said.
    assert _cls(conn, run_id) is None


def test_spawn_without_a_caller_record_still_writes_a_placement(conn):
    tid = kb.create_task(conn, title="caller passed no record", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    assert kbd._set_worker_pid(conn, tid, 4242, run_id=claimed.current_run_id) is True
    placed = _payload(conn, claimed.current_run_id, "placement")
    assert placed is not None and placed["reason"] == "unrecorded" and placed["run_id"] == claimed.current_run_id


def test_worker_adopted_pid_writes_a_placement_when_the_dispatcher_wrote_none(conn, monkeypatch):
    tid = kb.create_task(conn, title="dispatcher died before the pid write", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    monkeypatch.setattr(kb, "_host_prefix", lambda: (claimed.claim_lock or "").split(":", 1)[0])
    assert kbd.adopt_worker_pid(conn, tid, claimed.current_run_id, 5151) is True
    placed = _payload(conn, claimed.current_run_id, "placement")
    assert placed is not None and placed["entry"] == "worker_registered"


def test_worker_registering_after_its_run_was_closed_records_placement_and_clears_class(conn, monkeypatch):
    """Prism f065138133fd: claim R, close R mid-spawn, dispatcher dies before _set_worker_pid, worker starts."""
    tid = kb.create_task(conn, title="dispatcher died, run closed, worker started anyway", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    run_id = claimed.current_run_id
    assert kb.schedule_task(conn, tid, reason="operator hold", wake_at=2_000_000_000) is True
    assert _cls(conn, run_id) == "refused"
    monkeypatch.setattr(kb, "_host_prefix", lambda: (claimed.claim_lock or "").split(":", 1)[0])
    assert kbd.adopt_worker_pid(conn, tid, run_id, 9191) is False
    kinds = _kinds(conn, run_id)
    assert "worker_registered" in kinds and kinds.count("placement") == 1
    placed = _payload(conn, run_id, "placement")
    assert placed["entry"] == "worker_registered" and placed["claim_lost"] is True
    assert _payload(conn, run_id, "worker_registered")["claim_lost"] is True
    assert _cls(conn, run_id) is None
    # The card's pid is not touched by a losing worker.
    assert conn.execute("SELECT worker_pid FROM tasks WHERE id=?", (tid,)).fetchone()[0] is None


def test_worker_with_a_run_id_of_another_task_records_nothing(conn):
    tid = kb.create_task(conn, title="a", assignee="worker")
    other = kb.create_task(conn, title="b", assignee="worker")
    run_id = kb.claim_task(conn, other).current_run_id
    assert kbd.adopt_worker_pid(conn, tid, run_id, 9292) is False
    assert "placement" not in _kinds(conn, run_id)


def test_placement_is_never_written_twice_for_one_run(conn, monkeypatch):
    tid = kb.create_task(conn, title="adopt after a normal spawn", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    assert kbd._set_worker_pid(conn, tid, 6161, run_id=claimed.current_run_id, placement={"host": "ace-ai"}) is True
    monkeypatch.setattr(kb, "_host_prefix", lambda: (claimed.claim_lock or "").split(":", 1)[0])
    kbd.adopt_worker_pid(conn, tid, claimed.current_run_id, 6161)
    assert _kinds(conn, claimed.current_run_id).count("placement") == 1


def test_operator_verdict_row_is_classed_operator(conn):
    tid = kb.create_task(conn, title="human review verdict", assignee="human:apollo")
    with kb.write_txn(conn):
        run_id = kb._synthesize_ended_run(conn, tid, outcome="completed", summary="approved")
    assert _cls(conn, run_id) == "operator"


def test_never_claimed_close_is_classed_synthetic(conn):
    tid = kb.create_task(conn, title="closed without a claim", assignee="worker")
    with kb.write_txn(conn):
        run_id = kb._synthesize_ended_run(conn, tid, outcome="completed", summary="superseded")
    assert _cls(conn, run_id) == "synthetic"


def test_claimed_then_closed_before_any_spawn_is_classed_refused(conn):
    tid = kb.create_task(conn, title="refused before spawn", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    with kb.write_txn(conn):
        kb._end_run(conn, tid, outcome="workspace_refused", status="workspace_refused")
    assert _cls(conn, claimed.current_run_id) == "refused"


def test_a_run_that_spawned_is_not_classed(conn):
    tid = kb.create_task(conn, title="normal worker", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    assert kbd._set_worker_pid(conn, tid, 7171, run_id=claimed.current_run_id, placement={"host": "ace-ai"}) is True
    with kb.write_txn(conn):
        kb._end_run(conn, tid, outcome="completed", status="done")
    assert _cls(conn, claimed.current_run_id) is None


def test_claimed_human_lane_close_is_classed_operator(conn):
    """2,412 of the 14-day rows: a review-lane run under a human profile, closed with changes_requested."""
    tid = kb.create_task(conn, title="human lane claimed", assignee="worker")
    claimed = kb.claim_task(conn, tid)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET profile='human:apollo' WHERE id=?", (claimed.current_run_id,))
        kb._end_run(conn, tid, outcome="changes_requested", status="changes_requested")
    assert _cls(conn, claimed.current_run_id) == "operator"
