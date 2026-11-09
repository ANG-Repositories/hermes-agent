"""Reap ownership compares CLOCK_BOOTTIME with CLOCK_BOOTTIME (t_29dee3c8).

The run's liveness evidence (claim = lower bound, last heartbeat = upper bound)
used to exist only as wall-clock epoch seconds, while a member's birth is
/proc starttime on CLOCK_BOOTTIME and was converted with an epoch sampled at
reap time. A wall step between the evidence and the reap moved every converted
birth by the step (Prism cc1f9d478681). On Linux the evidence now also records
BOOTTIME + boot id, and ownership compares births with it directly.

These tests drive the real DB path (claim -> heartbeat -> terminate-path reap)
against a fake process table, with the Linux clock seams pinned so they run on
any host. Timeline: boot 0 == wall 10_000 before any step; claim at boot 100,
last heartbeat at boot 200.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb

pytestmark = pytest.mark.skipif(
    not hasattr(os, "getsid") or not hasattr(os, "killpg"), reason="POSIX sessions only",
)

SID = 99_999
BOOT0_WALL = 10_000.0


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as c:
        yield c


def _run(conn, monkeypatch, *, writer_boot_id, member_boot_birth, wall_step):
    """Record a run (claim at boot 100, heartbeat at boot 200, stamped with
    ``writer_boot_id``; None = no BOOTTIME recorded), then reap session SID
    after a wall step of ``wall_step`` seconds. Returns the signalled pgids."""
    monkeypatch.setattr(kb, "_PRECISE_BIRTH", True)
    monkeypatch.setattr(kb, "_boot_id", lambda: "boot-B")
    stamp = {"boot": 100.0}
    monkeypatch.setattr(
        kb, "_boot_evidence",
        lambda: None if writer_boot_id is None else (stamp["boot"], writer_boot_id),
        raising=False,
    )
    tid = kb.create_task(conn, title="card", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    stamp["boot"] = 200.0
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "heartbeat", {}, run_id=run_id)
        # The same two moments on the wall clock, before any step.
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'claimed'",
                     (int(BOOT0_WALL + 100), tid))
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'heartbeat'",
                     (int(BOOT0_WALL + 200), tid))

    # Fake process table: one member (pid == pgid 222) in SID, born at
    # ``member_boot_birth`` on CLOCK_BOOTTIME, as /proc/<pid>/stat reports it.
    sids = {222: SID}
    signalled: list = []
    real_getsid = os.getsid

    def getsid(pid):
        if pid == 0:
            return real_getsid(0)
        if pid not in sids:
            raise ProcessLookupError(pid)
        return sids[pid]

    def killpg(pgid, sig):
        signalled.append(pgid)
        sids.pop(pgid, None)  # the member dies on the signal

    epoch_now = BOOT0_WALL + wall_step  # wall - BOOTTIME after the step
    monkeypatch.setattr(kb, "_worker_session_members",
                        lambda sid: [(p, p) for p, s in sids.items() if s == sid])
    monkeypatch.setattr(kb, "_reap_record",
                        lambda pid, sid: (pid, member_boot_birth) if sids.get(pid) == sid else None)
    # The wall conversion as #2523 does it: starttime + an epoch sampled NOW.
    monkeypatch.setattr(kb, "_ownership_birth", lambda pid: epoch_now + member_boot_birth)
    monkeypatch.setattr(kb, "_run_env_escapees_detailed", lambda task_id, run_id: [])
    monkeypatch.setattr(kb, "_reap_now", lambda: 1_000.0)
    monkeypatch.setattr(kb.os, "getsid", getsid)
    monkeypatch.setattr(kb.os, "killpg", killpg)

    owner_window = kb._worker_owner_window(conn, tid, SID, run_id)
    kb._reap_terminated_worker_session(SID, {}, None, owner_window, None, conn, tid, run_id)
    return signalled


def test_backward_wall_step_never_signals_a_replacement_session(conn, monkeypatch):
    """Prism cc1f9d478681: wall -20 s after the last heartbeat. A replacement
    born at boot 205 (after the worker died) converts to wall 10_185, inside
    [claim 10_100, heartbeat 10_200]. On BOOTTIME it is plainly after 200."""
    assert _run(conn, monkeypatch, writer_boot_id="boot-B",
                member_boot_birth=205.0, wall_step=-20.0) == []


def test_forward_wall_step_still_reaps_the_runs_leftover(conn, monkeypatch):
    """Wall +20 s after the last heartbeat. The run's own child, born at boot
    190, converts to wall 10_210, past the heartbeat, and leaked. On BOOTTIME
    it is inside [100, 200]."""
    assert _run(conn, monkeypatch, writer_boot_id="boot-B",
                member_boot_birth=190.0, wall_step=20.0) == [222]


def test_evidence_from_another_boot_is_no_ownership(conn, monkeypatch, caplog):
    """BOOTTIME restarts at a reboot: evidence recorded under boot A says
    nothing about a process alive under boot B, even when the numbers fit."""
    with caplog.at_level(logging.INFO, logger=kb._log.name):
        assert _run(conn, monkeypatch, writer_boot_id="boot-A",
                    member_boot_birth=150.0, wall_step=0.0) == []
    assert "from another boot" in caplog.text


def test_old_row_without_boottime_falls_back_to_the_wall_conversion(conn, monkeypatch, caplog):
    """A row written before this change (or with no BOOTTIME) keeps the wall
    conversion with its margin, and says so in the log."""
    with caplog.at_level(logging.INFO, logger=kb._log.name):
        assert _run(conn, monkeypatch, writer_boot_id=None,
                    member_boot_birth=150.0, wall_step=0.0) == [222]
    assert "no CLOCK_BOOTTIME bound" in caplog.text


@pytest.mark.platforms("linux")
def test_liveness_events_record_this_boots_clock(conn):
    """On a real Linux board the claim and heartbeat rows carry this boot's id
    and a BOOTTIME no later than now; other event kinds carry none."""
    import time

    tid = kb.create_task(conn, title="card", assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "heartbeat", {}, run_id=run_id)
    now = time.clock_gettime(time.CLOCK_BOOTTIME)
    rows = {r["kind"]: r for r in conn.execute(
        "SELECT kind, boottime, boot_id FROM task_events WHERE task_id = ?", (tid,))}
    for kind in ("claimed", "heartbeat"):
        assert rows[kind]["boot_id"] == kb._boot_id()
        assert 0 < rows[kind]["boottime"] <= now
    assert rows["created"]["boottime"] is None
    assert kb._run_boot_evidence(conn, tid, run_id)[1] == (
        rows["heartbeat"]["boottime"], rows["heartbeat"]["boot_id"])
