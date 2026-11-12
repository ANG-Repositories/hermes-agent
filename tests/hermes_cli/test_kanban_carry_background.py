"""Workspace carry with no size cap, off the dispatcher tick (t_f3a411c2; Ace 2026-11-12 09:25 "no cap").

The pool host is a LOOPBACK here: every ``ssh <target> <script>`` the carry issues runs as ``bash -c <script>`` and
every rsync runs for real into a local directory, with the workspace path rewritten from the Studio root to a
``pool`` root. So the copies, the staging dir, ``df``, ``du``, the finalize ``mv`` and an rsync killed mid-copy are
real; only the network hop is not.

The 3 GiB / 120,000-entry fixture is opt-in (``CARRY_BIG_FIXTURE=1``): CI never writes 3 GiB.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_worker_hosts as kwh

from tests.hermes_cli.test_kanban_worker_pool import _plan, _tick, kanban_home  # noqa: F401
from tests.hermes_cli.test_kanban_studio_last_resort import _write_policy

try:  # absent on a tree before t_f3a411c2: the dispatch assertions below are then the red
    from hermes_cli import kanban_carry_background as kcb
except ImportError:  # pragma: no cover
    kcb = None


class Loopback:
    """A pool host that is this machine under ``pool``: ssh scripts run in bash, rsync copies locally."""

    def __init__(self, studio: Path, pool: Path, fake_bin: Path = None):
        self.studio, self.pool, self.fake_bin = str(studio), str(pool), fake_bin
        self.calls = []
        self.kill_rsync_after_files = None  # interrupt: kill the rsync once this many files reached the staging dir
        self.rsync_rc = []

    def _map(self, s: str) -> str:
        return s.replace(self.studio, self.pool)

    def __call__(self, argv, **kw):
        kw.pop("stdin", None)
        env = dict(os.environ)
        if self.fake_bin is not None:
            env["PATH"] = f"{self.fake_bin}:{env['PATH']}"
        if argv[0] == "ssh":
            self.calls.append(("ssh", self._map(argv[-1])))
            return subprocess.run(["bash", "-c", self._map(argv[-1])], env=env, stdin=subprocess.DEVNULL, **kw)
        assert argv[0] == "rsync", argv
        i = argv.index("-e")
        local = argv[:i] + argv[i + 2:]
        local[-1] = self._map(local[-1].split(":", 1)[1])
        self.calls.append(("rsync", local))
        if self.kill_rsync_after_files is None:
            proc = subprocess.run(local, env=env, stdin=subprocess.DEVNULL, **kw)
            self.rsync_rc.append(proc.returncode)
            return proc
        dest = Path(local[-1])
        # throttled so the kill lands mid-copy (an option: before the "--" that ends them)
        p = subprocess.Popen([local[0], "--bwlimit=2000", *local[1:]], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
        deadline = time.monotonic() + 60
        while p.poll() is None and time.monotonic() < deadline:
            if dest.is_dir() and sum(len(f) for _r, _d, f in os.walk(dest)) >= self.kill_rsync_after_files:
                p.kill()  # the interrupt: SIGKILL mid-copy, as a reboot or OOM would
                break
            time.sleep(0.02)
        out, err = p.communicate()
        self.rsync_rc.append(p.returncode)
        self.kill_rsync_after_files = None
        return subprocess.CompletedProcess(local, p.returncode, out, err)


@pytest.fixture
def board(kanban_home, monkeypatch):
    fleet = kanban_home / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    _write_policy(fleet)
    (kanban_home / "config.yaml").write_text("kanban:\n  studio_pin_requires_reason: false\n")
    kwh._carry_scan_cache.clear()
    kwh._carry_waiting.clear()
    if kcb is not None:
        kcb.reset()
    # the in-tick copy must never run on this path: a call is a test failure
    monkeypatch.setattr(kwh, "carry_workspace", lambda *a, **k: pytest.fail("in-tick carry_workspace ran"))
    yield kanban_home
    if kcb is not None:
        kcb.reset()


@pytest.fixture
def loop(board, tmp_path, monkeypatch):
    pool = tmp_path / "pool"
    pool.mkdir()
    lb = Loopback(board.parent, pool)
    if kcb is not None:
        monkeypatch.setattr(kcb, "_run", lb)
        monkeypatch.setattr(kcb, "RETRY_S", 0.0)
    # prepare_remote_workspace's default runner was bound at import; route it through the loopback too
    real_prep = kwh.prepare_remote_workspace
    monkeypatch.setattr(kwh, "prepare_remote_workspace",
                        lambda host, ws, runner=None, **kw: real_prep(host, ws, lb, **kw))
    return lb


def _card(conn, files=None):
    tid = kb.create_task(conn, title="lf", assignee="alpha")
    path = Path(kbw.resolve_workspace(kb.get_task(conn, tid)))
    path.mkdir(parents=True, exist_ok=True)
    for rel, data in (files or {"notes.md": b"prior run's work\n"}).items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(data)
    kbw.set_workspace_path(conn, tid, str(path))
    return tid, path


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, kind))]


def _comments(conn, tid):
    return [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (tid,))]


def _tick_full(conn, **kw):
    return _tick(conn, spillover=_plan(free=2), spawn_limit=0, spawn_paused="saturated:cpu", **kw)


def _wait_done(tid, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with kcb._lock:
            hit = kcb._jobs.get(tid)
        if hit is None or hit[0].future.done():
            return
        time.sleep(0.05)
    raise AssertionError("background carry did not finish")


def _tree(root: Path):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        for f in files:
            p = Path(dirpath) / f
            out[str(p.relative_to(root))] = p.stat().st_size
    return out


# -- the carry leaves the tick -------------------------------------------------------------------------------------

def test_tick_starts_the_carry_and_returns_while_it_copies(board, loop, monkeypatch):
    """The tick only submits the copy; a copy that blocks does not block the tick. The card waits carry_pending."""
    import threading
    gate = threading.Event()
    real = kcb.run

    def slow(job, runner=None):
        gate.wait(30)
        return real(job, runner)
    monkeypatch.setattr(kcb, "run", slow)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        t0 = time.monotonic()
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        tick_s = time.monotonic() - t0
        assert spawned == [] and res.placement_waits[tid] == "carry_pending:ace-ai", res.placement_waits
        assert _events(conn, tid, kcb.STARTED_EVENT) == [{"host": "ace-ai", "path": str(path), "background": True}]
        res, spawned = _tick_full(conn, defer_pool_clone=True)  # still copying: waits again, no second job
        assert spawned == [] and res.placement_waits[tid] == "carry_pending:ace-ai"
        assert len(_events(conn, tid, kcb.STARTED_EVENT)) == 1
        gate.set()
        _wait_done(tid)
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        ev = _events(conn, tid, kwh.CARRIED_EVENT)
    assert tick_s < 5.0, tick_s
    assert (tid, "ace-ai") in spawned
    assert ev[-1]["host"] == "ace-ai" and ev[-1]["path"] == str(path) and ev[-1]["background"]["entries"] == 1
    assert (Path(loop._map(str(path))) / "notes.md").read_bytes() == b"prior run's work\n"
    assert not Path(loop._map(kcb.staging_path(str(path)))).exists()
    assert (path / "notes.md").exists()  # the Studio copy is untouched


def test_over_the_old_caps_is_carried(board, loop):
    """25,000 entries / 1.1 GB sparse would have waited under CARRY_MAX_ENTRIES; it is carried now."""
    files = {f"d{i // 1000}/f{i}": b"x" for i in range(kwh.CARRY_MAX_ENTRIES + 5000)}
    with kb.connect_closing() as conn:
        tid, path = _card(conn, files)
        assert not kwh.carry_size_ok(str(path))  # the old rule's verdict on this dir
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
        ev = _events(conn, tid, kwh.CARRIED_EVENT)[-1]
    assert (tid, "ace-ai") in spawned
    assert ev["background"]["entries"] > kwh.CARRY_MAX_ENTRIES
    assert _tree(Path(loop._map(str(path)))) == _tree(path)


def test_one_shot_tick_carries_inline(board, loop):
    """A CLI tick (no defer) runs the same carry in the tick, as the pool clone does."""
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        _res, spawned = _tick_full(conn)
    assert (tid, "ace-ai") in spawned
    assert (Path(loop._map(str(path))) / "notes.md").exists()


# -- an interrupted copy -------------------------------------------------------------------------------------------

def test_interrupted_copy_is_resumable_named_and_resumed(board, loop):
    files = {f"d{i // 100}/f{i}": os.urandom(8192) for i in range(1500)}
    with kb.connect_closing() as conn:
        tid, path = _card(conn, files)
        loop.kill_rsync_after_files = 50
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        assert spawned == [] and res.placement_waits[tid].startswith("carry_failed:ace-ai: rsync rc=")
        failed = _events(conn, tid, kcb.FAILED_EVENT)
        assert failed[-1]["state"] == "resumable" and failed[-1]["phase"] == "copy"
        assert any("failed (resumable)" in c and ".carry-" in c for c in _comments(conn, tid))
        staging = Path(loop._map(kcb.staging_path(str(path))))
        partial = _tree(staging)
        assert 0 < len(partial) < len(files)
        # nothing half-carried at the workspace path itself: the card never ran against a partial copy
        assert _tree(Path(loop._map(str(path)))) == {}
        assert not _events(conn, tid, kwh.CARRIED_EVENT)
        # next attempt (RETRY_S = 0): resumes into the same staging dir, then finalizes
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
        ev = _events(conn, tid, kwh.CARRIED_EVENT)
    assert loop.rsync_rc[0] != 0 and loop.rsync_rc[-1] == 0
    assert (tid, "ace-ai") in spawned and ev
    assert _tree(Path(loop._map(str(path)))) == _tree(path)
    assert not staging.exists()


def test_a_dispatcher_restart_mid_carry_cleans_the_other_host(board, loop):
    """A started carry with no live job (the gateway restarted) placed on another host removes the staging dir it
    may have left on the first one."""
    from tests.hermes_cli.test_kanban_worker_pool import _host
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        kb._append_event(conn, tid, kcb.STARTED_EVENT, {"host": "ace-media", "path": str(path), "background": True})
        conn.commit()
        left = Path(loop._map(kcb.staging_path(str(path))))
        left.mkdir(parents=True)
        (left / "half").write_text("x")
        plan = _plan(free=2, hosts=[_host("ace-ai"), _host("ace-media", priority=9)])
        stale = kcb.stale_host(conn, tid, str(path), plan.hosts, "ace-ai")
        assert stale is not None and stale.name == "ace-media"
        job = kcb.Job(tid, plan.hosts["ace-ai"], str(path), stale)
        kcb.run(job)
    # loopback: both hosts are the same directory, so the cleanup ran before this carry's copy
    assert job.stale_cleaned is True
    assert not (Path(loop._map(kcb.staging_path(str(path)))) / "half").exists()


# -- no room on the destination ------------------------------------------------------------------------------------

def test_no_room_refuses_before_copying_naming_host_and_both_numbers(board, loop, tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "df").write_text("#!/bin/sh\necho 'Filesystem 1024-blocks Used Available Capacity Mounted'\n"
                             "echo '/dev/x 100 99 1 99% /'\n")
    (fake / "df").chmod(0o755)
    loop.fake_bin = fake
    with kb.connect_closing() as conn:
        tid, path = _card(conn, {"big": b"y" * 50_000})
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        failed = _events(conn, tid, kcb.FAILED_EVENT)[-1]
        comments = _comments(conn, tid)
    assert spawned == []
    assert res.placement_waits[tid].startswith("carry_no_room:ace-ai: No room on ace-ai: need ")
    assert "need 50" in failed["reason"] and "free 1024" in failed["reason"], failed
    assert failed["state"] == "clean"
    assert any("No room on ace-ai: need " in c and "nothing of this card on the host" in c for c in comments)
    assert not any(c[0] == "rsync" for c in loop.calls)  # nothing copied
    assert not Path(loop._map(kcb.staging_path(str(path)))).exists()


# -- what still keeps the files home --------------------------------------------------------------------------------

def test_a_checkout_whose_git_dir_does_not_travel_is_refused_and_keeps_the_capped_rule(board, loop, tmp_path,
                                                                                         monkeypatch):
    capped = []
    monkeypatch.setattr(kwh, "carry_workspace", lambda host, ws, runner=None, **kw: capped.append(ws))
    outside = tmp_path / "outside.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outside)], check=True)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        (path / "wt").mkdir()
        (path / "wt" / ".git").write_text(f"gitdir: {outside}\n")
        _tick_full(conn, defer_pool_clone=True)
        _wait_done(tid)
        _tick_full(conn, defer_pool_clone=True)
        failed = _events(conn, tid, kcb.FAILED_EVENT)[-1]
        assert failed["state"] == "refused"
        assert kcb.refused(conn, tid, str(path))
        assert capped == []  # nothing copied by the refused attempt
        _res, spawned = _tick_full(conn, defer_pool_clone=True)  # now the capped rule decides, as before
        started = _events(conn, tid, kcb.STARTED_EVENT)
    assert len(started) == 1 and capped == [str(path)]  # no second background attempt; the in-tick rule ran
    assert not Path(loop._map(kcb.staging_path(str(path)))).exists()


def test_a_checkout_rooted_at_the_workspace_whose_git_dir_does_not_travel_is_refused(board, loop, tmp_path):
    """Prism b68c58398adb: the git-state check runs from the workspace root, so a linked checkout AT the root counts."""
    outside = tmp_path / "outside.git"
    subprocess.run(["git", "init", "-q", "--bare", str(outside)], check=True)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        (path / ".git").write_text(f"gitdir: {outside}\n")
        job = kcb.Job(tid, _plan(free=2).hosts["ace-ai"], str(path))
        with pytest.raises(kcb.CarryRefused):
            kcb.run(job)
    assert not any(c[0] == "rsync" for c in loop.calls)


def test_the_carry_lock_is_held_from_the_start_until_the_finalize(board, loop, tmp_path):
    """Prism 18996b99e57f / b198d365c020: a finished copy keeps its (host, path) lock until its staging dir is
    consumed, so no other dispatcher can start an rsync into a staging dir being moved."""
    lock_dir = tmp_path / "locks"
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        host = _plan(free=2).hosts["ace-ai"]
        state, job = kcb.poll(tid, host, str(path), lock_dir)
        assert state == "started"
        _wait_done(tid)
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "done"
        lp = kcb.lock_path(lock_dir, host.name, str(path))
        assert kcb._try_lock(lp) is None          # another dispatcher: still locked after the copy finished
        assert kcb.take(tid, host, str(path)) is job
        assert kcb._try_lock(lp) is None          # still locked across the finalize
        kcb.finalize(host, str(path), runner=loop)
        kcb.release(job)
        fd = kcb._try_lock(lp)
    assert fd is not None
    kcb._unlock(fd)


def test_a_sync_carry_whose_claim_is_skipped_does_not_strand_its_lock(board, loop, tmp_path):
    """Prism eba22256a789: a one-shot carry that finished but whose claim never ran (claim_task None, PauseHeld, a
    route failure) is kept in the registry, not only in the tick's dict: the next carry of that card in the same
    process reuses its lock, and a finalize still releases it."""
    lock_dir = tmp_path / "locks"
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        host = _plan(free=2).hosts["ace-ai"]
        lp = kcb.lock_path(lock_dir, host.name, str(path))
        assert kcb.run_sync(kcb.Job(tid, host, str(path), None, lock_dir), lock_dir, runner=loop)
        # the tick that ran it exits without the claim: its tick-local job is dropped
        job2 = kcb.Job(tid, host, str(path), None, lock_dir)
        assert kcb.run_sync(job2, lock_dir, runner=loop)  # not stranded: the same process carries it again
        assert kcb.take(tid, host, str(path)) is job2
        kcb.finalize(host, str(path), runner=loop)
        kcb.release(job2)
        fd = kcb._try_lock(lp)
    assert fd is not None
    kcb._unlock(fd)


def test_a_kept_sync_carry_is_refreshed_in_a_process_that_never_started_a_background_one(
        board, loop, tmp_path, monkeypatch):
    """Prism 64edba28c538: a one-shot carry kept in the registry and then polled once older than FRESH_S is
    refreshed on the carry pool, which this process may never have created (no background carry ran yet)."""
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(kcb, "_pool", None)
    monkeypatch.setattr(kcb, "FRESH_S", 0.0)
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        host = _plan(free=2).hosts["ace-ai"]
        job = kcb.Job(tid, host, str(path), None, lock_dir)
        assert kcb.run_sync(job, lock_dir, runner=loop)
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "running"
        _wait_done(tid)
        monkeypatch.setattr(kcb, "FRESH_S", 3600.0)
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "done"
        assert kcb.take(tid, host, str(path)) is job
        kcb.finalize(host, str(path), runner=loop)
        kcb.release(job)


def test_a_finished_copy_that_waited_is_refreshed_before_the_claim(board, loop, tmp_path, monkeypatch):
    """Prism 38c86a910163: a Studio edit made after the copy finished reaches the pool host when the finished copy
    is older than FRESH_S: it is re-synced off the tick, never claimed stale."""
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(kcb, "FRESH_S", 0.0)
    with kb.connect_closing() as conn:
        tid, path = _card(conn, {"notes.md": b"v1\n"})
        host = _plan(free=2).hosts["ace-ai"]
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "started"
        _wait_done(tid)
        (path / "notes.md").write_bytes(b"v2\n")
        later = time.time() + 5
        os.utime(path / "notes.md", (later, later))   # an edit after the copy: rsync's quick check sees the mtime
        assert kcb.poll(tid, host, str(path), lock_dir)[0] == "running"   # the refresh, off the tick
        _wait_done(tid)
        monkeypatch.setattr(kcb, "FRESH_S", 3600.0)
        state, job = kcb.poll(tid, host, str(path), lock_dir)
        assert state == "done"
        kcb.take(tid, host, str(path))
        kcb.finalize(host, str(path), runner=loop)
        kcb.release(job)
    assert (Path(loop._map(str(path))) / "notes.md").read_bytes() == b"v2\n"


def test_stale_staging_is_left_alone_while_its_host_lock_is_held(board, loop, tmp_path):
    """Prism 4d42b4210a5a: the stale host's staging dir is removed only under that host's carry lock."""
    from tests.hermes_cli.test_kanban_worker_pool import _host
    lock_dir = tmp_path / "locks"
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        plan = _plan(free=2, hosts=[_host("ace-ai"), _host("ace-media", priority=9)])
        left = Path(loop._map(kcb.staging_path(str(path))))
        left.mkdir(parents=True)
        (left / "half").write_text("x")
        held = kcb._try_lock(kcb.lock_path(lock_dir, "ace-media", str(path)))   # ace-media's carry is live
        try:
            job = kcb.Job(tid, plan.hosts["ace-ai"], str(path), plan.hosts["ace-media"], lock_dir)
            removes = []
            real = kcb.remove_staging
            kcb.remove_staging, _r = (lambda h, ws, runner=None: removes.append(h.name) or real(h, ws, runner)), real
            try:
                kcb.run(job)
            finally:
                kcb.remove_staging = _r
        finally:
            kcb._unlock(held)
    assert job.stale_cleaned is False and removes == []


def test_finalize_into_a_nonempty_workspace_never_writes_through_a_symlink(board, loop, tmp_path):
    """Prism 0e52f77edd32: a destination file symlink is replaced, as the in-tick rsync carry did."""
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
    host = _plan(free=2).hosts["ace-ai"]
    dest = Path(loop._map(str(path)))
    dest.mkdir(parents=True)
    victim = tmp_path / "important.txt"
    victim.write_text("keep me\n")
    (dest / "notes.md").symlink_to(victim)
    (dest / "other").write_text("left\n")
    staging = Path(loop._map(kcb.staging_path(str(path))))
    staging.mkdir(parents=True)
    (staging / "notes.md").write_text("new\n")
    kcb.finalize(host, str(path), runner=loop)
    assert victim.read_text() == "keep me\n"
    assert not (dest / "notes.md").is_symlink() and (dest / "notes.md").read_text() == "new\n"
    assert (dest / "other").read_text() == "left\n" and not staging.exists()


def test_kill_switch_restores_the_capped_in_tick_carry(board, loop, monkeypatch):
    (board / "config.yaml").write_text("kanban:\n  studio_pin_requires_reason: false\n  placement:\n"
                                       "    carry_caps: true\n")
    assert kcb.caps_on()
    calls = []
    monkeypatch.setattr(kwh, "carry_workspace", lambda host, ws, runner=None, **kw: calls.append((host.name, ws)))
    with kb.connect_closing() as conn:
        tid, path = _card(conn)
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
        assert not _events(conn, tid, kcb.STARTED_EVENT)
    assert calls == [("ace-ai", str(path))] and (tid, "ace-ai") in spawned
    monkeypatch.setattr(kwh, "carry_size_ok", lambda path, **_kw: False)
    with kb.connect_closing() as conn:
        tid2, _ = _card(conn)
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
    assert (tid2, "ace-ai") not in spawned  # over the caps: waits, exactly as before


def test_caps_knob_parsing():
    assert kcb.caps_on({}) is False
    assert kcb.caps_on({"placement": {"carry_caps": True}}) is True
    assert kcb.caps_on({"placement": {"carry_caps": "off"}}) is False
    assert kcb.caps_on({"placement": {"carry_caps": "on"}}) is True


# -- the acceptance fixture: >= 3 GiB, >= 120,000 entries (opt-in) --------------------------------------------------

@pytest.mark.slow_test(3000, reason="writes and carries a 3 GiB / 120,000-entry fixture; opt-in only")
@pytest.mark.skipif(os.environ.get("CARRY_BIG_FIXTURE") != "1", reason="writes 3 GiB; CARRY_BIG_FIXTURE=1")
def test_three_gib_120k_entries_is_carried_and_the_tick_returns_fast(board, loop, capsys):
    import resource
    n_dirs, per_dir, size = 120, 1000, 27_000
    block = os.urandom(size)
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="big", assignee="alpha")
        path = Path(kbw.resolve_workspace(kb.get_task(conn, tid)))
        for d in range(n_dirs):
            sub = path / f"d{d:03d}"
            sub.mkdir(parents=True, exist_ok=True)
            for f in range(per_dir):
                (sub / f"f{f:04d}").write_bytes(block)
        kbw.set_workspace_path(conn, tid, str(path))
        total, entries = kcb.census(str(path))
        assert total >= 3 * 1024 ** 3 and entries >= 120_000, (total, entries)
        assert not kwh.carry_size_ok(str(path))
        t0 = time.monotonic()
        res, spawned = _tick_full(conn, defer_pool_clone=True)
        tick_s = time.monotonic() - t0
        assert res.placement_waits[tid] == "carry_pending:ace-ai" and spawned == []
        c0 = resource.getrusage(resource.RUSAGE_SELF)
        _wait_done(tid, timeout=3600)
        c1 = resource.getrusage(resource.RUSAGE_SELF)
        t1 = time.monotonic()
        _res, spawned = _tick_full(conn, defer_pool_clone=True)
        claim_tick_s = time.monotonic() - t1
        ev = _events(conn, tid, kwh.CARRIED_EVENT)[-1]
    dest = Path(loop._map(str(path)))
    file_bytes = lambda root: sum(_tree(root).values())  # noqa: E731 (dir st_size differs by creation order)
    with capsys.disabled():
        print(f"\nBIG CARRY: bytes={total} entries={entries} start_tick_s={tick_s:.3f} "
              f"claim_tick_s={claim_tick_s:.3f} carry={json.dumps(ev['background'])} "
              f"dispatcher_self_cpu_s={(c1.ru_utime - c0.ru_utime) + (c1.ru_stime - c0.ru_stime):.2f}")
    assert (tid, "ace-ai") in spawned
    assert kcb.census(str(dest))[1] == entries and file_bytes(dest) == file_bytes(path)
    assert tick_s < 5.0
